import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import pymupdf
from fastapi.testclient import TestClient

import app as service
import pdf_document as structure
import pdf_model
import pdf_translation as transport
import pdf_layout
import pdf_visual_check
import httpx


def fixture(rotation=0, columns=1, crop=False):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=620, height=480)
        if crop:
            page.set_cropbox(pymupdf.Rect(20, 20, 600, 460))
        for column in range(columns):
            x = 35 + column * 280
            page.insert_text((x, 60), 'Method. ', fontsize=11, fontname='hebo')
            page.insert_text((x + 48, 60), 'The state is ', fontsize=11, fontname='helv')
            page.insert_text((x + 106, 60), 'x', fontsize=11, fontname='symb')
            page.insert_text((x + 116, 60), ' and remains stable.', fontsize=11, fontname='helv')
            page.insert_text((x, 110), 'The next paragraph describes our results.', fontsize=11)
        page.set_rotation(rotation)
        return pdf.tobytes()


def packet_fixture():
    block = {'id': 'p1-b1', 'text': 'Label: x and x remain stable.', 'translation': '', 'target': ''}
    base = {'bold': False, 'italic': False, 'color': 0, 'script': ''}
    para = {'base_style': base, 'formulas': [
        {'id': 'first', 'start': 7, 'end': 8, 'text': 'x', 'asset': None},
        {'id': 'second', 'start': 13, 'end': 14, 'text': 'x', 'asset': None}],
        'runs': [{'start': 0, 'end': 6, 'text': 'Label:', 'style': {**base, 'bold': True}}],
        'issues': [], 'role': 'plain text'}
    return block, para


