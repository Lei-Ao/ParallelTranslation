import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pymupdf

import app as service
import paper_analysis as analysis


def sample(rotation=0, columns=1, width=612, bookmarks=False, empty=False):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=width, height=792)
        if not empty:
            page.insert_text((45, 65), '1. Method', fontsize=18)
            for col in range(columns):
                x = 45 + col * (width / 2)
                page.insert_textbox((x, 110, x + width / columns - 70, 230),
                                    'Attention models learn representations. The proposed method uses attention. '
                                    'Experiments compare accuracy and computational cost.', fontsize=10)
            page.insert_text((45, 320), '2. Experiments', fontsize=16)
            page.insert_text((45, 370), 'Results show improved accuracy.', fontsize=10)
        page.set_rotation(rotation)
        pdf.new_page(width=width, height=792)
        if bookmarks:
            pdf.set_toc([[1, 'Method', 1], [2, 'Details', 1]])
        return pdf.tobytes()


def named_bookmark(rotation):
    with pymupdf.open(stream=sample(rotation=rotation), filetype='pdf') as pdf:
        pdf.set_toc([[1, 'Method', 1]])
        # Real LaTeX PDFs often store /D as a named destination in PDF coordinates.
        outline_xref = pdf.get_outline_xrefs()[0]
        page_xref = pdf.page_xref(0)
        pdf.xref_set_key(pdf.pdf_catalog(), 'Names',
                         f'<</Dests <</Names [(method) [{page_xref} 0 R /XYZ 45 727 0]]>>>>')
        pdf.xref_set_key(outline_xref, 'A', '<</S/GoTo /D(method)>>')
        pdf.xref_set_key(outline_xref, 'Dest', 'null')
        return pdf.tobytes()


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(service, 'DATA', Path(self.temp.name))
        self.data_patch.start()
        self.client = TestClient(service.app)
        self.client.__enter__()
        self.client.headers['X-Local-Token'] = self.client.get('/api/session').json()['token']
        self.agent = {'base_url': 'https://example.com/v1', 'model': 'mock', 'api_key': 'never-persist-this-secret'}
        self.calls = []
        self.agent_patch = patch.object(service, 'invoke_agent', self.respond)
        self.agent_patch.start()
        self.doc = self.upload()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.agent_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    def upload(self, **kwargs):
        response = self.client.post('/api/documents', files={'file': ('sample.pdf', sample(**kwargs), 'application/pdf')})
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def respond(self, config, prompt, **kwargs):
        value = json.loads(prompt)
        self.calls.append(value)
        material = value['material']
        ids = [r['id'] for r in material if 'id' in r]
        if not ids:
            ids = [s['block_id'] for r in material for s in r['sources']]
        final = '生成论文概览' in value['task']
        return json.dumps({'points': [dict(section=s, text='原文指出 attention 方法及其评估结果。',
                                          kind='claim', source_ids=ids[:1])
                                      for s in analysis.SECTIONS if final] or [dict(
                                          section='方法', text='Attention is used in the proposed method.', kind='claim', source_ids=ids[:1])]})

    def start(self, kind='overview', **kwargs):
        response = self.client.post(f"/api/documents/{self.doc['id']}/analysis/jobs",
                                    json=dict(agent=self.agent, kind=kind, **kwargs))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait(self, job):
        if job['status'] == 'completed':
            return job
        for _ in range(200):
            response = self.client.get(f"/api/documents/{self.doc['id']}/analysis/jobs/{job['id']}")
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(.01)
        self.fail('Job did not finish')

    def saved(self):
        return self.client.get(f"/api/documents/{self.doc['id']}/analysis").json()

    def test_overview_sources_coverage_and_original_files_unchanged(self):
        directory = service.doc_dir(self.doc['id'])
        before = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                  for name in ['source.pdf', 'document.json']}
        job = self.wait(self.start())
        self.assertEqual(job['status'], 'completed', job)
        result = job['result']
        self.assertEqual({p['section'] for p in result['points']}, set(analysis.SECTIONS))
        self.assertEqual(result['coverage']['unreadable_pages'], [2])
        self.assertTrue(result['partial'])
        self.assertEqual(result['points'][0]['sources'][0]['doc_id'], self.doc['id'])
        self.assertEqual(before, {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in before})
        self.assertNotIn(self.agent['api_key'], (directory / 'analysis.json').read_text(encoding='utf-8'))

    def test_native_inferred_and_page_outlines_across_geometry(self):
        for rotation in (0, 90, 180, 270):
            for columns, width in [(1, 612), (2, 720)]:
                for native in (True, False):
                    with self.subTest(rotation=rotation, columns=columns, native=native):
                        doc = self.upload(rotation=rotation, columns=columns, width=width, bookmarks=native)
                        result = self.client.get(f"/api/documents/{doc['id']}/outline").json()
                        self.assertEqual(result['mode'], 'native' if native else 'inferred')
                        self.assertTrue(all(0 <= i['fraction'] <= 1 for i in result['items']))
                        if native:
                            self.assertEqual([i['level'] for i in result['items']], [1, 2])
                        else:
                            self.assertTrue(all(i['block_id'] for i in result['items']))
        doc = self.upload(empty=True)
        self.assertEqual(self.client.get(f"/api/documents/{doc['id']}/outline").json()['mode'], 'pages')

    def test_empty_document_fails_without_model_call(self):
        self.doc = self.upload(empty=True)
        self.assertEqual(self.wait(self.start())['status'], 'failed')
        self.assertEqual(self.calls, [])

    def test_named_bookmarks_use_pdf_coordinate_transform(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                raw = named_bookmark(rotation)
                response = self.client.post('/api/documents', files={'file': ('named.pdf', raw, 'application/pdf')})
                doc = response.json()
                result = self.client.get(f"/api/documents/{doc['id']}/outline").json()
                with pymupdf.open(stream=raw, filetype='pdf') as pdf:
                    target = pymupdf.Point(45, 65) * pdf[0].rotation_matrix
                    expected = target.y / pdf[0].rect.height
                self.assertEqual(result['mode'], 'native')
                self.assertAlmostEqual(result['items'][0]['fraction'], expected, places=4)

    def test_detail_cache_config_invalidation_and_translation_preserved(self):
        block = self.doc['pages'][0]['blocks'][1]
        job = self.wait(self.start('explain', block_id=block['id']))
        self.assertEqual(job['status'], 'completed')
        cached = self.start('explain', block_id=block['id'])
        self.assertTrue(cached['cached'])
        self.assertEqual(len(self.calls), 1)
        self.agent['model'] = 'another'
        self.assertEqual(self.wait(self.start('explain', block_id=block['id']))['status'], 'completed')
        self.assertEqual(len(self.calls), 2)
        current = service.load_doc(self.doc['id'])
        self.assertEqual(current['pages'], self.doc['pages'])
        self.assertEqual(len(self.saved()['results']), 2)

    def test_bad_response_preserves_last_success(self):
        block_id = self.doc['pages'][0]['blocks'][1]['id']
        self.wait(self.start('summary', block_id=block_id))
        before = self.saved()['results']
        for response in ['not json', '{"points":[]}', json.dumps({'points': [dict(text='fake', kind='claim', source_ids=['foreign'])]})]:
            async def invalid(*args, **kwargs):
                return response
            with patch.object(service, 'invoke_agent', invalid):
                failed = self.wait(self.start('summary', block_id=block_id, force=True))
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(self.saved()['results'], before)

    def test_chunk_failure_reuses_completed_parts_on_retry(self):
        original = self.respond
        failed_once = False
        async def flaky(config, prompt, **kwargs):
            nonlocal failed_once
            value = json.loads(prompt)
            if not failed_once and value['material'][0].get('text', '').startswith('Results'):
                failed_once = True
                raise HTTPException(504, '模拟超时')
            return await original(config, prompt, **kwargs)
        with patch.object(analysis, 'split_rows', lambda rows: [[r] for r in rows]), patch.object(service, 'invoke_agent', flaky):
            first = self.wait(self.start())
            self.assertEqual(first['status'], 'completed', first)
            self.assertEqual(len(first['result']['coverage']['failed_chunks']), 1)
            calls = len(self.calls)
            second = self.wait(self.start())
            self.assertEqual(second['status'], 'completed')
            self.assertEqual(second['result']['coverage']['failed_chunks'], [])
            self.assertEqual(len(self.calls) - calls, 2)  # missing chunk + final synthesis

    def test_question_history_retrieval_and_clear_during_pending_request(self):
        job = self.wait(self.start('question', question='What is attention?'))
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(len(self.saved()['conversation']), 1)
        reached, release = threading.Event(), threading.Event()
        async def delayed(config, prompt, **kwargs):
            reached.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            return await self.respond(config, prompt, **kwargs)
        with patch.object(service, 'invoke_agent', delayed):
            pending = self.start('question', question='Explain attention again')
            self.assertTrue(reached.wait(2))
            response = self.client.delete(f"/api/documents/{self.doc['id']}/analysis/conversation")
            self.assertEqual(response.status_code, 200)
            release.set()
            for _ in range(100):
                if not service.analysis_service['running']:
                    break
                time.sleep(.01)
        self.assertEqual(self.saved()['conversation'], [])
        self.assertNotIn(pending['id'], self.saved()['jobs'])
        self.assertNotIn('Explain attention again', (service.doc_dir(self.doc['id']) / 'analysis.json').read_text(encoding='utf-8'))

    def test_question_without_evidence_no_model_call(self):
        job = self.wait(self.start('question', question='量子纠缠测量'))
        self.assertEqual(job['result']['points'][0]['kind'], 'missing')
        self.assertEqual(self.calls, [])

    def test_interrupted_job_and_invalid_block_and_security(self):
        path = service.doc_dir(self.doc['id']) / 'analysis.json'
        data = dict(version=1, results={}, chunks={}, jobs={'old': dict(id='old', kind='overview', status='running')}, conversation=[], generation=0)
        path.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(self.saved()['jobs']['old']['status'], 'interrupted')
        response = self.client.post(f"/api/documents/{self.doc['id']}/analysis/jobs",
                                    json=dict(agent=self.agent, kind='explain', block_id='missing'))
        self.assertEqual(response.status_code, 404)
        token = self.client.headers.pop('X-Local-Token')
        self.assertEqual(self.client.delete(f"/api/documents/{self.doc['id']}/analysis/conversation").status_code, 403)
        self.client.headers['X-Local-Token'] = token

    def test_pending_overview_is_deduplicated_and_parallel_translation_is_preserved(self):
        reached, release = threading.Event(), threading.Event()
        async def delayed(config, prompt, **kwargs):
            reached.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            return await self.respond(config, prompt, **kwargs)
        block_id = self.doc['pages'][0]['blocks'][1]['id']
        with patch.object(service, 'invoke_agent', delayed):
            first = self.start()
            self.assertTrue(reached.wait(2))
            second = self.start(force=True)
            self.assertEqual(first['id'], second['id'])
            other = self.client.post(f"/api/documents/{self.doc['id']}/analysis/jobs",
                                     json=dict(agent={**self.agent, 'model': 'another-model'}, kind='overview'))
            self.assertEqual(other.status_code, 409)
            edited = self.client.patch(f"/api/documents/{self.doc['id']}/blocks/{block_id}",
                                       json={'translation': '已人工修订的译文', 'target': '简体中文'})
            self.assertEqual(edited.status_code, 200)
            release.set()
            self.assertEqual(self.wait(first)['status'], 'completed')
        _, _, block = service.find_block(service.load_doc(self.doc['id']), block_id)
        self.assertEqual(block['translation'], '已人工修订的译文')

    def test_long_overview_reduction_visits_every_chunk(self):
        doc = service.load_doc(self.doc['id'])
        doc['pages'][0]['blocks'] = [dict(id=f'long-{i}', text=f'Chunk {i} attention study.',
                                               bbox=[.1, .1, .9, .2], translation='', target='') for i in range(32)]
        service.save_doc(service.doc_dir(doc['id']), doc)
        visited = []
        reductions = []
        async def long_response(config, prompt, **kwargs):
            value = json.loads(prompt)
            material = value['material']
            if 'id' in material[0]:
                ids = [r['id'] for r in material]
                visited.extend(ids)
                text = 'A detailed scientific observation. ' * 30
            else:
                reductions.append(len(json.dumps(material, ensure_ascii=False)))
                ids = [s['block_id'] for r in material for s in r['sources']]
                text = 'Condensed summary.'
            return json.dumps({'points': [dict(section='方法', text=text, kind='claim', source_ids=ids[:1])]})
        with patch.object(analysis, 'split_rows', lambda rows: [[r] for r in rows]), patch.object(service, 'invoke_agent', long_response):
            job = self.wait(self.start())
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(set(visited), {f'long-{i}' for i in range(32)})
        self.assertGreater(len(reductions), 1)
        self.assertTrue(all(size < analysis.CHUNK_SIZE + 100 for size in reductions))

    def test_question_sources_are_document_scoped(self):
        other = self.upload()
        block_id = self.doc['pages'][0]['blocks'][1]['id']
        job = self.wait(self.start('question', question='Explain the method', scope='selection', block_id=block_id))
        self.assertTrue(all(s['doc_id'] == self.doc['id'] for p in job['result']['points'] for s in p['sources']))
        self.assertEqual(self.client.get(f"/api/documents/{other['id']}/analysis").json()['conversation'], [])


class HelpersTests(unittest.TestCase):
    def test_large_block_is_split_losslessly(self):
        rows = [dict(id='a', page=1, text='x' * 40000)]
        chunks = analysis.split_rows(rows)
        self.assertEqual(''.join(r['text'] for c in chunks for r in c), rows[0]['text'])
        self.assertTrue(all(sum(len(r['text']) for r in c) <= analysis.CHUNK_SIZE for c in chunks))
        self.assertTrue(all(r['id'] == 'a' for c in chunks for r in c))

    def test_retrieval_supports_saved_chinese_translation(self):
        rows = [dict(id='a', page=1, text='Neural attention', translation='注意力机制'),
                dict(id='b', page=2, text='References', translation='')]
        self.assertEqual(analysis.retrieve(rows, '注意力机制', [])[0]['id'], 'a')

    def test_chinese_method_question_can_retrieve_untranslated_english(self):
        rows = [dict(id='a', page=1, text='The proposed method compares with previous approaches.', translation='')]
        self.assertEqual(analysis.retrieve(rows, '作者的方法与已有方法有什么区别？', [])[0]['id'], 'a')
        self.assertEqual(analysis.retrieve(rows, 'What is quantum entanglement?', []), [])

    def test_citations_must_be_in_provided_context(self):
        raw = json.dumps({'points': [dict(section='Method', text='Claim', kind='claim', source_ids=['b'])]})
        with self.assertRaises(HTTPException):
            analysis.parse_points(raw, {'a': dict(page=1)}, 'doc')


if __name__ == '__main__':
    unittest.main()
