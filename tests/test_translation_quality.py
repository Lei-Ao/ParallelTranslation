"""Regression: direct translation must not execute retired model workflows."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import app as service
from test_app import sample_pdf
from translation_terms import contains, active_entries, read_csv, csv_text


class DirectTranslationTests(unittest.TestCase):
    def setUp(self):
        from vision_fixture import install
        install(self)
        self.temp = tempfile.TemporaryDirectory()
        self.patcher = patch.object(service, 'DATA', Path(self.temp.name)); self.patcher.start()
        self.client = TestClient(service.app)
        self.client.headers['X-Local-Token'] = self.client.get('/api/session').json()['token']
        self.doc = self.client.post('/api/documents', files={'file': ('paper.pdf', sample_pdf(), 'application/pdf')}).json()
        self.base = f"/api/documents/{self.doc['id']}"
        self.ids = [b['id'] for b in service.all_blocks(self.doc)]
        self.agent = dict(model='test',base_url='https://example.com/v1',api_key='test-secret')

    def tearDown(self):
        self.client.close(); self.patcher.stop(); self.temp.cleanup()

    def test_single_uses_one_call_and_manual_glossary(self):
        r=self.client.post(self.base+'/quality/glossary/import',json=dict(revision=0,text='source,target,tgt_lng\nagent,智能体,zh-CN'))
        self.assertEqual(r.status_code,200,r.text)
        async def model(config,prompt,text='',target='',system_prompt=''):
            self.assertIn('agent = 智能体',system_prompt)
            self.assertIn('用户要求',system_prompt)
            self.assertNotIn('审校员',system_prompt)
            return '智能体使用上下文翻译文档。'
        with patch.object(service,'invoke_agent',side_effect=model) as call:
            r=self.client.post(self.base+f'/blocks/{self.ids[1]}/translate',json=dict(agent=self.agent,instructions='用户要求'))
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(call.call_count,1)
        self.assertEqual(r.json()['translation'],'智能体使用上下文翻译文档。')

    def test_batch_uses_one_call_and_keeps_source_ids(self):
        async def model(config,prompt,text='',target='',system_prompt=''):
            payload=json.loads(prompt.split('参考资料与本次待翻译段落：\n')[1])
            return json.dumps({'blocks':[dict(id=b['id'],translation='直接译文') for b in payload['current_blocks']]})
        with patch.object(service,'invoke_agent',side_effect=model) as call:
            r=self.client.post(self.base+'/blocks/translate-batch',json=dict(agent=self.agent,block_ids=self.ids))
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(call.call_count,1)
        self.assertEqual([b['id'] for b in r.json()['blocks']],self.ids)
        self.assertEqual([b['text'] for b in service.all_blocks(service.snapshot_doc(self.doc['id']))],
                         [b['text'] for b in service.all_blocks(self.doc)])

    def test_retired_endpoints_cannot_call_model(self):
        with patch.object(service,'invoke_agent') as call:
            for suffix in ['/quality/jobs','/quality/jobs/old/resume','/quality/jobs/old/pause']:
                response=self.client.post(self.base+suffix,json=dict(agent=self.agent,block_ids=self.ids,action='review'))
                self.assertEqual(response.status_code,410,response.text)
            call.assert_not_called()
        self.assertFalse(self.client.get('/api/session').json()['quality_translation'])

    def test_history_and_auto_entries_stay_on_disk_but_are_not_reused(self):
        job=dict(id='old',action='translate',status='running',stage='旧审校',target='简体中文',created='2026-11-08T00:00:00Z',
                 block_ids=self.ids,completed_ids=[],gaps=[],metrics={},error='',glossary_revision=0,
                 source_signature='old',records={},drafts={})
        data=dict(version=1,revision=0,entries=[dict(source='agent',target='错误自动词',origin='auto',tgt_lng='简体中文')],jobs={'old':job},cache={})
        path=service.doc_dir(self.doc['id'])/'translation-quality.json'
        path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');before=path.read_bytes()
        response=self.client.get(self.base+'/quality').json()
        self.assertEqual(response['entries'],[])
        self.assertEqual(response['jobs'][0]['status'],'interrupted')
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(service.quality_service['manual_prompt'](self.doc['id'],'简体中文','agent'),'')

    def test_historical_accept_still_protects_manual_changes(self):
        original=service.all_blocks(self.doc)[0]
        data=dict(version=1,revision=0,entries=[],cache={},jobs={'old':dict(id='old',status='completed',target='简体中文',
            records={original['id']:dict(original=original,candidate='旧建议',status='passed')})})
        (service.doc_dir(self.doc['id'])/'translation-quality.json').write_text(json.dumps(data),encoding='utf-8')
        self.client.patch(self.base+'/blocks/'+original['id'],json=dict(translation='最新人工编辑'))
        response=self.client.post(self.base+'/quality/jobs/old/accept',json=dict(block_id=original['id']))
        self.assertEqual(response.status_code,409)

    def test_manual_csv_and_language_boundaries(self):
        entries=read_csv('source,target,tgt_lng\nagent,智能体,zh-CN','简体中文')
        self.assertEqual(read_csv(csv_text(entries),'简体中文'),entries)
        self.assertEqual(len(active_entries(entries,'简体中文','An agent.')),1)
        self.assertFalse(contains('agency','agent'))


if __name__=='__main__': unittest.main()
