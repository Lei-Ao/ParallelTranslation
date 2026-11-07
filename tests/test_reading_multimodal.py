import asyncio
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
import pymupdf
from fastapi import HTTPException

import app as service
import paper_reading as reading
import reading_context
import reading_model
import reading_workflow
from paper_analysis import digest
from reading_text import CitationRequest, resolve_citation
import test_reading_workflow as base
import test_paper_reading as baseline


class MultimodalTests(unittest.TestCase):
    setUp = base.WorkflowTests.setUp
    tearDown = base.WorkflowTests.tearDown
    upload = base.WorkflowTests.upload
    start = base.WorkflowTests.start
    finish = base.WorkflowTests.finish
    respond = base.WorkflowTests.respond
    payload = base.WorkflowTests.payload

    def quoted_document(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            for y, text in [(60,'1 Introduction'),(120,'The error term is introduced before the claim.'),
                            (180,'It follows from Eq. (7) that the error converges.'),
                            (240,'The next paragraph explains the consequence.')]:
                page.insert_text((50,y),text)
            page=pdf.new_page();page.insert_text((50,60),'2 Definition')
            page.insert_text((50,140),'Error dynamics e = x - r. (7)')
            pdf.set_toc([[1,'1 Introduction',1],[1,'2 Definition',2]])
            raw=pdf.tobytes()
        result=self.client.post('/api/documents',files={'file':('context.pdf',raw,'application/pdf')})
        self.doc=result.json();self.url=f"/api/documents/{self.doc['id']}/reading"
        self.session=self.client.post(self.url).json()
        block=next(b for b in self.doc['pages'][0]['blocks'] if b['text'].startswith('It follows'))
        self.quote=dict(page=1,view='paragraph',block_id=block['id'],text_version=digest(block['text']),
            ranges=[dict(run_id=block['id'],start=0,end=2)])
        return block

    def ask(self, question='解释引用中的公式和代词指向', quotes=None):
        data=self.client.get(self.url).json()
        result=self.client.post(self.url+'/messages',json=dict(agent=self.agent,revision=data['revision'],
            action='message',mode_id='followup',question=question,citations=quotes or [self.quote]))
        self.assertEqual(result.status_code,200,result.text)
        return self.finish(result.json()['id'])

    def test_citation_reads_actual_neighbors_and_searches_cross_page_definition(self):
        block=self.quoted_document()
        # The browsing unit points elsewhere; quotation context must be anchored to the quote.
        data=self.client.get(self.url).json()
        self.client.patch(self.url,json=dict(revision=data['revision'],unit_id=data['structure']['units'][-1]['id']))
        original=self.respond
        once=False
        async def model(module,config,messages,tools):
            nonlocal once
            if tools and not once:
                once=True
                initial=self.payload(messages)
                text=' '.join(r['text'] for m in initial['selected_material'] for r in m['rows'])
                self.assertIn('introduced before',text)
                self.assertIn('It follows',text)
                self.assertIn('next paragraph',text)
                self.assertEqual(initial['citations'][0]['text'],'It')
                self.assertTrue(any(isinstance(m['content'],list) for m in messages))
                return baseline.call('search_material',query='Eq. (7) error dynamics')
            return await original(module,config,messages,tools)
        with patch.object(reading, 'invoke', model), patch.object(
                reading, 'resolve_citation', wraps=resolve_citation) as resolve:
            job = self.ask()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(job['status'],'completed',job)
        turn=self.client.get(self.url).json()['messages'][-1]
        self.assertEqual(turn['mode_id'],'citation')
        remote=self.doc['pages'][1]['blocks'][-1]['id']
        self.assertIn(remote,turn['context_sources'])
        tools=[m for m in self.calls[-1] if m['role']=='tool']
        self.assertIn('e = x - r',json.dumps(tools))
        self.assertEqual(turn['context_map'][0]['block_ids'],[block['id']])

    def test_multiple_cross_page_quotes_merge_complete_context_without_duplicates(self):
        self.quoted_document()
        second=self.doc['pages'][1]['blocks'][-1]
        request=dict(page=2,view='paragraph',block_id=second['id'],text_version=digest(second['text']),
            ranges=[dict(run_id=second['id'],start=0,end=5)])
        job=self.ask(quotes=[self.quote,request]);self.assertEqual(job['status'],'completed',job)
        payload=self.payload(self.calls[0]);ids=[r['id'] for m in payload['selected_material'] for r in m['rows']]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(len(payload['context_map']),2)
        self.assertIn(second['id'],ids)

    def test_citation_on_preworkflow_session_uses_current_skill(self):
        self.quoted_document()
        path=service.doc_dir(self.doc['id'])/'reading.json'
        session=json.loads(path.read_text(encoding='utf-8'));session.pop('workflow')
        path.write_text(json.dumps(session),encoding='utf-8')
        job=self.ask();self.assertEqual(job['status'],'completed',job)
        self.assertIn('引用上下文问答',self.calls[0][0]['content'])

    def test_visual_region_high_resolution_matches_display_coordinates_all_rotations(self):
        for angle in (0,90,180,270):
            with pymupdf.open() as pdf:
                page=pdf.new_page(width=600,height=800)
                box=pymupdf.Rect(50,60,170,140)
                page.draw_rect(box,color=(1,0,0),fill=(1,0,0));page.insert_text((80,110),'x = 7',fontsize=18)
                page.set_rotation(angle);display=box*page.rotation_matrix
                bbox=[display.x0/page.rect.width,display.y0/page.rect.height,display.x1/page.rect.width,display.y1/page.rect.height]
                raw=pdf.tobytes()
            doc=self.client.post('/api/documents',files={'file':('geometry.pdf',raw,'application/pdf')}).json()
            location=reading.region(doc,1,bbox)
            low=pymupdf.Pixmap(reading.render_region(service,doc,{**location,'render_scale':2}))
            high=pymupdf.Pixmap(reading.render_region(service,doc,{**location,'render_scale':8}))
            self.assertAlmostEqual(high.width/low.width,4,delta=.08)
            self.assertAlmostEqual(high.height/low.height,4,delta=.08)
            self.assertEqual(high.pixel(10,10),(255,0,0))
            self.assertLessEqual(high.width*high.height,4_000_000)
            self.assertRaises(HTTPException,reading.render_region,service,doc,{**location,'render_scale':9})

    def test_visual_pixel_budget_on_large_pages(self):
        with pymupdf.open() as pdf:
            pdf.new_page(width=4000,height=5000);raw=pdf.tobytes()
        doc=self.client.post('/api/documents',files={'file':('large.pdf',raw,'application/pdf')}).json()
        pix=pymupdf.Pixmap(reading.render_region(service,doc,dict(page=1,bbox=[0,0,1,1],render_scale=8)))
        self.assertLessEqual(pix.width*pix.height,4_000_000)

    def test_quotation_context_includes_same_column_neighbors_on_rotated_pages(self):
        for angle in (0,90,180,270):
            with pymupdf.open() as pdf:
                page=pdf.new_page(width=800,height=1000)
                for y,label in ((100,'prior'),(200,'focus'),(300,'after')):
                    page.insert_text((50,y),'Left '+label+' context paragraph for the selected claim.')
                    page.insert_text((440,y),'Right '+label+' paragraph belonging to another column.')
                page.set_rotation(angle);raw=pdf.tobytes()
            doc=self.client.post('/api/documents',files={'file':('columns.pdf',raw,'application/pdf')}).json()
            structure=reading.build_structure(service,doc)
            own=next(b for b in doc['pages'][0]['blocks'] if b['text'].startswith('Left focus'))
            quote=dict(id='q',text='focus',block_ids=[own['id']],source=dict(page=1,block_id=own['id']))
            material,_,_=reading_context.surrounding(doc,structure,[quote],service=service)
            text=' '.join(r['text'] for r in material)
            self.assertIn('Left prior',text)
            self.assertIn('Left after',text)

    def test_translated_region_renders_selected_revision(self):
        self.quoted_document()
        for block in self.doc['pages'][0]['blocks']:
            block.update(translation='Translated statement about control.',target='English')
        directory=service.doc_dir(self.doc['id']);service.save_doc(directory,self.doc)
        report=service.build_layout(directory,self.doc,'English')
        from reading_text import text_layer
        layer=text_layer(service,self.doc['id'],1,report['revision']);run=layer['runs'][0]
        request=CitationRequest(page=1,view='translation',revision=report['revision'],text_version=layer['version'],
            ranges=[dict(run_id=run['id'],start=0,end=5)])
        quote=resolve_citation(service,self.doc['id'],request)
        selected,contexts,visual=reading_context.surrounding(self.doc,self.session['structure'],[quote],'解释公式')
        self.assertTrue(selected)
        self.assertTrue(any(v.get('revision')==report['revision'] for v in visual))
        source=dict(page=1,bbox=[0,0,1,1],view='translation',revision=report['revision'],render_scale=2)
        translated=reading.render_region(service,self.doc,source)
        original=reading.render_region(service,self.doc,{**source,'view':'source'})
        self.assertNotEqual(translated,original)

    def test_ambiguous_translation_quote_preserves_selection_and_adds_both_page_views(self):
        self.quoted_document()
        for block in self.doc['pages'][0]['blocks']:
            block.update(translation='Translated statement about control.', target='English')
        directory = service.doc_dir(self.doc['id'])
        service.save_doc(directory, self.doc)
        report = service.build_layout(directory, self.doc, 'English')
        report_path = directory / 'layout' / (report['revision'] + '.json')
        mapping = json.loads(report_path.read_text(encoding='utf-8'))
        mapping['placements'] = {b['id']: [0, 0, 1, 1] for b in self.doc['pages'][0]['blocks']}
        report_path.write_text(json.dumps(mapping), encoding='utf-8')
        from reading_text import text_layer
        layer = text_layer(service, self.doc['id'], 1, report['revision'])
        run = layer['runs'][0]
        request = dict(page=1, view='translation', revision=report['revision'], text_version=layer['version'],
            ranges=[dict(run_id=run['id'], start=0, end=5)])
        quote = resolve_citation(service, self.doc['id'], CitationRequest(**request))
        self.assertEqual(quote['text'], run['text'][:5])
        self.assertTrue(quote['context_ambiguous'])
        job = self.ask('解释这句话', [request])
        self.assertEqual(job['status'], 'completed', job)
        session = self.client.get(self.url).json()
        turn = session['messages'][-1]
        self.assertEqual(turn['context_map'][0]['mapping'], 'page_context')
        self.assertIn('对应关系需核对', turn['context_map'][0]['note'])
        views = {session['sources'][sid].get('view', 'source') for sid in turn['visual_sources']}
        self.assertEqual(views, {'source', 'translation'})
        note = self.client.post(self.url + '/notes', json={'message_id': turn['id']}).json()
        self.client.request('DELETE', self.url + '/messages', json={'revision': session['revision']})
        exported = self.client.get(self.url + '/notes/' + note['id'] + '/download')
        self.assertIn(quote['text'], exported.text)
        self.assertIn('引用定位需核对', exported.text)
        restored = self.client.get(self.url + '/notes/' + note['id']).json()
        self.assertTrue(restored['turn']['citations'][0]['context_ambiguous'])

    def test_specific_issue_validation_rejects_fabricated_sources_and_local_gaps(self):
        self.quoted_document();self.assertEqual(self.ask()['status'],'completed')
        data=self.client.get(self.url).json();turn=data['messages'][-1]
        raw=copy.deepcopy(turn['structured']);raw['status']='incomplete';raw['missing']=[]
        sid=turn['visual_sources'][0]
        issue=dict(category='visual_ambiguity',page=1,source_ids=[sid],description='公式下标难以辨认',impact='符号定义待确认')
        raw['issues']=[issue]
        workflow=reading_workflow.effective(None,service.ROOT,'citation')
        self.assertTrue(reading_workflow.validate(json.dumps(raw),workflow,'citation',data['sources'],[])[0]['issues'])
        raw['issues'][0]['source_ids']=['fake']
        self.assertRaises(HTTPException,reading_workflow.validate,json.dumps(raw),workflow,'citation',data['sources'],[])
        raw['issues']=[];raw['missing']=['本块未找到局限性']
        self.assertRaises(HTTPException,reading_workflow.validate,json.dumps(raw),workflow,'citation',data['sources'],[])
        raw['missing']=[];raw['issues']=[{**issue,'category':'source_unavailable'}]
        self.assertRaises(HTTPException,reading_workflow.validate,json.dumps(raw),workflow,'citation',data['sources'],[])

    def test_notes_restore_visual_and_context_sources_after_clear(self):
        self.quoted_document();self.assertEqual(self.ask()['status'],'completed')
        session=self.client.get(self.url).json();turn=session['messages'][-1]
        note=self.client.post(self.url+'/notes',json={'message_id':turn['id']}).json()
        self.client.request('DELETE',self.url+'/messages',json={'revision':session['revision']})
        restored=self.client.get(self.url+'/notes/'+note['id']).json()
        self.assertEqual(restored['turn']['context_map'],turn['context_map'])
        for sid in turn['context_sources']+turn['visual_sources']:
            self.assertIn(sid,restored['sources'])
        sid=turn['visual_sources'][0]
        self.assertEqual(self.client.get(self.url+'/notes/'+note['id']+'/images/'+sid).status_code,200)

    def test_page_preparation_failure_reports_page_and_keeps_previous_answer(self):
        self.assertEqual(self.finish(self.start())['status'],'completed')
        previous=self.client.get(self.url).json()['messages']
        render=reading.render_region
        def fail(service_arg,doc,source):
            if source['page']==2:raise HTTPException(422,'测试页面渲染失败')
            return render(service_arg,doc,source)
        with patch.object(reading,'render_region',fail):job=self.finish(self.start())
        self.assertEqual(job['status'],'failed',job)
        self.assertIn('第 2 页',job['error'])
        self.assertEqual(self.client.get(self.url).json()['messages'],previous)
        self.assertEqual(self.finish(self.start())['status'],'completed')

    def test_failed_zoom_is_a_specific_backend_issue_and_can_be_resolved_on_retry(self):
        render=reading.render_region
        original=self.respond
        requested=False
        def fail(service_arg,doc,source):
            if source.get('render_scale')==8:raise RuntimeError('test visual failure')
            return render(service_arg,doc,source)
        async def model(module,config,messages,tools):
            nonlocal requested
            if tools and not requested:
                requested=True
                return baseline.call('render_page',page=1,bbox=[.1,.1,.4,.4],render_scale=8)
            value=await original(module,config,messages,tools)
            if not tools:
                raw=json.loads(value['content']);raw['status']='incomplete'
                value['content']=json.dumps(raw)
            return value
        with patch.object(reading,'render_region',fail),patch.object(reading,'invoke',model):
            job=self.finish(self.start())
        self.assertEqual(job['status'],'completed',job)
        turn=self.client.get(self.url).json()['messages'][-1]
        self.assertEqual(len(turn['structured']['issues']),1)
        self.assertEqual(turn['structured']['issues'][0]['category'],'source_unavailable')
        self.assertEqual(turn['structured']['issues'][0]['page'],1)
        self.assertEqual(self.finish(self.start())['status'],'completed')
        self.assertEqual(self.client.get(self.url).json()['messages'][-1]['structured']['issues'],[])

    def test_per_round_protocol_limit_is_enforced_without_executing_partial_batch(self):
        async def model(*args):
            call=baseline.call('render_page',page=1)['tool_calls'][0]
            return dict(role='assistant',content='',tool_calls=[{**call,'id':str(i)} for i in range(13)])
        with patch.object(reading,'invoke',model):job=self.finish(self.start())
        self.assertEqual(job['status'],'failed',job)
        self.assertIn('超过12项',job['error'])
        self.assertTrue(all(t['name']=='prepare_page_image' for t in job['trace']))

    def test_old_answer_source_state_is_frozen_when_later_zoom_fails(self):
        original=self.respond
        requested=False
        async def model(module,config,messages,tools):
            nonlocal requested
            if tools and not requested:
                requested=True
                return baseline.call('render_page',page=1,bbox=[.1,.1,.4,.4],render_scale=8)
            value=await original(module,config,messages,tools)
            if not tools and fail_visual:
                raw=json.loads(value['content']);raw['status']='incomplete';value['content']=json.dumps(raw)
            return value
        fail_visual=False
        with patch.object(reading,'invoke',model):
            self.assertEqual(self.finish(self.start())['status'],'completed')
            previous=self.client.get(self.url).json()['messages'][-1]
            zoom=previous['visual_sources'][-1]
            requested=False;fail_visual=True
            render=reading.render_region
            def fail(service_arg,doc,source):
                if source.get('render_scale')==8:raise RuntimeError('test failure')
                return render(service_arg,doc,source)
            with patch.object(reading,'render_region',fail):self.assertEqual(self.finish(self.start())['status'],'completed')
        note=self.client.post(self.url+'/notes',json={'message_id':previous['id']}).json()
        self.assertTrue(note['sources'][zoom]['provided'])


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_and_unsupported_images_are_reported_without_leaking_provider_body(self):
        for status, message, expected in [(400,'maximum context length exceeded secret','上下文长度'),
                                          (400,'too many images secret','图片数量'),
                                          (413,'request body secret','请求大小'),
                                          (400,'model does not support image input secret','不支持图片')]:
            async def handler(request):
                return httpx.Response(status,json={'error':{'message':message}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(service,'agent_client',return_value=client):
                    with self.assertRaises(HTTPException) as raised:
                        await reading_model.invoke(service,service.AgentConfig(model='test',base_url='https://example.test'),
                                                   [dict(role='user',content='test')],[])
                    # Context errors identify the input limit rather than repeating the provider body.
                    self.assertIn(expected,raised.exception.detail)
                    self.assertNotIn('secret',raised.exception.detail)
