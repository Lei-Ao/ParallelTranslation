import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
import pymupdf
from fastapi.testclient import TestClient
from fastapi import HTTPException
import app as service
import paper_reading as reading
import reading_model
from test_paper_analysis import sample


def call(name, **args):
    return dict(role='assistant', content='', tool_calls=[dict(id='test-call', type='function',
                function=dict(name=name, arguments=json.dumps(args)))])


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(service, 'DATA', Path(self.temp.name)); self.data_patch.start()
        self.client = TestClient(service.app); self.client.__enter__()
        self.client.headers['X-Local-Token'] = self.client.get('/api/session').json()['token']
        self.agent = dict(base_url='https://example.test/v1', model='test-model', api_key='secret-not-in-reading')
        self.doc = self.upload()
        self.url = f"/api/documents/{self.doc['id']}/reading"
        self.session = self.client.post(self.url).json()
        # These tests also exercise compatibility with pre-workflow pinned sessions.
        saved_path=service.doc_dir(self.doc['id'])/'reading.json'
        legacy=json.loads(saved_path.read_text(encoding='utf-8'));legacy.pop('workflow',None)
        saved_path.write_text(json.dumps(legacy),encoding='utf-8')
        self.calls = []
        self.responses = []
        self.mock = patch.object(reading, 'invoke', self.respond); self.mock.start()

    def tearDown(self):
        self.client.__exit__(None, None, None); self.mock.stop(); self.data_patch.stop(); self.temp.cleanup()

    def upload(self, **kwargs):
        result = self.client.post('/api/documents', files={'file': ('paper.pdf', sample(**kwargs), 'application/pdf')})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    async def respond(self, module, config, messages, tools):
        self.calls.append(copy.deepcopy(messages))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        if not any(m['role'] == 'tool' for m in messages):
            return call('read_material', page=1)
        return dict(role='assistant', content='方法解释。[[p1-b1]]\n\n请选择阅读路线。', tool_calls=[])

    def start(self, **kwargs):
        revision = self.client.get(self.url).json()['revision']
        result = self.client.post(self.url+'/messages', json=dict(agent=self.agent, revision=revision, action='start', **kwargs))
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['id']

    def finish(self, job):
        for _ in range(200):
            result = self.client.get(self.url+'/jobs/'+job).json()
            if result['status'] not in ('queued', 'running'):
                return result
            time.sleep(.01)
        self.fail('job did not finish')

    def test_skill_is_pinned_and_no_translation_writes(self):
        p=service.doc_dir(self.doc['id'])
        before={f:hashlib.sha256((p/f).read_bytes()).hexdigest() for f in ('source.pdf','document.json')}
        job=self.finish(self.start())
        self.assertEqual(job['status'],'completed',job)
        data=self.client.get(self.url).json()
        self.assertIn('一次只推进一小块',self.calls[0][0]['content'])
        self.assertIn('domain_index',self.calls[0][-1]['content'])
        self.assertTrue(data['coverage'])
        self.assertEqual(data['progress'],{})
        self.assertNotIn('skill',data)
        self.assertNotIn(self.agent['api_key'],(p/'reading.json').read_text(encoding='utf-8'))
        self.assertEqual(before,{f:hashlib.sha256((p/f).read_bytes()).hexdigest() for f in before})
        saved=json.loads((p/'reading.json').read_text(encoding='utf-8'))
        saved['skill']['files']['SKILL.md']='PINNED OLD SKILL'
        (p/'reading.json').write_text(json.dumps(saved),encoding='utf-8')
        self.finish(self.start())
        self.assertIn('PINNED OLD SKILL',self.calls[-2][0]['content'])

    def test_revision_progress_and_invalid_units(self):
        r=self.client.patch(self.url,json={'revision':0,'status':'completed'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['progress'][self.session['unit_id']],'completed')
        self.assertEqual(self.client.patch(self.url,json={'revision':0,'route':'method'}).status_code,409)
        self.assertEqual(self.client.patch(self.url,json={'revision':1,'unit_id':'fake'}).status_code,422)

    def test_long_paragraph_has_lossless_cursors(self):
        doc=copy.deepcopy(self.doc);doc['pages'][0]['blocks'][0]['text']='abcdef'*9000
        total=''.join(b['text'] for b in doc['pages'][0]['blocks'])
        out=[];cursor=0
        while True:
            value=reading.read_material(doc,self.session['structure'],dict(page=1,cursor=cursor))
            out.extend(r['text'] for r in value['rows'])
            cursor=value['next_cursor']
            if cursor is None:break
        self.assertEqual(''.join(out),total)
        self.assertRaises(HTTPException,reading.read_material,doc,self.session['structure'],dict(page=1,cursor=-1))

    def test_forged_citation_rejected_old_answer_kept(self):
        self.finish(self.start())
        self.responses=[dict(role='assistant',content='[[foreign-block]]',tool_calls=[])]
        job=self.finish(self.start())
        self.assertEqual(job['status'],'failed')
        self.assertEqual(len(self.client.get(self.url).json()['messages']),1)

    def test_tool_allowlist_and_resource_loading(self):
        self.responses=[call('read_skill',path='../../app.py'),call('read_skill',path='references/question-checklist.md'),call('read_material',page=1)]
        job=self.finish(self.start())
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual(job['trace'][0]['status'],'error')
        self.assertEqual(job['trace'][1]['status'],'ok')
        self.assertNotIn('Local PDF reader',json.dumps(self.calls))

    def test_tool_limit_persists_work(self):
        self.responses=[call('read_material',page=1) for _ in range(15)]
        job=self.finish(self.start())
        self.assertEqual(job['status'],'limited')
        self.assertEqual(len(job['trace']),12)
        self.assertTrue(self.client.get(self.url).json()['coverage'])
        self.assertTrue(self.client.get(self.url).json()['tool_progress'])

    def test_visual_input_and_geometry_all_rotations(self):
        for angle in (0,90,180,270):
            doc=self.upload(rotation=angle,columns=2,width=720)
            png=reading.render_region(service,doc,reading.region(doc,1,[.1,.2,.6,.8]))
            pix=pymupdf.Pixmap(png)
            w,h=doc['pages'][0]['width'],doc['pages'][0]['height']
            self.assertAlmostEqual(pix.width/pix.height,(w*.5)/(h*.6),delta=.02)
        self.responses=[call('render_page',page=1,bbox=[.1,.1,.8,.8]),dict(role='assistant',content='区域已查看。',tool_calls=[])]
        job=self.finish(self.start())
        self.assertEqual(job['status'],'completed',job)
        image_messages=[m for m in self.calls[-1] if isinstance(m['content'],list)]
        self.assertTrue(image_messages[-1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
        data=self.client.get(self.url).json(); source=data['messages'][-1]['images'][0]
        self.assertEqual(self.client.get(self.url+'/images/'+source).status_code,200)

    def test_scanned_pages_and_invalid_regions(self):
        doc=self.upload(empty=True)
        structure=reading.build_structure(service,doc)
        self.assertEqual(len(structure['units']),2)
        value=reading.read_material(doc,structure,dict(page=1))
        self.assertEqual(value['rows'],[]);self.assertIn('视觉',value['note'])
        for bbox in ([.5,0,.1,1],[-1,0,1,1],[0,0,float('nan'),1]):
            self.assertRaises(HTTPException,reading.region,doc,1,bbox)

    def test_notes_versioned_and_template_required(self):
        self.finish(self.start())
        for _ in range(2):
            self.responses=[call('read_skill',path='references/summary-template.md'),call('read_skill',path='references/question-checklist.md'),
                            call('save_summary',markdown='# 阅读小结\n方法 [[p1-b1]]'),dict(role='assistant',content='小结已保存。',tool_calls=[])]
            rev=self.client.get(self.url).json()['revision']
            result=self.client.post(self.url+'/messages',json=dict(agent=self.agent,revision=rev,action='summary'))
            self.assertEqual(self.finish(result.json()['id'])['status'],'completed')
        data=self.client.get(self.url).json();self.assertEqual(len(data['summaries']),2)
        self.assertNotEqual(data['summaries'][0]['id'],data['summaries'][1]['id'])
        for note in data['summaries']:
            r=self.client.get(self.url+'/summaries/'+note['id'])
            self.assertIn('阅读覆盖说明',r.text)
            self.assertNotIn('[[p1-b1]]',r.text)

    def test_clear_rejects_late_response(self):
        async def slow(*args):
            try:await asyncio.sleep(.3)
            except asyncio.CancelledError:pass
            return dict(role='assistant',content='late response',tool_calls=[])
        with patch.object(reading,'invoke',slow):
            self.start();self.client.delete(self.url)
            self.client.post(self.url)
            time.sleep(.4)
        data=self.client.get(self.url).json()
        self.assertEqual(data['messages'],[])
        self.assertTrue(list(service.doc_dir(self.doc['id']).glob('reading-archive-*.json')))

    def test_cancel_and_restart(self):
        async def slow(*args):
            await asyncio.sleep(1)
            return dict(role='assistant',content='late response',tool_calls=[])
        with patch.object(reading,'invoke',slow):
            job=self.start();result=self.client.delete(self.url+'/jobs/'+job)
            self.assertEqual(result.json()['status'],'cancelled')
        p=service.doc_dir(self.doc['id'])/'reading.json';data=json.loads(p.read_text(encoding='utf-8'))
        data['jobs'][job]['status']='running';p.write_text(json.dumps(data),encoding='utf-8')
        time.sleep(.03)
        self.assertEqual(self.client.get(self.url+'/jobs/'+job).json()['status'],'interrupted')

    def test_test_does_not_accept_plain_text_as_capability(self):
        self.responses=[dict(role='assistant',content='我支持看图',tool_calls=[])]
        rev=self.client.get(self.url).json()['revision']
        r=self.client.post(self.url+'/messages',json=dict(agent=self.agent,revision=rev,action='test'))
        self.assertEqual(self.finish(r.json()['id'])['status'],'failed')

    def test_document_and_local_token_isolation(self):
        other=self.upload()
        r=self.client.post(self.url+'/messages',json=dict(agent=self.agent,revision=0,action='visual',page=999))
        self.assertEqual(r.status_code,422)
        self.assertEqual(self.client.get(f"/api/documents/{other['id']}/reading").json(),{'session':None})
        self.client.headers.pop('X-Local-Token')
        self.assertEqual(self.client.post(self.url).status_code,403)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_custom_template_does_not_expand_placeholders_in_quoted_paper(self):
        variables = {'text': 'a literal {{prompt}} token', 'prompt': 'agent instructions'}
        self.assertEqual(reading_model.expand('paper: {{text}}', variables),
                         'paper: a literal {{prompt}} token')

    async def test_native_tools_images_and_custom_structured_template(self):
        messages=[dict(role='user',content=[dict(type='text',text='看图'),dict(type='image_url',image_url={'url':'data:image/png;base64,a'})])]
        seen=[]
        async def handler(request):
            seen.append(json.loads(request.content))
            if len(seen)==1:return httpx.Response(200,json={'choices':[{'message':{'content':None,'tool_calls':[{'id':'a','function':{'name':'get_structure','arguments':'{}'}}]}}]})
            return httpx.Response(200,json={'output':{'content':'回答','tool_calls':[]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(service,'agent_client',return_value=client):
                config=service.AgentConfig(model='test',base_url='https://example.test/v1')
                r=await reading_model.invoke(service,config,messages,reading.TOOLS)
                self.assertEqual(r['tool_calls'][0]['function']['name'],'get_structure')
                config.mode='custom';config.template='{"messages":"{{messages}}","images":"{{images}}","tools":"{{tools}}"}'
                await reading_model.invoke(service,config,messages,reading.TOOLS)
                self.assertIsInstance(seen[-1]['images'],list)
                self.assertIsInstance(seen[-1]['messages'],list)
                config.template='{"input":"{{prompt}}"}'
                with self.assertRaises(HTTPException):await reading_model.invoke(service,config,messages,reading.TOOLS)


if __name__=='__main__':unittest.main()
