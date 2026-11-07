import json
import copy
import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import pymupdf
from fastapi import HTTPException
import app as service
import paper_reading as reading
import reading_workflow as workflow
from reading_text import CitationRequest, text_layer, resolve_citation
import test_paper_reading as baseline


class WorkflowTests(unittest.TestCase):
    upload=baseline.ReadingTests.upload
    start=baseline.ReadingTests.start
    finish=baseline.ReadingTests.finish
    tearDown=baseline.ReadingTests.tearDown

    def setUp(self):
        baseline.ReadingTests.setUp(self)
        path=service.doc_dir(self.doc['id'])/'reading.json'
        data=json.loads(path.read_text(encoding='utf-8'));data['workflow']=workflow.snapshot(service.ROOT)
        path.write_text(json.dumps(data),encoding='utf-8');self.snapshot=data['workflow']

    def payload(self, messages):
        for message in messages:
            if message['role'] == 'user' and isinstance(message['content'], str):
                try:
                    value = json.loads(message['content'])
                    if isinstance(value, dict) and 'instruction' in value:
                        return value
                except ValueError:
                    pass
        raise AssertionError('No material payload')

    async def respond(self,module,config,messages,tools):
        self.calls.append(copy.deepcopy(messages))
        if tools and '本轮为统一上下文阅读' in messages[0]['content']:
            return dict(role='assistant',content='材料已查看，可以生成最终回答。',tool_calls=[])
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):raise response
            return response
        mode='quickread'
        for name,spec in self.snapshot['manifest']['modes'].items():
            if workflow.instructions(self.snapshot,name) in messages[0]['content']:mode=name
        payload = self.payload(messages)
        ids = [r['id'] for material in payload['selected_material'] for r in material['rows']]
        if not ids:
            ids = payload['material_coverage']['image_sources']
        sections=[dict(section=f,text='基于所提供的论文材料。',kind='claim',source_ids=ids[:1]) for f in self.snapshot['manifest']['modes'][mode]['fields']]
        return dict(role='assistant',content=json.dumps(dict(status='complete' if mode in ('quickread','citation') else 'incomplete',sections=sections,missing=[],issues=[])),tool_calls=[])

    def test_workflow_loads_material_and_returns_structured_response(self):
        job=self.finish(self.start())
        self.assertEqual(job['status'],'completed',job)
        turn=self.client.get(self.url).json()['messages'][-1]
        self.assertEqual(turn['mode_id'],'quickread')
        self.assertEqual(turn['structured']['status'],'complete')
        self.assertEqual(turn['structured']['missing'],[])
        self.assertIn('WORKFLOW_RESPONSE_JSON',self.calls[0][0]['content'])
        self.assertTrue(self.payload(self.calls[0])['selected_material'])
        self.assertEqual(len(turn['visual_sources']),2)

    def test_invalid_response_repaired_once_then_rejected(self):
        self.responses=[dict(role='assistant',content='自由发挥，不符合工作流',tool_calls=[]) for _ in range(2)]
        job=self.finish(self.start());self.assertEqual(job['status'],'failed')
        self.assertEqual(len(self.calls),3)
        self.assertEqual(self.client.get(self.url).json()['messages'],[])

    def test_whole_paper_intent_routes_composer_messages_without_hijacking_local_questions(self):
        for question in ('分析整篇论文，生成文章速读并给出建议阅读路线。', '请帮我通读全文并总结',
                         '请对整篇论文进行分析', '重新生成文章速读', '论文概览',
                         'Please analyze the whole paper and suggest a reading route.'):
            with self.subTest(question=question):
                self.assertEqual(workflow.mode_for('message','followup',question),'quickread')
        for question in ('这一步为什么？', '请解释文章速读怎么使用', '不要分析整篇论文，只解释这句话',
                         '请分析这个公式在全文中的作用', '论文速读结果中这个术语什么意思？'):
            with self.subTest(question=question):
                self.assertEqual(workflow.mode_for('message','followup',question),'followup')
        self.assertEqual(workflow.mode_for('message','detail','分析整篇论文'),'detail')
        self.assertEqual(workflow.mode_for('continue',None,'分析整篇论文'),'guided')

    def test_typed_fulltext_request_uses_full_pipeline_and_clears_stale_selection(self):
        self.fulltext_document()
        question = '分析整篇论文，生成文章速读并给出建议阅读路线。'
        result = self.client.post(self.url+'/messages',json=dict(agent=self.agent,revision=0,
            action='message',mode_id='followup',question=question,block_id=self.doc['pages'][0]['blocks'][0]['id']))
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['mode_id'],'quickread')
        job = self.finish(result.json()['id'])
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual(job['progress']['percent'],100)
        data = self.client.get(self.url).json()
        self.assertEqual(data['messages'][-1]['question'],question)
        self.assertEqual(data['messages'][-1]['mode_id'],'quickread')
        self.assertEqual(data['messages'][-1]['analysis_scope']['text_pages'],[1,2,3,4])
        self.assertIsNone(data['messages'][-1]['context']['selected_block'])
        self.assertEqual(''.join(r['text'] for m in self.payload(self.calls[0])['selected_material'] for r in m['rows']),
                         ''.join(b['text'] for p in self.doc['pages'] for b in p['blocks']))

    def test_fulltext_synthesis_closes_tools_after_visual_inspection(self):
        self.fulltext_document()
        original_respond = self.respond
        tool_sets = []
        async def respond(module,config,messages,tools):
            tool_sets.append([t['function']['name'] for t in tools])
            self.assertEqual(len([m for m in messages if isinstance(m['content'],list)]),5)
            self.assertNotIn('analysis_progress',self.payload(messages))
            return await original_respond(module,config,messages,tools)
        with patch.object(reading,'invoke',respond):job=self.finish(self.start())
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual(tool_sets,[['render_page'],[]])

    def test_tool_requests_from_misbehaving_final_model_fail_after_one_correction(self):
        self.finish(self.start())
        previous = self.client.get(self.url).json()['messages']
        self.calls.clear()
        self.responses = [baseline.call('read_material',page=1) for _ in range(2)]
        job = self.finish(self.start())
        self.assertEqual(job['status'],'failed',job)
        self.assertIn('模型仍未返回最终回答',job['error'])
        self.assertEqual(len(self.calls),3)
        self.assertTrue(all(t['name']=='prepare_page_image' for t in job['trace']))
        self.assertEqual(self.client.get(self.url).json()['messages'],previous)

    def test_citation_reconstructed_frozen_and_no_client_text(self):
        layer=text_layer(service,self.doc['id'],1);run=layer['runs'][0]
        request=dict(page=1,text_version=layer['version'],ranges=[dict(run_id=run['id'],start=0,end=4)])
        quote=self.client.post(self.url+'/citations',json={**request,'text':'forged'}).json()
        self.assertEqual(quote['text'],run['text'][:4])
        r=self.client.post(self.url+'/messages',json=dict(agent=self.agent,revision=0,action='message',mode_id='followup',question='解释',citations=[request]))
        self.assertEqual(self.finish(r.json()['id'])['status'],'completed')
        turn=self.client.get(self.url).json()['messages'][-1]
        self.assertEqual(turn['citations'][0]['text'],run['text'][:4])
        invalid={**request,'text_version':'old'}
        self.assertEqual(self.client.post(self.url+'/citations',json=invalid).status_code,409)

    def test_text_geometry_and_range_validation(self):
        for rotation in (0,90,180,270):
            doc=self.upload(rotation=rotation,columns=2,width=720)
            layer=text_layer(service,doc['id'],1)
            self.assertTrue(layer['runs'])
            for run in layer['runs']:
                self.assertEqual(len(run['boxes']),len(run['text']))
                for box in run['boxes']:
                    self.assertTrue(0<=box[0]<=box[2]<=1)
                    self.assertTrue(0<=box[1]<=box[3]<=1)
            r=layer['runs'][0]
            cite=CitationRequest(page=1,text_version=layer['version'],ranges=[dict(run_id=r['id'],start=1,end=5)])
            resolved=resolve_citation(service,doc['id'],cite)
            self.assertEqual(resolved['text'],r['text'][1:5])
        empty=self.upload(empty=True)
        self.assertEqual(text_layer(service,empty['id'],1)['runs'],[])

    def test_translated_pdf_quotes_use_revision_geometry_and_keep_old_versions(self):
        for columns, rotation, width in ((1, 0, 612), (2, 90, 720), (2, 270, 840)):
            doc = self.upload(columns=columns, rotation=rotation, width=width)
            for block in doc['pages'][0]['blocks']:
                block.update(translation='Translated text for selection.', target='English')
            directory = service.doc_dir(doc['id'])
            service.save_doc(directory, doc)
            report = service.build_layout(directory, doc, 'English')
            layer = text_layer(service, doc['id'], 1, report['revision'])
            run = layer['runs'][0]
            request = dict(page=1, view='translation', revision=report['revision'], text_version=layer['version'],
                           ranges=[dict(run_id=run['id'], start=0, end=min(10, len(run['text'])))])
            response = self.client.post(f"/api/documents/{doc['id']}/reading/citations", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            quote = response.json()
            self.assertEqual(quote['text'], run['text'][:10])
            self.assertEqual(quote['source']['revision'], report['revision'])
            self.assertNotIn('block_id', quote['source'])
            for box in quote['source']['rects']:
                self.assertTrue(0 <= box[0] <= box[2] <= 1)
                self.assertTrue(0 <= box[1] <= box[3] <= 1)
            original = text_layer(service, doc['id'], 1)
            self.assertNotEqual(original['version'], layer['version'])
            invalid = {**request, 'text_version': original['version']}
            url = f"/api/documents/{doc['id']}/reading/citations"
            self.assertEqual(self.client.post(url, json=invalid).status_code, 409)
            self.assertEqual(self.client.post(url, json={**request, 'revision': None}).status_code, 422)
            doc['pages'][0]['blocks'][0]['translation'] = 'A newer translation.'
            service.save_doc(directory, doc)
            service.build_layout(directory, doc, 'English')
            self.assertEqual(self.client.post(url, json=request).json(), quote)

    def test_paragraph_quote_offsets_are_unicode_and_content_versioned(self):
        from paper_analysis import digest
        block = self.doc['pages'][0]['blocks'][0]
        text = '译文😀片段\n与公式 x²。'
        block.update(translation=text, target='简体中文')
        service.save_doc(service.doc_dir(self.doc['id']), self.doc)
        request = dict(page=1, view='paragraph', block_id=block['id'], text_version=digest(text),
                       ranges=[dict(run_id=block['id'], start=2, end=7)])
        response = self.client.post(self.url+'/citations', json={**request, 'text': 'forged'})
        self.assertEqual(response.status_code, 200, response.text)
        quote = response.json()
        self.assertEqual(quote['text'], text[2:7])
        self.assertTrue(quote['source']['translated'])
        self.assertEqual(quote['source']['view'], 'paragraph')
        invalid = {**request, 'ranges': [dict(run_id=block['id'], start=2, end=100)]}
        self.assertEqual(self.client.post(self.url+'/citations', json=invalid).status_code, 422)
        block['translation'] = 'changed'
        service.save_doc(service.doc_dir(self.doc['id']), self.doc)
        self.assertEqual(self.client.post(self.url+'/citations', json=request).status_code, 409)

    def test_paragraph_quote_can_select_source_even_when_translation_exists(self):
        from paper_analysis import digest
        block = self.doc['pages'][0]['blocks'][0]
        source_text = block['text']
        translation = '译文😀片段'
        block.update(translation=translation, target='简体中文')
        service.save_doc(service.doc_dir(self.doc['id']), self.doc)
        source_request = dict(page=1, view='paragraph', block_id=block['id'], translated=False,
                              text_version=digest(source_text),
                              ranges=[dict(run_id=block['id'], start=0, end=min(4, len(source_text)))])
        response = self.client.post(self.url+'/citations', json=source_request)
        self.assertEqual(response.status_code, 200, response.text)
        quote = response.json()
        self.assertEqual(quote['text'], source_text[:4])
        self.assertFalse(quote['source']['translated'])
        translated_request = dict(source_request, translated=True, text_version=digest(translation),
                                  ranges=[dict(run_id=block['id'], start=0, end=min(4, len(translation)))])
        response = self.client.post(self.url+'/citations', json=translated_request)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['source']['translated'])
        self.assertEqual(response.json()['text'], translation[:4])

    def test_complete_cannot_hide_material_gaps_or_forged_sources(self):
        raw=dict(status='complete',sections=[dict(section=f,text='测试',kind='claim',source_ids=['a']) for f in self.snapshot['manifest']['modes']['quickread']['fields']],missing=[])
        self.assertRaises(HTTPException,workflow.validate,json.dumps(raw),self.snapshot,'quickread',{'a':{}},['缺少结论'])
        self.assertRaises(HTTPException,workflow.validate,json.dumps(raw),self.snapshot,'quickread',{},[])
        raw['sections'][0]['text']='长'*160
        self.assertEqual(workflow.validate(json.dumps(raw),self.snapshot,'quickread',{'a':{}},[])[0]['status'],'complete')

    def fulltext_document(self):
        with pymupdf.open() as pdf:
            for index in range(4):
                width = 612 if index % 2 == 0 else 720
                page = pdf.new_page(width=width, height=800 + index * 20)
                for col in range(1 + index % 2):
                    x = 45 + col * width / 2
                    page.insert_textbox((x, 50, x + width / (1 + index % 2) - 70, 600),
                        f'Body material on page {index + 1}.\n' + 'Methods and measured results. ' * 30,
                        fontsize=10)
                page.set_rotation(index * 90)
            pdf.new_page()  # A page without extractable text must remain an explicit gap.
            raw = pdf.tobytes()
        response = self.client.post('/api/documents',files={'file':('mixed.pdf',raw,'application/pdf')})
        self.assertEqual(response.status_code,200,response.text)
        self.doc = response.json()
        # Simulate an existing document containing a block longer than both read budgets.
        self.doc['pages'][0]['blocks'][0]['text'] += 'Long original paragraph. ' * 1500
        service.save_doc(service.doc_dir(self.doc['id']), self.doc)
        self.url = f"/api/documents/{self.doc['id']}/reading"
        self.session = self.client.post(self.url).json()
        return self.doc

    def test_fulltext_reads_every_character_across_layouts_and_long_blocks(self):
        doc=self.fulltext_document()
        before={name:(service.doc_dir(doc['id'])/name).read_bytes() for name in ('source.pdf','document.json')}
        job=self.finish(self.start());self.assertEqual(job['status'],'completed',job)
        for messages in self.calls:
            payload=self.payload(messages)
            self.assertEqual(''.join(r['text'] for m in payload['selected_material'] for r in m['rows']),
                ''.join(b['text'] for p in doc['pages'] for b in p['blocks']))
            self.assertEqual(len([m for m in messages if isinstance(m['content'],list)]),5)
            self.assertNotIn('fulltext_analysis',payload)
        data=self.client.get(self.url).json()
        for page in doc['pages']:
            for block in page['blocks']:
                self.assertEqual(data['coverage'][block['id']]['ranges'],[[0,len(block['text'])]])
        self.assertEqual(job['progress']['percent'],100)
        self.assertEqual(data['messages'][-1]['analysis_scope']['image_pages'],[1,2,3,4,5])
        self.assertEqual(data['messages'][-1]['structured']['missing'],[])
        self.assertEqual(before,{name:(service.doc_dir(doc['id'])/name).read_bytes() for name in before})

    def test_long_quickread_answers_work_with_pinned_150_character_workflows(self):
        path = service.doc_dir(self.doc['id']) / 'reading.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['workflow']['manifest']['modes']['quickread']['max_chars'] = 150
        data['workflow']['files']['quickread.md'] = '四项正文合计最多150个字符。'
        path.write_text(json.dumps(data),encoding='utf-8')
        raw = dict(status='incomplete',missing=[],sections=[
            dict(section=f,text='原文方法与实验证据。'*1600,kind='claim',source_ids=['p1-b1'])
            for f in self.snapshot['manifest']['modes']['quickread']['fields']])
        self.responses = [dict(role='assistant',content=json.dumps(raw),tool_calls=[])]
        job = self.finish(self.start())
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual(len(self.calls),2)
        self.assertIn('正文取消150字及其他总字数限制',self.calls[0][0]['content'])
        self.assertEqual(self.client.get(self.url).json()['messages'][-1]['structured']['sections'],raw['sections'])

    def test_fulltext_progress_is_live_and_cancellation_keeps_previous_answers(self):
        self.fulltext_document();self.finish(self.start())
        prior=self.client.get(self.url).json()['messages']
        original=self.respond
        async def slow(*args):
            await asyncio.sleep(1)
            return await original(*args)
        with patch.object(reading,'invoke',slow):
            job_id=self.start()
            for _ in range(200):
                job=self.client.get(self.url+'/jobs/'+job_id).json()
                if job.get('progress',{}).get('phase')=='inspecting':break
                time.sleep(.01)
            self.assertEqual(job['status'],'running')
            self.assertEqual(job['progress']['prepared_pages'],5)
            self.assertIsNone(job['progress']['percent'])
            self.assertEqual(self.client.delete(self.url+'/jobs/'+job_id).json()['status'],'cancelled')
        self.assertEqual(self.client.get(self.url).json()['messages'],prior)

    def test_fulltext_failure_never_saves_a_partial_answer_as_completed(self):
        self.fulltext_document()
        self.responses=[HTTPException(504,'整体分析请求超时')]
        job=self.finish(self.start())
        self.assertEqual(job['status'],'failed',job)
        self.assertNotEqual(job['progress']['percent'],100)
        self.assertEqual(self.client.get(self.url).json()['messages'],[])
        self.assertEqual(self.finish(self.start())['status'],'completed')

    def test_fulltext_exceeding_local_image_budget_keeps_previous_answer(self):
        self.assertEqual(self.finish(self.start())['status'], 'completed')
        previous = self.client.get(self.url).json()['messages']
        self.calls.clear()
        with patch.object(reading, 'MAX_IMAGE_BYTES', 1):
            job = self.finish(self.start())
        self.assertEqual(job['status'], 'failed')
        self.assertIn('本轮页面图像超过', job['error'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.get(self.url).json()['messages'], previous)

    def test_large_fulltext_is_provided_whole_without_independent_summaries(self):
        self.fulltext_document()
        self.doc['pages'][0]['blocks'][0]['text']='Original text. '*14001
        service.save_doc(service.doc_dir(self.doc['id']),self.doc)
        job=self.finish(self.start());self.assertEqual(job['status'],'completed',job)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.payload(self.calls[0])['selected_material'][0]['total_chars'],
            sum(len(b['text']) for p in self.doc['pages'] for b in p['blocks']))

    def test_four_visual_rounds_reuse_same_region_and_keep_shared_context(self):
        self.fulltext_document()
        original=self.respond
        rounds=[]
        async def respond(module,config,messages,tools):
            if tools:
                rounds.append(copy.deepcopy(messages))
                return baseline.call('render_page',page=1,bbox=[.1,.1,.4,.4],render_scale=8)
            return await original(module,config,messages,tools)
        with patch.object(reading,'invoke',respond):job=self.finish(self.start())
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual(len(rounds),4)
        turn=self.client.get(self.url).json()['messages'][-1]
        self.assertEqual(len(turn['visual_sources']),6)
        self.assertEqual(len([m for m in self.calls[-1] if isinstance(m['content'],list)]),6)
        self.assertEqual(self.payload(rounds[0]),self.payload(rounds[-1]))

    def test_scanned_document_is_provided_as_all_page_images(self):
        self.doc=self.upload(empty=True);self.url=f"/api/documents/{self.doc['id']}/reading"
        self.client.post(self.url)
        job=self.finish(self.start());self.assertEqual(job['status'],'completed',job)
        turn=self.client.get(self.url).json()['messages'][-1]
        self.assertEqual(turn['analysis_scope']['text_pages'],[])
        self.assertEqual(turn['analysis_scope']['image_pages'],[1,2])
        self.assertEqual(len(turn['visual_sources']),2)

    def test_previous_coverage_cannot_hide_unread_material_in_a_new_turn(self):
        doc = service.load_doc(self.doc['id'])
        doc['pages'][0]['blocks'][0]['text'] = 'Long current unit. ' * 1800
        service.save_doc(service.doc_dir(doc['id']),doc)
        path = service.doc_dir(doc['id']) / 'reading.json'
        session = json.loads(path.read_text(encoding='utf-8'))
        session['coverage'] = {b['id']:dict(total=len(b['text']),ranges=[[0,len(b['text'])]])
                               for p in doc['pages'] for b in p['blocks']}
        path.write_text(json.dumps(session),encoding='utf-8')
        response = self.client.post(self.url+'/messages',json=dict(
            agent=self.agent,revision=0,action='continue'))
        job = self.finish(response.json()['id'])
        self.assertEqual(job['status'],'completed',job)
        turn = self.client.get(self.url).json()['messages'][-1]
        self.assertTrue(any('必读原文尚未完整获取' in m for m in turn['structured']['missing']))

    def test_chapter_unit_can_span_pages_without_changing_block_ids(self):
        with pymupdf.open() as pdf:
            for text in ('1 Method','More method text','2 Results'):
                p=pdf.new_page();p.insert_text((60,60),text)
            pdf.set_toc([[1,'1 Method',1],[1,'2 Results',3]])
            raw=pdf.tobytes()
        doc=self.client.post('/api/documents',files={'file':('sections.pdf',raw,'application/pdf')}).json()
        structure=reading.build_structure(service,doc)
        unit=structure['units'][0]
        self.assertIn(doc['pages'][1]['blocks'][0]['id'],unit['block_ids'])
        self.assertEqual(unit['page'],1)

    def test_ligature_hyphen_and_multiple_page_quotes(self):
        font=next((p for p in [Path('C:/Windows/Fonts/times.ttf'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf')] if p.exists()),None)
        if font is None:self.skipTest('No test serif font available')
        with pymupdf.open() as pdf:
            for number in range(2):
                p=pdf.new_page(width=600+number*120,height=800)
                p.insert_text((50,70),'inter-\noperable ﬁ ligature',fontsize=12,fontname='quote-test',fontfile=str(font))
            raw=pdf.tobytes()
        doc=self.client.post('/api/documents',files={'file':('unicode.pdf',raw,'application/pdf')}).json()
        for page in (1,2):
            layer=text_layer(service,doc['id'],page)
            self.assertIn('ﬁ',''.join(r['text'] for r in layer['runs']))
            request=CitationRequest(page=page,text_version=layer['version'],ranges=[dict(run_id=r['id'],start=0,end=len(r['text'])) for r in layer['runs']])
            quote=resolve_citation(service,doc['id'],request)
            self.assertEqual(quote['text'],'\n'.join(r['text'] for r in layer['runs']))
            self.assertIn('inter-\noperable ﬁ',quote['text'].replace('\u00ad','-').replace('\u00a0',' '))
            self.assertEqual(quote['source']['page'],page)


if __name__=='__main__':unittest.main()
