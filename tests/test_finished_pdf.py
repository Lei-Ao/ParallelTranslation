import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import app as service


class FinishedPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exports = self.root / "exports"
        self.export_patch = patch.object(service, "EXPORT_DIR", self.exports)
        self.export_patch.start()
        self.directory = self.root / "doc"
        (self.directory / "layout").mkdir(parents=True)
        self.revision = "a" * 24
        self.source = self.directory / "layout" / (self.revision + ".pdf")
        self.source.write_bytes(b"%PDF-first")
        self.doc = {"id": "b" * 32, "name": "../paper:study.pdf", "pages": [
            {"blocks": [{"translation": "译文", "target": "简体中文"}]}]}

    def tearDown(self):
        self.export_patch.stop()
        self.temp.cleanup()

    def publish(self):
        report = {"revision": self.revision}
        service.save_finished_pdf(self.directory, self.doc, "简体中文", report)
        return report

    def test_complete_safe_stable_name_and_updates(self):
        first = self.publish()
        output = Path(first["saved_pdf"])
        self.assertEqual(output.parent, self.exports.resolve())
        self.assertEqual(output.name, "paper_study-简体中文-bbbbbbbbbbbb.pdf")
        self.assertEqual(output.read_bytes(), self.source.read_bytes())
        mtime = output.stat().st_mtime_ns
        self.publish()
        self.assertEqual(output.stat().st_mtime_ns, mtime)
        self.source.write_bytes(b"%PDF-edited")
        self.assertEqual(self.publish()["saved_pdf"], first["saved_pdf"])
        self.assertEqual(output.read_bytes(), b"%PDF-edited")
        self.assertEqual(list(self.exports.glob("*.tmp")), [])

    def test_partial_wrong_language_empty_and_distinct_documents(self):
        for block in [{"translation": "", "target": "简体中文"},
                      {"translation": "   ", "target": "简体中文"},
                      {"translation": "text", "target": "English"}]:
            self.doc["pages"][0]["blocks"] = [block]
            self.assertNotIn("saved_pdf", self.publish())
        self.doc["pages"] = []
        self.assertNotIn("saved_pdf", self.publish())
        self.assertFalse(self.exports.exists())
        self.doc["pages"] = [{"blocks": [{"translation": "译文", "target": "简体中文"}]}]
        first = self.publish()["saved_pdf"]
        self.doc["id"] = "c" * 32
        self.assertNotEqual(first, self.publish()["saved_pdf"])

    def test_failure_reports_error_and_keeps_previous_file(self):
        output = Path(self.publish()["saved_pdf"])
        self.source.write_bytes(b"%PDF-new")
        with patch.object(Path, "replace", side_effect=PermissionError("locked")):
            report = self.publish()
        self.assertIn("save_error", report)
        self.assertNotIn("saved_pdf", report)
        self.assertEqual(output.read_bytes(), b"%PDF-first")
        self.assertEqual(list(self.exports.glob("*.tmp")), [])

    def test_layout_api_publishes_even_when_engine_uses_cache(self):
        cached = {"revision": self.revision, "issues": []}
        with patch.object(service, "load_doc", return_value=self.doc), \
             patch.object(service, "doc_dir", return_value=self.directory), \
             patch.object(service, "build_layout", return_value=cached), \
             TestClient(service.app) as client:
            client.headers["X-Local-Token"] = client.get("/api/session").json()["token"]
            for _ in range(2):
                response = client.post("/api/documents/" + self.doc["id"] + "/layout", json={"target": "简体中文"})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(Path(response.json()["saved_pdf"]).is_file())
        self.assertNotIn("saved_pdf", cached)
