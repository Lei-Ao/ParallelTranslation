import asyncio
import concurrent.futures
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import app as service
import paper_reading as reading
import reading_notes
import reading_workflow
import test_paper_reading as baseline


class NoteTests(unittest.TestCase):
    setUp = baseline.ReadingTests.setUp
    tearDown = baseline.ReadingTests.tearDown
    upload = baseline.ReadingTests.upload
    start = baseline.ReadingTests.start
    finish = baseline.ReadingTests.finish
    respond = baseline.ReadingTests.respond

    def answer(self):
        self.assertEqual(self.finish(self.start())['status'], 'completed')
        return self.client.get(self.url).json()['messages'][-1]

    def favorite(self, turn):
        result = self.client.post(self.url + '/notes', json={'message_id': turn['id'], 'answer': 'forged'})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def clear(self):
        revision = self.client.get(self.url).json()['revision']
        return self.client.request('DELETE', self.url + '/messages', json={'revision': revision})

    def test_favorite_snapshot_is_idempotent_and_does_not_change_paper_or_translation(self):
        turn = self.answer()
        directory = service.doc_dir(self.doc['id'])
        before = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                  for name in ('source.pdf', 'document.json')}
        note = self.favorite(turn)
        self.assertEqual(note['turn'], turn)
        self.assertIn('p1-b1', note['sources'])
        self.assertEqual(self.favorite(turn)['id'], note['id'])
        self.assertEqual(len(self.client.get(self.url + '/notes').json()['notes']), 1)
        saved = (directory / 'reading-notes.json').read_text(encoding='utf-8')
        self.assertNotIn(self.agent['api_key'], saved)
        self.assertNotIn('forged', saved)
        self.assertEqual(before, {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in before})

    def test_concurrent_favorites_create_one_note(self):
        turn = self.answer()
        def save(_):
            return self.client.post(self.url + '/notes', json={'message_id': turn['id']}).json()['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(save, range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.client.get(self.url + '/notes').json()['notes']), 1)

    def test_note_survives_message_deletion_clear_and_session_archive(self):
        turn = self.answer(); note = self.favorite(turn)
        data = self.client.get(self.url).json()
        deleted = self.client.request('DELETE', self.url + '/messages/' + turn['id'], json={'revision': data['revision']})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()['messages'], [])
        self.assertEqual(deleted.json()['sources'], {})
        self.assertEqual(self.clear().status_code, 200)
        self.client.delete(self.url)
        self.assertEqual(self.client.get(self.url + '/notes/' + note['id']).json(), note)
        exported = self.client.get(self.url + '/notes/' + note['id'] + '/download')
        self.assertEqual(exported.status_code, 200)
        self.assertIn('原文第 1 页', exported.text)
        self.assertNotIn('[[p1-b1]]', exported.text)
        self.assertIn('.md', exported.headers['content-disposition'])

    def test_favorite_can_be_deleted_and_saved_again(self):
        turn = self.answer(); note = self.favorite(turn)
        self.assertEqual(self.client.delete(self.url + '/notes/' + note['id']).status_code, 200)
        self.assertEqual(self.client.get(self.url + '/notes').json()['notes'], [])
        self.assertEqual(self.client.get(self.url + '/notes/' + note['id']).status_code, 404)
        self.assertEqual(self.favorite(turn)['id'], note['id'])

    def test_delete_rebuilds_memory_sources_and_associated_jobs(self):
        turns = [self.answer() for _ in range(8)]
        path = service.doc_dir(self.doc['id']) / 'reading.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        removed = data['messages'][0]
        removed['question'] = 'unique deleted question'
        removed['answer'] = 'unique deleted memory [[only-deleted]]'
        removed['refs'] = ['only-deleted']
        data['sources']['only-deleted'] = dict(doc_id=self.doc['id'], page=1)
        data['memory'] = reading.conversation_memory(data['messages'])
        path.write_text(json.dumps(data), encoding='utf-8')
        result = self.client.request('DELETE', self.url + '/messages/' + turns[0]['id'], json={'revision': data['revision']})
        self.assertEqual(result.status_code, 200, result.text)
        remaining = result.json()
        self.assertNotIn('unique deleted', json.dumps(remaining))
        self.assertNotIn('only-deleted', remaining['sources'])
        self.assertIn('p1-b1', remaining['sources'])
        self.assertEqual(len(remaining['jobs']), 7)
        self.assertEqual(remaining['memory'], reading.conversation_memory(remaining['messages']))
        self.assertEqual(remaining['last_context'], remaining['messages'][-1]['context'])
        self.assertEqual(remaining['coverage'], {})
        self.assertEqual(remaining['tool_progress'], [])

    def test_clear_preserves_reading_position_progress_and_notes(self):
        note = self.favorite(self.answer())
        data = self.client.get(self.url).json()
        updated = self.client.patch(self.url, json=dict(revision=data['revision'], browse_page=2, status='completed')).json()
        cleared = self.clear().json()
        for key in ('browse_page', 'unit_id', 'progress', 'structure', 'skill_version'):
            self.assertEqual(cleared[key], updated[key])
        for key, empty in [('messages', []), ('jobs', {}), ('memory', ''), ('sources', {}), ('coverage', {}), ('tool_progress', [])]:
            self.assertEqual(cleared[key], empty)
        self.assertIsNone(cleared['last_context'])
        self.assertNotEqual(cleared['id'], updated['id'])
        self.assertEqual(self.client.get(self.url + '/notes/' + note['id']).status_code, 200)

    def test_clear_cancels_inflight_work_and_rejects_late_writes(self):
        turn = self.answer(); note = self.favorite(turn)
        async def slow(*args):
            try:
                await asyncio.sleep(.3)
            except asyncio.CancelledError:
                pass
            return dict(role='assistant', content='late answer [[p1-b1]]', tool_calls=[])
        with patch.object(reading, 'invoke', slow):
            self.start()
            data = self.client.get(self.url).json()
            rejected = self.client.request('DELETE', self.url + '/messages/' + turn['id'], json={'revision': data['revision']})
            self.assertEqual(rejected.status_code, 409)
            self.assertEqual(self.clear().status_code, 200)
            time.sleep(.4)
        data = self.client.get(self.url).json()
        self.assertEqual(data['messages'], [])
        self.assertEqual(data['jobs'], {})
        self.assertEqual(self.client.get(self.url + '/notes/' + note['id']).status_code, 200)

    def test_saved_visual_source_remains_available_after_clear(self):
        self.responses = [baseline.call('render_page', page=1, bbox=[.1,.1,.8,.8]),
                          dict(role='assistant', content='实际图像区域。', tool_calls=[])]
        note = self.favorite(self.answer())
        source_id = note['turn']['images'][0]
        self.clear()
        self.assertEqual(self.client.get(self.url + '/images/' + source_id).status_code, 404)
        response = self.client.get(self.url + '/notes/' + note['id'] + '/images/' + source_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['content-type'], 'image/png')
        self.assertIn('图像区域', self.client.get(self.url + '/notes/' + note['id'] + '/download').text)

    def test_legacy_summaries_import_without_duplicates_and_delete_permanently(self):
        directory = service.doc_dir(self.doc['id']) / 'reading-notes'; directory.mkdir()
        summary_id = 'a' * 32
        path = directory / (summary_id + '.md'); path.write_text('# 旧小结\n\n已有正文', encoding='utf-8')
        data = self.client.get(self.url).json()
        stored = service.doc_dir(self.doc['id']) / 'reading.json'
        raw = json.loads(stored.read_text(encoding='utf-8'))
        raw['summaries'] = [dict(id=summary_id, created='2020-01-01', markdown='旧小结')]
        stored.write_text(json.dumps(raw), encoding='utf-8')
        for _ in range(2):
            notes = self.client.get(self.url + '/notes').json()['notes']
            self.assertEqual([n['id'] for n in notes], [summary_id])
        self.assertIn('已有正文', self.client.get(self.url + '/notes/' + summary_id + '/download').text)
        self.assertEqual(self.client.delete(self.url + '/notes/' + summary_id).status_code, 200)
        self.assertFalse(path.exists())
        self.assertEqual(self.client.get(self.url + '/notes').json()['notes'], [])
        self.assertEqual(self.client.get(self.url).json()['summaries'], [])
        # A leftover file after an interrupted deletion must never reappear as a note.
        path.write_text('leftover', encoding='utf-8')
        self.assertEqual(self.client.get(self.url + '/notes').json()['notes'], [])
        self.assertEqual(self.client.delete(self.url + '/notes/' + summary_id).status_code, 200)
        self.assertFalse(path.exists())

    def test_notes_remain_readable_when_conversation_file_is_unavailable(self):
        note = self.favorite(self.answer())
        (service.doc_dir(self.doc['id']) / 'reading.json').unlink()
        self.assertEqual(self.client.get(self.url + '/notes/' + note['id']).json(), note)

    def test_corrupt_note_store_is_reported_without_overwrite(self):
        turn = self.answer()
        path = service.doc_dir(self.doc['id']) / 'reading-notes.json'
        path.write_text('{corrupt', encoding='utf-8')
        self.assertEqual(self.client.get(self.url + '/notes').status_code, 409)
        self.assertEqual(self.client.post(self.url + '/notes', json={'message_id': turn['id']}).status_code, 409)
        self.assertEqual(path.read_text(encoding='utf-8'), '{corrupt')

    def test_atomic_write_failure_keeps_existing_notes_and_retry_succeeds(self):
        note = self.favorite(self.answer())
        turn = self.answer()
        path = service.doc_dir(self.doc['id']) / 'reading-notes.json'
        original_bytes = path.read_bytes()
        replace = Path.replace
        def fail_replace(source, target):
            if Path(target).name == 'reading-notes.json':
                raise PermissionError('locked test file')
            return replace(source, target)
        with patch.object(Path, 'replace', fail_replace):
            response = self.client.post(self.url + '/notes', json={'message_id': turn['id']})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(self.client.delete(self.url + '/notes/' + note['id']).status_code, 503)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.favorite(turn)
        self.assertEqual(len(self.client.get(self.url + '/notes').json()['notes']), 2)

    def test_legacy_file_delete_failure_is_reported_and_can_be_retried(self):
        directory = service.doc_dir(self.doc['id']) / 'reading-notes'; directory.mkdir()
        note_id = 'b' * 32
        legacy = directory / (note_id + '.md'); legacy.write_text('# old note', encoding='utf-8')
        unlink = Path.unlink
        def fail_unlink(path, *args, **kwargs):
            if path == legacy:
                raise PermissionError('locked test note')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_unlink):
            self.assertEqual(self.client.delete(self.url + '/notes/' + note_id).status_code, 503)
        self.assertTrue(legacy.exists())
        self.assertEqual([n['id'] for n in self.client.get(self.url + '/notes').json()['notes']], [note_id])
        self.assertIn('old note', self.client.get(self.url + '/notes/' + note_id + '/download').text)
        self.assertEqual(self.client.delete(self.url + '/notes/' + note_id).status_code, 200)
        self.assertFalse(legacy.exists())

    def test_revision_and_document_isolation(self):
        turn = self.answer(); note = self.favorite(turn)
        self.assertEqual(self.client.request('DELETE', self.url + '/messages', json={'revision': 0}).status_code, 409)
        other = self.upload(); other_url = f"/api/documents/{other['id']}/reading"
        self.assertEqual(self.client.get(other_url + '/notes/' + note['id']).status_code, 404)
        self.assertEqual(self.client.get(other_url + '/notes').json()['notes'], [])
        self.client.headers.pop('X-Local-Token')
        self.assertEqual(self.client.post(self.url + '/notes', json={'message_id': turn['id']}).status_code, 403)
        self.assertEqual(self.client.delete(self.url + '/notes/' + note['id']).status_code, 403)

    def test_presentation_rules_apply_to_pinned_sessions_and_record_version(self):
        turn = self.answer()
        presentation = reading_workflow.presentation(service.ROOT)
        self.assertIn(presentation['text'], self.calls[0][0]['content'])
        self.assertEqual(turn['format_version'], presentation['version'])

    def test_export_preserves_math_original_quotes_and_deduplicated_sources(self):
        turn = dict(id='turn', question='说明 $x^2$', answer='公式 $$x^2$$ [[a]] [[a]]', refs=['a'],
            structured=dict(sections=[dict(section='回答',text='原文',source_ids=['a'])]),
            citations=[dict(id='quote',text='完整原文',source=dict(doc_id=self.doc['id'],page=2,rects=[[0,0,1,1]]))])
        session = {**self.session, 'sources': {'a':dict(doc_id=self.doc['id'],page=1,block_id='p1-b1')}}
        note = reading_notes.snapshot(self.doc, session, turn)
        result = reading_notes.markdown(note)
        self.assertIn('$$x^2$$', result)
        self.assertIn('完整原文', result)
        self.assertEqual(result.count('原文第 1 页'), 1)
        self.assertIn('引用区域', result)


if __name__ == '__main__':
    unittest.main()
