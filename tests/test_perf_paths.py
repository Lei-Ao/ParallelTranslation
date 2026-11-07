"""Regression guards for the performance-oriented fast paths."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

import app as service


def sample_pdf():
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=595, height=842)
        page.insert_text((60, 80), "A small paper", fontsize=18)
        page.insert_textbox((60, 120, 530, 260),
                            "A paragraph that can be translated by the reader. "
                            "It contains enough ordinary prose for extraction.",
                            fontsize=12)
        pdf.new_page()
        return pdf.tobytes()


class PerfPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.data_patch = patch.object(service, "DATA", self.data)
        self.data_patch.start()
        service._PIXMAP_CACHE.clear()
        self.client = TestClient(service.app)
        self.client.headers["X-Local-Token"] = self.client.get("/api/session").json()["token"]

    def tearDown(self):
        self.client.close()
        service._PIXMAP_CACHE.clear()
        self.data_patch.stop()
        self.temp.cleanup()

    def upload(self):
        response = self.client.post(
            "/api/documents", files={"file": ("sample.pdf", sample_pdf(), "application/pdf")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_layout_without_translations_does_not_typeset_or_write_a_revision(self):
        doc = self.upload()
        response = self.client.post(f"/api/documents/{doc['id']}/layout", json={"target": "简体中文"})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["empty"])
        self.assertIsNone(payload["revision"])
        self.assertFalse((self.data / doc["id"] / "layout").exists())
        self.assertNotIn("saved_pdf", payload)

    def test_unchanged_page_image_is_served_from_memory(self):
        doc = self.upload()
        route = f"/api/documents/{doc['id']}/pages/1.png"
        with patch.object(service.pymupdf, "open", wraps=service.pymupdf.open) as opened:
            first = self.client.get(route)
            second = self.client.get(route)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, second.content)
        self.assertEqual(opened.call_count, 1)
        self.assertIn("immutable", first.headers.get("cache-control", ""))

    def test_repeated_load_reuses_the_parsed_document(self):
        doc = self.upload()
        first = service.load_doc(doc["id"])
        second = service.load_doc(doc["id"])
        self.assertIs(first, second)
        first["pages"][0]["blocks"][0]["translation"] = "已翻译"
        service.save_doc(self.data / doc["id"], first)
        third = service.load_doc(doc["id"])
        self.assertIs(first, third)
        self.assertEqual(third["pages"][0]["blocks"][0]["translation"], "已翻译")

    def test_drop_cap_review_does_not_rescan_on_every_load(self):
        doc = self.upload()
        stored = service.load_doc(doc["id"])
        stored["drop_cap_review_pending"] = True
        stored["drop_cap_schema"] = service.DROP_CAP_SCHEMA
        service.save_doc(self.data / doc["id"], stored)
        with patch.object(service.pymupdf, "open", wraps=service.pymupdf.open) as opened:
            service.load_doc(doc["id"])
            service.load_doc(doc["id"])
        self.assertEqual(opened.call_count, 0)


if __name__ == "__main__":
    unittest.main()