class TransportTests(unittest.TestCase):
    def test_markers_restore_plain_text_and_local_styles(self):
        block, para = packet_fixture()
        packet = transport.build_input(block, para)
        raw = packet.text.replace('Label:', '标签：').replace(' and ', ' 与 ').replace('remain stable.', '保持稳定。')
        text, record = transport.restore_output(packet, raw, '简体中文')
        self.assertEqual(text, '标签： x 与 x 保持稳定。')
        self.assertNotIn('[[PT_', text)
        self.assertEqual(len(record['formulas']), 2)
        self.assertTrue(record['styles'][0]['style']['bold'])
        self.assertEqual(text[record['styles'][0]['start']:record['styles'][0]['end']], '标签：')

    def test_missing_duplicate_unknown_and_crossed_markers_rejected(self):
        block, para = packet_fixture()
        packet = transport.build_input(block, para)
        token = next(iter(packet.formulas))
        left = next(iter(packet.styles)); right = packet.styles[left]['end_token']
        bad = [packet.text.replace(token, ''), packet.text + token,
               packet.text + packet.prefix + 'F999]]', packet.text.replace(right, '').replace(left, right),
               packet.text.replace(token, token[:-1])]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                transport.restore_output(packet, value, 'zh')

    def test_source_collision_and_literal_marker_survive(self):
        block, para = packet_fixture()
        block['text'] += ' literal [[PT_literal_F0]]'
        packet = transport.build_input(block, para)
        text, _ = transport.restore_output(packet, packet.text, 'en')
        self.assertEqual(text, block['text'])

    def test_formula_equivalent_spelling_and_repetitions(self):
        block = {'text': 'xn and ˙x and x and x', 'translation': 'x_n 与 ẋ，还有 x 和 x', 'target': 'zh'}
        para = {'formulas': [{'id': str(i), 'text': text} for i, text in enumerate(['xn', '˙x', 'x', 'x'])]}
        record, issues = transport.legacy_record(block, para)
        self.assertFalse(issues)
        self.assertEqual(len(record['formulas']), 4)
        self.assertEqual(len({(f['start'], f['end']) for f in record['formulas']}), 4)

    def test_legacy_ambiguous_or_missing_formula_preserved(self):
        block, para = packet_fixture()
        block.update(translation='这里仅有 x', target='zh')
        result = transport.layout_input(block, para)
        self.assertIn('_structure_issue', result)
        self.assertEqual(block['translation'], '这里仅有 x')

    def test_edit_invalidates_sidecar_and_group_offsets(self):
        block, para = packet_fixture(); packet = transport.build_input(block, para)
        text, record = transport.restore_output(packet, packet.text, 'en')
        block.update(translation=text, target='en')
        with tempfile.TemporaryDirectory() as directory:
            transport.save_records(directory, {block['id']: record})
            doc = {'pages': [{'blocks': [block]}]}
            self.assertEqual(len(transport.load_records(directory, doc)), 1)
            block['translation'] += '!'
            self.assertEqual(transport.load_records(directory, doc), {})
        block['translation'] = text
        first = {'id': 'prefix', 'text': 'Intro', 'translation': 'Intro', 'target': 'en'}
        combined = {**block, '_members': ['prefix', block['id']], 'translation': 'Intro ' + text}
        result = transport.project_group(combined, {'prefix': first, block['id']: block}, {block['id']: para}, {block['id']: record})
        self.assertEqual(result['_ir_styles'][0]['start'], 6)

    def test_style_ranges_survive_cjk_spacing_without_whole_body_bold(self):
        block = {'translation': '标签x与正文', '_ir_styles': [{'start': 0, 'end': 2, 'style': {'bold': True, 'italic': False, 'color': 0}}]}
        projected = transport.project_styles(block, '标签 x 与正文')
        html = pdf_layout._bold_html('标签 x 与正文', 0, 8, (), styles=projected)
        self.assertIn('font-weight:600', html)
        self.assertIn('</span> x 与正文', html)

    def test_style_offsets_survive_renderer_soft_newline_normalization(self):
        block={'translation':'前文 后续 标签','_ir_translation':'前文\n\n后续 标签',
               '_ir_styles':[{'start':7,'end':9,'style':{'bold':True}}]}
        spans=transport.project_styles(block,'前文 后续 标签')
        self.assertEqual('前文 后续 标签'[spans[0]['start']:spans[0]['end']],'标签')


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = patch.object(service, 'DATA', self.root); self.data.start()
        self.model = patch.object(pdf_model, 'detect_page', side_effect=pdf_model.VisionError('offline test')); self.model.start()

    def tearDown(self):
        self.model.stop(); self.data.stop(); self.temp.cleanup()

    def document(self, **kwargs):
        identifier, doc = service.extract_document(fixture(**kwargs), 'fixture.pdf')
        return self.root / identifier, doc

    def test_coordinates_and_ids_across_rotations_columns_and_crop(self):
        for rotation in (0, 90, 180, 270):
            for columns in (1, 2):
                with self.subTest(rotation=rotation, columns=columns):
                    directory, doc = self.document(rotation=rotation, columns=columns, crop=True)
                    before = copy.deepcopy(doc)
                    ir = structure.ensure_pages(directory, doc)[1]
                    self.assertEqual(doc, before)
                    self.assertEqual(ir['rotation'], rotation)
                    self.assertEqual(ir['reading_order'], [b['id'] for b in doc['pages'][0]['blocks']])
                    self.assertEqual(ir['model']['state'], 'unavailable')
                    for b, para in zip(doc['pages'][0]['blocks'], ir['paragraphs']):
                        with pymupdf.open(directory/'source.pdf') as pdf:
                            expected = pdf_layout.source_rect(pdf[0], b)
                        self.assertLess(max(abs(a-b) for a,b in zip(expected, para['bbox'])), .001)
                    formula = next(p for p in ir['paragraphs'] if p['formulas'])
                    self.assertFalse(formula['base_style']['bold'])
                    self.assertTrue(formula['formulas'][0]['asset']['svg'].startswith('<svg'))

    def test_structure_cache_ignores_translation_and_invalidates_source(self):
        directory, doc = self.document()
        first = structure.ensure_pages(directory, doc)
        doc['pages'][0]['blocks'][0]['translation'] = '用户编辑'
        with patch.object(structure, '_build_page', side_effect=AssertionError('rebuilt')):
            self.assertEqual(first, structure.ensure_pages(directory, doc))
        doc['pages'][0]['blocks'][0]['text'] += ' updated'
        self.assertNotEqual(first[1]['fingerprint'], structure.ensure_pages(directory, doc)[1]['fingerprint'])

    def test_success_empty_detection_and_recovery_after_unavailable(self):
        directory, doc = self.document()
        config={'base_url':'https://example.com/v1','model':'vision','mode':'openai'}
        with pdf_model.using(config), patch.object(pdf_model, 'detect_page', return_value=[]) as infer:
            ready = structure.ensure_pages(directory, doc)
            self.assertEqual(ready[1]['model']['state'], 'ready')
            self.assertEqual(ready[1]['regions'], [])
            structure.ensure_pages(directory, doc)
            self.assertEqual(infer.call_count, 1)
            with pdf_model.using({**config,'model':'another'}):
                structure.ensure_pages(directory, doc)
            self.assertEqual(infer.call_count, 2)

    def test_model_conflict_and_graphic_evidence_required(self):
        box = [0, 0, 100, 100]
        figure = {'bbox': box, 'label': 'figure', 'confidence': .9}
        self.assertEqual(structure._role(box, [figure], '', []), 'unknown')
        self.assertEqual(structure._role(box, [figure], '', [box]), 'figure')
        self.assertEqual(structure._role(box, [figure, {**figure, 'label': 'plain text'}], '', [box]), 'unknown')

    def test_author_superscript_is_not_formula(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((40, 60), 'Alice', fontsize=12)
            page.insert_text((70, 55), '1,2', fontsize=7)
            identifier, doc = service.extract_document(pdf.tobytes(), 'authors.pdf')
        ir = structure.ensure_pages(self.root/identifier, doc)[1]
        self.assertFalse(any(p['formulas'] for p in ir['paragraphs']))

    def test_hidden_ocr_page_is_preserved_without_double_printing(self):
        with pymupdf.open() as image_pdf, pymupdf.open() as pdf:
            p=image_pdf.new_page(width=400,height=300)
            p.insert_text((40,60),'Scanned text on the image.',fontsize=12)
            image=p.get_pixmap().tobytes('png')
            p=pdf.new_page(width=400,height=300)
            p.insert_image(p.rect,stream=image)
            p.insert_text((40,60),'Scanned text on the image.',fontsize=12,render_mode=3)
            identifier,doc=service.extract_document(pdf.tobytes(),'scan.pdf')
        directory=self.root/identifier
        for block in doc['pages'][0]['blocks']:block.update(translation='不应叠印',target='简体中文')
        report=pdf_layout.build_layout(directory,doc,'简体中文')
        self.assertEqual(report['replaced'],0)
        self.assertEqual(report['preserved'],doc['total_blocks'])
        with pymupdf.open(directory/'source.pdf') as original,pymupdf.open(directory/'layout'/(report['revision']+'.pdf')) as output:
            self.assertEqual(original[0].get_pixmap().samples,output[0].get_pixmap().samples)

    def test_corrupt_cache_rebuilds_and_structure_failure_reports(self):
        directory,doc=self.document()
        structure.ensure_pages(directory,doc)
        for path in (directory/'structure').glob('*.json'):path.write_text('{}',encoding='utf-8')
        structure._CACHE.clear()
        with patch.object(structure,'_build_page',side_effect=ValueError('damaged glyph')):
            pages=structure.ensure_pages(directory,doc)
        self.assertTrue(pages[1]['paragraphs'][0]['issues'])
        self.assertEqual(pages[1]['model']['state'],'unavailable')

    def test_structure_failure_excludes_only_unsafe_batch_member(self):
        directory,doc=self.document()
        first,second=doc['pages'][0]['blocks'][:2]
        structures={first['id']:{'issues':['损坏公式'],'pure_formula':False},second['id']:None}
        reply=AsyncMock(return_value=json.dumps({'blocks':[{'id':second['id'],'translation':'有效译文'}]}))
        with patch.object(service,'translation_structures',AsyncMock(return_value=structures)), patch.object(service,'invoke_agent',reply):
            result=asyncio.run(service.translate_batch(doc['id'],service.BatchTranslateRequest(agent=service.AgentConfig(),block_ids=[first['id'],second['id']])))
        context=json.loads(reply.await_args.args[1].split('参考资料与本次待翻译段落：\n')[1])
        self.assertEqual([b['id'] for b in context['current_blocks']],[second['id']])
        self.assertEqual(result['missing'],[first['id']])

    def test_pure_equation_bypasses_model_translation(self):
        with pymupdf.open() as pdf:
            p = pdf.new_page(); p.insert_text((40, 60), 'E = mc2', fontsize=12)
            identifier, doc = service.extract_document(pdf.tobytes(), 'equation.pdf')
        block = doc['pages'][0]['blocks'][0]
        with patch.object(service, 'invoke_agent', AsyncMock(side_effect=AssertionError('called'))):
            result = asyncio.run(service.translate(identifier, block['id'], service.TranslateRequest(agent=service.AgentConfig())))
        self.assertEqual(result['translation'], block['text'])

    def test_api_partial_batch_and_edit_keep_plain_text_and_ids(self):
        directory, doc = self.document()
        block, other = doc['pages'][0]['blocks'][:2]
        async def reply(config, prompt, *args, **kwargs):
            context = json.loads(prompt.split('参考资料与本次待翻译段落：\n')[1])
            return json.dumps({'blocks': [{'id': b['id'], 'translation': b['source'] if b['id'] == block['id'] else ''} for b in context['current_blocks']]})
        with patch.object(service, 'invoke_agent', reply):
            result = asyncio.run(service.translate_batch(doc['id'], service.BatchTranslateRequest(agent=service.AgentConfig(), block_ids=[block['id'], other['id']])))
        self.assertEqual(result['missing'], [other['id']])
        self.assertNotIn('[[PT_', result['blocks'][0]['translation'])
        saved = service.snapshot_doc(doc['id'])
        self.assertEqual(len(transport.load_records(directory, saved)), 1)
        service.edit(doc['id'], block['id'], service.EditRequest(translation='手动译文', target='简体中文'))
        self.assertEqual(transport.load_records(directory, service.snapshot_doc(doc['id'])), {})

    def test_missing_formula_never_overwrites_existing_translation(self):
        directory, doc = self.document()
        block = doc['pages'][0]['blocks'][0]
        block.update(translation='已保存译文', target='简体中文', edited=True)
        service.save_doc(directory, doc)
        with patch.object(service, 'invoke_agent', AsyncMock(return_value='丢失公式的译文')):
            with self.assertRaises(service.HTTPException):
                asyncio.run(service.translate(doc['id'], block['id'], service.TranslateRequest(agent=service.AgentConfig())))
        self.assertEqual(service.load_doc(doc['id'])['pages'][0]['blocks'][0]['translation'], '已保存译文')

    def test_visual_issues_prevent_automatic_export_but_keep_preview(self):
        directory, doc = self.document()
        for b in doc['pages'][0]['blocks']:b.update(translation=b['text'],target='en')
        service.save_doc(directory,doc)
        issue={'status':'issues','issues':[{'kind':'overlap','severity':'error','message':'文字与图形重叠','block_id':None,'bbox':[.1,.1,.5,.5]}]}
        request=service.LayoutRequest(target='en',agent=service.AgentConfig(base_url='https://example.com/v1',model='vision'))
        with patch.object(pdf_model,'request_json',return_value=issue),patch.object(service,'save_finished_pdf') as save:
            report=service.layout(doc['id'],request)
        self.assertEqual(report['visual_check']['status'],'issues');save.assert_not_called()
        self.assertTrue((directory/'layout'/(report['revision']+'.pdf')).exists())
        self.assertIn('save_error',report)

    def test_visual_pass_allows_export_and_configuration_not_saved(self):
        directory,doc=self.document()
        for b in doc['pages'][0]['blocks']:b.update(translation=b['text'],target='en')
        service.save_doc(directory,doc)
        request=service.LayoutRequest(target='en',agent=service.AgentConfig(base_url='https://example.com/v1',model='vision',api_key='never-write-this-key'))
        with patch.object(pdf_model,'request_json',return_value={'status':'passed','issues':[]}),patch.object(service,'save_finished_pdf') as save:
            report=service.layout(doc['id'],request)
        self.assertEqual(report['visual_check']['status'],'passed');save.assert_called_once()
        for path in directory.rglob('*.json'):self.assertNotIn('never-write-this-key',path.read_text(encoding='utf-8'))


class VisionAPITests(unittest.TestCase):
    config={'mode':'openai','base_url':'https://example.com/v1','model':'vision-model','api_key':'secret-key','reasoning':'off','timeout':10}

    def test_shared_custom_template_preserves_structured_text_messages(self):
        messages=[{'role':'user','content':'translate "quoted" text'}]
        expanded=service.expand_template({'messages':'{{messages}}','images':'{{images}}','debug':'values {{messages}}'},
                                         {'messages':messages,'images':[]})
        self.assertEqual(expanded['messages'],messages)
        self.assertEqual(expanded['images'],[])
        self.assertTrue(expanded['debug'].startswith('values [{'))

    def test_same_translation_endpoint_model_auth_and_real_image_payload(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'regions':[{'label':'plain text','bbox':[.1,.2,.5,.7],'confidence':.9}]})}}]})
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.pdf';source.write_bytes(fixture(rotation=90,crop=True))
            with pdf_model.using(self.config), httpx.Client(transport=httpx.MockTransport(handler)) as client, patch.object(pdf_model,'_CLIENT',client):
                regions=pdf_model.detect_page(source,{'number':1})
        request=seen[0];body=json.loads(request.content)
        self.assertEqual(str(request.url),'https://example.com/v1/chat/completions')
        self.assertEqual(request.headers['Authorization'],'Bearer secret-key')
        self.assertEqual(body['model'],'vision-model');self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertTrue(body['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertEqual(len(regions),1)
        self.assertNotIn('secret-key',pdf_model.fingerprint())

    def test_invalid_coordinates_and_truncated_response_rejected(self):
        for value in ([0,0,2,1],[.5,0,.2,1],[0,0,float('nan'),1]):
            with self.assertRaises(pdf_model.VisionError):pdf_model.box(value)
        with pdf_model.using(self.config), httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'{}'}}]}))) as client, patch.object(pdf_model,'_CLIENT',client):
            with self.assertRaises(pdf_model.VisionError):pdf_model.request_json('vision_layout','check',['data:image/png;base64,AA=='])

    def test_custom_interface_requires_structured_images_and_uses_response_path(self):
        c={**self.config,'mode':'custom','base_url':'https://example.com/process','template':'{"input":"{{prompt}}"}','response_path':'data'}
        with pdf_model.using(c),self.assertRaises(pdf_model.VisionError):pdf_model.request_json('vision_layout','test',['data:image/png;base64,AA=='])
        c['template']='{"messages":"{{messages}}","model":"{{model}}"}'
        def handler(request):
            body=json.loads(request.content);self.assertIsInstance(body['messages'],list)
            return httpx.Response(200,json={'data':{'regions':[]}})
        with pdf_model.using(c), httpx.Client(transport=httpx.MockTransport(handler)) as client, patch.object(pdf_model,'_CLIENT',client):
            self.assertEqual(pdf_model.request_json('vision_layout','test',['data:image/png;base64,AA==']),{'regions':[]})

    def test_acceptance_checks_both_images_caches_and_invalidates_on_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);(directory/'layout').mkdir();(directory/'source.pdf').write_bytes(fixture())
            shutil=__import__('shutil');shutil.copy2(directory/'source.pdf',directory/'layout'/'revision.pdf')
            doc={'pages':[{'number':1,'blocks':[{'id':'b','text':'Original','translation':'译文','target':'zh'}]}]}
            report={'revision':'revision','target':'zh','issues':[]}
            def check(task,prompt,images):
                self.assertEqual(task,'vision_acceptance');self.assertEqual(len(images),2)
                return {'status':'passed','issues':[]}
            with pdf_model.using(self.config),patch.object(pdf_model,'request_json',side_effect=check) as call:
                self.assertEqual(pdf_visual_check.validate(directory,doc,report)['visual_check']['status'],'passed')
                pdf_visual_check.validate(directory,doc,report);self.assertEqual(call.call_count,1)
                doc['pages'][0]['blocks'][0]['translation']='新译文'
                pdf_visual_check.validate(directory,doc,report);self.assertEqual(call.call_count,2)
            for path in (directory/'structure'/'visual-checks').glob('*.json'):self.assertNotIn('secret-key',path.read_text())

    def test_acceptance_failure_is_not_pass_or_cached_success(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);(directory/'layout').mkdir();raw=fixture();(directory/'source.pdf').write_bytes(raw);(directory/'layout'/'revision.pdf').write_bytes(raw)
            doc={'pages':[{'number':1,'blocks':[{'id':'b','text':'source','translation':'译文','target':'zh'}]}]}
            report={'revision':'revision','target':'zh','issues':[]}
            with pdf_model.using(self.config),patch.object(pdf_model,'request_json',side_effect=pdf_model.VisionError('unsupported images')):
                result=pdf_visual_check.validate(directory,doc,report)
            self.assertEqual(result['visual_check']['status'],'unavailable')
            self.assertTrue(result['issues']);self.assertFalse((directory/'structure'/'visual-checks').exists())
        with self.assertRaises(pdf_model.VisionError):pdf_visual_check._validate_result({'status':'passed','issues':[{}]},set())


if __name__ == '__main__':
    unittest.main()
