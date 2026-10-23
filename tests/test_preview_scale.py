import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient
import app as service


class PreviewScaleTests(unittest.TestCase):
    def test_preview_respects_rotation_and_pixel_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'preview.pdf'
            for width,height,rotation in ((300,500,0),(300,500,90),(3000,2000,270),(600,600,0)):
                with self.subTest(size=(width,height),rotation=rotation), pymupdf.open() as pdf:
                    page=pdf.new_page(width=width,height=height)
                    page.insert_text((40,60),'A readable preview')
                    page.set_rotation(rotation)
                    path.write_bytes(pdf.tobytes())
                    normal=pymupdf.Pixmap(service.render_pdf_page(path,1).body)
                    high=pymupdf.Pixmap(service.render_pdf_page(path,1,6).body)
                    self.assertGreater(high.width,normal.width)
                    self.assertLessEqual(max(high.width,high.height),4401)
                    self.assertLessEqual(high.width*high.height,12_010_000)
                    self.assertAlmostEqual(high.width/high.height,page.rect.width/page.rect.height,delta=.005)

    def test_both_preview_routes_accept_scale_and_reject_invalid_values(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(service,'DATA',Path(temp)):
            with pymupdf.open() as pdf:
                pdf.new_page(width=300,height=500).insert_text((40,60),'Preview')
                doc_id,_=service.extract_document(pdf.tobytes(),'preview.pdf')
            directory=Path(temp)/doc_id
            (directory/'layout').mkdir()
            revision='a'*24
            (directory/'layout'/f'{revision}.pdf').write_bytes((directory/'source.pdf').read_bytes())
            client=TestClient(service.app)
            urls=[f'/api/documents/{doc_id}/pages/1.png',f'/api/documents/{doc_id}/layout/{revision}/pages/1.png']
            for url in urls:
                plain=client.get(url);scaled=client.get(url+'?scale=3')
                self.assertEqual(plain.status_code,200)
                self.assertEqual(scaled.status_code,200)
                self.assertGreater(pymupdf.Pixmap(scaled.content).width,pymupdf.Pixmap(plain.content).width)
                for value in ('0','7','nan','inf','text'):
                    self.assertEqual(client.get(url+'?scale='+value).status_code,422)
            client.close()


if __name__=='__main__':
    unittest.main()
