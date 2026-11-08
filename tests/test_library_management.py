import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import app as service
from test_app import sample_pdf


class LibraryTests(unittest.TestCase):
    def setUp(self):
        from vision_fixture import install
        install(self)
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patcher=patch.object(service,'DATA',self.root/'data');self.patcher.start()
        service.DATA.mkdir()
        self.client=TestClient(service.app)
        self.client.headers['X-Local-Token']=self.client.get('/api/session').json()['token']
        self.doc=self.upload('same.pdf')
        self.base=f"/api/documents/{self.doc['id']}"

    def tearDown(self):
        self.client.close();self.patcher.stop();self.temp.cleanup()

    def upload(self,name):
        response=self.client.post('/api/documents',files={'file':(name,sample_pdf(),'application/pdf')})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_rename_preserves_translations_ids_assets_and_other_documents(self):
        other=self.upload('same.pdf')
        block=self.doc['pages'][0]['blocks'][0]
        self.client.patch(self.base+'/blocks/'+block['id'],json={'translation':'手工译文'})
        before=copy.deepcopy(service.snapshot_doc(self.doc['id']))
        directory=service.doc_dir(self.doc['id']);pdf=(directory/'source.pdf').read_bytes()
        self.client.get('/api/documents')  # Populate list cache before renaming.
        result=self.client.patch(self.base,json={'name':'  新论文 <标题>.pdf  '})
        self.assertEqual(result.status_code,200,result.text)
        after=service.snapshot_doc(self.doc['id'])
        self.assertEqual(after['name'],'新论文 <标题>.pdf')
        self.assertEqual(after['pages'],before['pages'])
        self.assertEqual((directory/'source.pdf').read_bytes(),pdf)
        names={d['id']:d['name'] for d in self.client.get('/api/documents').json()}
        self.assertEqual(names[self.doc['id']],after['name']);self.assertEqual(names[other['id']],'same.pdf')

    def test_delete_archives_owned_data_and_leaves_exports_and_other_docs(self):
        other=self.upload('same.pdf')
        directory=service.doc_dir(self.doc['id'])
        (directory/'notes.json').write_text('{"note":"saved"}')
        source=(directory/'source.pdf').read_bytes()
        external=self.root/'exported.pdf';external.write_bytes(b'export')
        self.client.get('/api/documents');self.client.get(self.base+'/pages/1.png')
        result=self.client.delete(self.base)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.client.get(self.base).status_code,404)
        self.assertEqual(self.client.get(self.base+'/pages/1.png').status_code,404)
        self.assertEqual([d['id'] for d in self.client.get('/api/documents').json()],[other['id']])
        archived=list((service.DATA/'.trash').glob(self.doc['id']+'-*'))
        self.assertEqual(len(archived),1)
        self.assertEqual((archived[0]/'source.pdf').read_bytes(),source)
        self.assertTrue((archived[0]/'notes.json').exists())
        self.assertEqual(external.read_bytes(),b'export')
        self.assertNotIn(self.doc['id'],service._DOC_CACHE)

    def test_invalid_names_and_authentication_do_not_modify_document(self):
        for name in [' ','.','..','a/b','a\\b','x\nY','x'*201]:
            self.assertIn(self.client.patch(self.base,json={'name':name}).status_code,[400,422])
        self.assertEqual(self.client.delete(self.base,headers={'X-Local-Token':'invalid'}).status_code,403)
        self.assertEqual(self.client.get(self.base).json()['name'],'same.pdf')

    def test_inflight_translation_prevents_delete_and_rename(self):
        started,release=threading.Event(),threading.Event()
        async def model(*args,**kwargs):
            started.set();await asyncio.to_thread(release.wait,5);return '完成译文'
        block=self.doc['pages'][0]['blocks'][0]
        with patch.object(service,'invoke_agent',side_effect=model),ThreadPoolExecutor() as executor:
            future=executor.submit(self.client.post,self.base+'/blocks/'+block['id']+'/translate',
                                   json={'agent':{'model':'test'}})
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(self.client.delete(self.base).status_code,409)
                self.assertEqual(self.client.patch(self.base,json={'name':'changed'}).status_code,409)
            finally:release.set()
            self.assertEqual(future.result().status_code,200)
        self.assertEqual(self.client.delete(self.base).status_code,200)

    def test_background_reader_prevents_delete(self):
        class Pending:
            def done(self):return False
        key=(str(service.doc_dir(self.doc['id']).resolve()),'test')
        service.reading_service['running'][key]=Pending()
        try:self.assertEqual(self.client.delete(self.base).status_code,409)
        finally:service.reading_service['running'].pop(key)
        self.assertEqual(self.client.get(self.base).status_code,200)

    def test_failed_archive_keeps_library_and_data(self):
        with patch.object(Path,'rename',side_effect=PermissionError('in use')):
            self.assertEqual(self.client.delete(self.base).status_code,409)
        self.assertEqual(self.client.get(self.base).status_code,200)
        self.assertEqual(len(self.client.get('/api/documents').json()),1)


if __name__=='__main__':unittest.main()
