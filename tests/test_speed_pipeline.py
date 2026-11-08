import asyncio
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import pymupdf
from fastapi.testclient import TestClient
import app as service
import pdf_layout
import performance
from test_app import sample_pdf


class SpeedPipelineTests(unittest.TestCase):
    def setUp(self):
        from vision_fixture import install
        install(self)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(service, 'DATA', self.root),
                        patch.object(service, 'EXPORT_DIR', self.root / 'exports'),
                        patch.object(performance, 'LOG', self.root / 'metrics.jsonl')]
        for item in self.patches:
            item.start()
        self.client = TestClient(service.app)
        self.client.headers['X-Local-Token'] = self.client.get('/api/session').json()['token']
        self.doc = self.client.post('/api/documents', files={'file': ('sample.pdf', sample_pdf(), 'application/pdf')}).json()
        self.directory = self.root / self.doc['id']
        self.doc = service.snapshot_doc(self.doc['id'])
        for block in service.all_blocks(self.doc):
            block.update(translation='测试正文与上下文。', target='简体中文')
        service.save_doc(self.directory, self.doc)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_prewarm_reuses_completed_pages_and_rejects_invalid_pages(self):
        route = f'/api/documents/{self.doc["id"]}/layout'
        self.assertEqual(self.client.post(route+'/prewarm', json={'pages':[999]}).status_code, 400)
        response = self.client.post(route+'/prewarm', json={'pages':[1,2]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['pages'], [1])
        self.assertFalse(list((self.directory/'layout').glob('*.pdf')))
        with patch.object(pdf_layout, 'typeset', wraps=pdf_layout.typeset) as render:
            final = self.client.post(route, json={}).json()
        self.assertEqual(render.call_count, 1)  # Only the blank second page.
        self.assertIn('saved_pdf', final)

    def test_late_old_prewarm_cannot_replace_edited_page_cache(self):
        old = copy.deepcopy(self.doc)
        self.doc['pages'][0]['blocks'][0]['translation'] = '更新后的标题'
        pdf_layout.prewarm_layout(self.directory, self.doc, '简体中文', [1])
        pdf_layout.prewarm_layout(self.directory, old, '简体中文', [1])
        report = pdf_layout.build_layout(self.directory, self.doc, '简体中文')
        with pymupdf.open(self.directory/'layout'/(report['revision']+'.pdf')) as pdf:
            self.assertIn('更新后的标题', pdf[0].get_text().replace(' ', ''))

    def test_failed_prewarm_falls_back_to_final_layout(self):
        route = f'/api/documents/{self.doc["id"]}/layout'
        with patch.object(service, 'prewarm_layout', side_effect=RuntimeError('failure')):
            self.assertEqual(self.client.post(route+'/prewarm', json={'pages':[1]}).status_code, 422)
        self.assertEqual(self.client.post(route, json={}).status_code, 200)
        self.assertEqual(service.snapshot_doc(self.doc['id'])['pages'], self.doc['pages'])

    def test_pending_and_wrong_language_pages_are_not_warmed(self):
        self.doc['pages'][0]['blocks'][0]['target'] = 'English'
        self.assertEqual(pdf_layout.prewarm_layout(self.directory, self.doc, '简体中文', [1])['pages'], [])

    def test_new_batch_limit_and_concurrent_manual_edit_protection(self):
        template = self.doc['pages'][0]['blocks'][1]
        self.doc['pages'][0]['blocks'] = [{**copy.deepcopy(template), 'id':str(i)} for i in range(12)]
        service.save_doc(self.directory, self.doc)
        agent = {'base_url':'https://example.com/v1','model':'test','api_key':'SECRET_MARKER'}
        real_client = httpx.AsyncClient
        def handler(request):
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'blocks':[
                {'id':str(i),'translation':'有效译文'} for i in range(12)]})},'finish_reason':'stop'}]})
        with patch.object(service.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler),**kw)):
            response=self.client.post(f'/api/documents/{self.doc["id"]}/blocks/translate-batch',json={'agent':agent,'block_ids':[str(i) for i in range(12)]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(response.json()['blocks']),12)
        async def edit_during_model(*args,**kwargs):
            latest=service.load_doc(self.doc['id'])
            latest['pages'][0]['blocks'][0].update(translation='人工修订',edited=True)
            service.save_doc(self.directory,latest)
            return '迟到的译文'
        with patch.object(service,'invoke_agent',side_effect=edit_during_model):
            response=self.client.post(f'/api/documents/{self.doc["id"]}/blocks/0/translate',json={'agent':agent})
        self.assertEqual(response.status_code,409)
        self.assertEqual(service.load_doc(self.doc['id'])['pages'][0]['blocks'][0]['translation'],'人工修订')
        metrics=(self.root/'metrics.jsonl').read_text()
        self.assertNotIn('SECRET_MARKER',metrics)
        self.assertNotIn('有效译文',metrics)
        rows=[json.loads(line) for line in metrics.splitlines()]
        self.assertTrue(any(row.get('model_requests')==1 for row in rows))

    def test_layout_does_not_export_a_snapshot_changed_during_render(self):
        original=service.build_layout
        def render(*args):
            result=original(*args)
            latest=service.load_doc(self.doc['id'])
            latest['pages'][0]['blocks'][0]['translation']='更新'
            service.save_doc(self.directory,latest)
            return result
        with patch.object(service,'build_layout',side_effect=render):
            report=self.client.post(f'/api/documents/{self.doc["id"]}/layout',json={}).json()
        self.assertTrue(report['stale'])
        self.assertNotIn('saved_pdf',report)

    def test_serial_and_prewarmed_outputs_match_for_rotations_columns_and_sizes(self):
        for rotation in (0,90,180,270):
            with self.subTest(rotation=rotation), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                with pymupdf.open() as pdf:
                    for width,height,columns in ((595,842,1),(720,540,2)):
                        page=pdf.new_page(width=width,height=height)
                        for column in range(columns):
                            x=40+column*width/2
                            page.insert_text((x,65),'An example title',fontsize=16)
                            page.insert_textbox((x,100,x+220,240),'This paragraph describes a method and its results. '*4,fontsize=10)
                            page.insert_text((x,300),'E = mc2',fontsize=11)
                            page.draw_rect((x,330,x+100,380),color=(0,0,1))
                        page.set_rotation(rotation)
                    raw=pdf.tobytes()
                _,doc=service.extract_document(raw,'geometry.pdf')
                for block in service.all_blocks(doc):
                    block.update(translation='研究方法与实验结果。',target='简体中文')
                a,b=root/'serial',root/'pipeline';a.mkdir();b.mkdir()
                (a/'source.pdf').write_bytes(raw);shutil.copy2(a/'source.pdf',b/'source.pdf')
                serial=pdf_layout.build_layout(a,doc,'简体中文')
                pdf_layout.prewarm_layout(b,doc,'简体中文',[1,2])
                with patch.object(pdf_layout,'typeset',side_effect=AssertionError('unexpected rebuild')):
                    pipelined=pdf_layout.build_layout(b,doc,'简体中文')
                self.assertEqual(serial,pipelined)
                with pymupdf.open(a/'layout'/(serial['revision']+'.pdf')) as left, pymupdf.open(b/'layout'/(pipelined['revision']+'.pdf')) as right:
                    for p,q in zip(left,right):
                        self.assertEqual(p.get_pixmap().samples,q.get_pixmap().samples)
                        self.assertEqual(p.get_text(),q.get_text())
