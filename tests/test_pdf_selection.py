import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

import app as service
from pdf_selection import page_selection


def text(data):
    return "\n".join("".join(char[0] for char in line) for line in data["lines"])


class PdfSelectionTests(unittest.TestCase):
    def test_geometry_matches_char_quads_for_sizes_columns_crops_and_rotations(self):
        for size in [(595, 842), (840, 560)]:
            for rotation in (0, 90, 180, 270):
                with self.subTest(size=size, rotation=rotation), pymupdf.open() as pdf:
                    page = pdf.new_page(width=size[0], height=size[1])
                    page.insert_text((70, 80), "First column: alpha beta.", fontsize=11)
                    page.insert_text((size[0] / 2, 150), "Second column: gamma delta.", fontsize=17)
                    page.insert_text((100, 250), "Vertical label", fontsize=9, rotate=90)
                    page.set_cropbox(pymupdf.Rect(20, 25, size[0] - 20, size[1] - 25))
                    page.set_rotation(rotation)
                    data = page_selection(page)
                    self.assertEqual((data["width"], data["height"]), (page.rect.width, page.rect.height))
                    self.assertIn("First column: alpha beta.", text(data))
                    self.assertIn("Second column: gamma delta.", text(data))
                    self.assertIn("Vertical label", text(data))
                    actual = [item for line in data["lines"] for item in line]
                    native = page.get_text("rawdict", sort=True)["blocks"]
                    expected = [(line, span, char) for block in native for line in block.get("lines", [])
                                for span in line["spans"] for char in span["chars"]]
                    self.assertEqual(len(actual), len(expected))
                    for item, (line, span, char) in zip(actual, expected):
                        value, x, y, width, height, a, b, c, d = item
                        quad = pymupdf.recover_char_quad(line["dir"], span, char)
                        for point, expected_point in [
                            ((x, y), quad.ul), ((x + width * a, y + width * b), quad.ur),
                            ((x + height * c, y + height * d), quad.ll)
                        ]:
                            expected_point *= page.rotation_matrix
                            self.assertAlmostEqual(point[0], expected_point.x, places=3)
                            self.assertAlmostEqual(point[1], expected_point.y, places=3)
                        self.assertTrue(all(math.isfinite(number) for number in item[1:]))

    def test_endpoints_read_actual_output_revision_without_changing_saved_data(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(service, "DATA", Path(folder)):
            with pymupdf.open() as pdf:
                page = pdf.new_page()
                page.insert_text((50, 80), "Original words to select.")
                pdf.new_page()
                doc_id, doc = service.extract_document(pdf.tobytes(), "generic.pdf")
            directory = Path(folder) / doc_id
            before = (directory / "document.json").read_bytes()
            cache = directory / "layout"
            cache.mkdir()
            revisions = ["a" * 24, "b" * 24]
            for revision, phrase in zip(revisions, ["译文可选择词句", "修改后的译文"]):
                with pymupdf.open() as output:
                    page = output.new_page(width=500, height=700)
                    page.insert_text((90, 160), phrase, fontname="china-s", fontsize=13)
                    page.set_rotation(90)
                    output.save(cache / (revision + ".pdf"))
            with TestClient(service.app) as client:
                source = client.get(f"/api/documents/{doc_id}/pages/1/text")
                self.assertEqual(source.status_code, 200)
                self.assertEqual(text(source.json()), "Original words to select.")
                self.assertEqual(client.get(f"/api/documents/{doc_id}/pages/2/text").json()["lines"], [])
                for revision, phrase in zip(revisions, ["译文可选择词句", "修改后的译文"]):
                    result = client.get(f"/api/documents/{doc_id}/layout/{revision}/pages/1/text")
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(text(result.json()), phrase)
                    self.assertEqual(result.json()["width"], 700)
                for path in [f"{doc_id}/pages/0/text", f"{doc_id}/pages/3/text",
                             f"{doc_id}/layout/{'c' * 24}/pages/1/text", "invalid/pages/1/text"]:
                    self.assertEqual(client.get("/api/documents/" + path).status_code, 404)
            self.assertEqual(before, (directory / "document.json").read_bytes())
