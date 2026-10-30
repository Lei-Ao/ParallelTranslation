"""Body font consistency uses safe column space, preserving source style levels."""
import math
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pymupdf
import pdf_layout


class BodyFontFlowTests(unittest.TestCase):
    def render(self, *, columns=2, width=612, height=792, size=10, rotation=0,
               cropped=False, dense=False, barrier=False, grouped=True, heading=False, linked=False):
        with tempfile.TemporaryDirectory() as temp, pymupdf.open() as source:
            page = source.new_page(width=width, height=height)
            if cropped:
                page.set_cropbox(pymupdf.Rect(12, 10, width - 12, height - 10))
            width, height = page.rect.width, page.rect.height
            gutter = 24
            column_width = (width - 80 - gutter * (columns - 1)) / columns
            paragraph_ids = []
            if heading:
                page.insert_text((40, 20), "1. Methods\nand analysis", fontname="tiro", fontsize=12)
            for column in range(columns):
                left = 40 + column * (column_width + gutter)
                words = ("The method is useful for the control of a robot. " * 20).split()
                line = ""
                for word in words:
                    candidate = (line + " " + word).strip()
                    if pymupdf.get_text_length(candidate, fontname="tiro", fontsize=size) > column_width - size:
                        break
                    line = candidate
                for paragraph, count in enumerate((3, 8)):
                    top = 65 + (size * 3.6 + (12 if barrier else 0)) * paragraph
                    for row in range(count):
                        page.insert_text((left + (size if row == 0 else 0), top + row * size * 1.2),
                                         line, fontname="tiro", fontsize=size)
                if barrier:
                    page.draw_line((left, 65 + size * 3), (left + column_width, 65 + size * 3))
            page.insert_text((40, height - 25), "A small footnote.", fontname="tiro", fontsize=size * .7)
            native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            if linked:
                page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(native[0]["bbox"]),
                                  "uri": "https://example.org/paper"})
            page.set_rotation(rotation)
            blocks = []
            for index, item in enumerate(native):
                box = pymupdf.Rect(item["bbox"]) * page.rotation_matrix
                text = "\n".join("".join(s["text"] for s in line["spans"]) for line in item["lines"])
                footnote = "footnote" in text
                if text.startswith("1. "):
                    translation = "1. 方法"
                elif footnote:
                    translation = "小号脚注。"
                else:
                    paragraph = len(item["lines"]) > 4
                    line_capacity = pymupdf.Rect(item["bbox"]).width / size
                    length = math.ceil(line_capacity * (10.5 if dense and paragraph else 2.5 if paragraph else 3.5))
                    translation = ("正文应保持一致字号并共享栏内空间" * 100)[:length] + "。"
                    paragraph_ids.append(str(index))
                blocks.append({"id": str(index), "text": text, "translation": translation,
                               "target": "简体中文", "bbox": [box.x0 / page.rect.width, box.y0 / page.rect.height,
                                                                   box.x1 / page.rect.width, box.y1 / page.rect.height]})
            path = Path(temp) / "source.pdf"
            source.save(path)
            document = {"pages": [{"number": 1, "blocks": blocks}]}
            if grouped:
                raw, report = pdf_layout.typeset(path, document, "简体中文")
            else:
                with patch.object(pdf_layout, "fit_body_runs", return_value=[]):
                    raw, report = pdf_layout.typeset(path, document, "简体中文")
        pdf = pymupdf.open(stream=raw, filetype="pdf")
        self.addCleanup(pdf.close)
        sizes = {}
        for block in blocks:
            if block["id"] not in report["placements"]:
                continue
            box = pdf_layout.source_rect(pdf[0], {"bbox": report["placements"][block["id"]]})
            spans = [s for b in pdf[0].get_text("dict", clip=box)["blocks"] if b["type"] == 0
                     for line in b["lines"] for s in line["spans"]]
            sizes[block["id"]] = {round(s["size"], 2) for s in spans if any("\u3400" <= c <= "\u9fff" for c in s["text"])}
        return pdf[0], report, blocks, paragraph_ids, sizes

    def test_local_fitting_reproduces_mixed_body_sizes(self):
        _, _, _, ids, sizes = self.render(grouped=False)
        self.assertGreater(len(set(size for key in ids for size in sizes[key])), 1)

    def test_body_runs_keep_nominal_size_across_columns_pages_sizes_and_rotation(self):
        cases = [(1, 595, 842, 10, 0, False), (2, 612, 792, 10, 90, False),
                 (3, 840, 595, 12, 180, False), (2, 720, 900, 8, 270, True),
                 (1, 595, 842, 7, 0, True)]
        for columns, width, height, size, rotation, cropped in cases:
            with self.subTest(columns=columns, size=size, rotation=rotation):
                page, report, blocks, ids, sizes = self.render(
                    columns=columns, width=width, height=height, size=size,
                    rotation=rotation, cropped=cropped)
                self.assertEqual(report["preserved"], 0, report["issues"])
                self.assertEqual(len(ids), columns * 2)
                for key in ids:
                    self.assertEqual(sizes[key], {size})
                self.assertEqual(sizes[blocks[-1]["id"]], {round(size * .7, 2)})
                expected = Counter(c for b in blocks for c in b["translation"] if not c.isspace())
                visible = Counter(c for c in page.get_text() if not c.isspace())
                self.assertFalse(expected - visible)
                self.assertEqual(page.rotation, rotation)
                positions = [pdf_layout.source_rect(page, {"bbox": report["placements"][key]}) for key in ids]
                self.assertFalse(any(pdf_layout.overlap(a, b) > .1 for i, a in enumerate(positions) for b in positions[i + 1:]))

    def test_dense_body_uses_one_shared_scale_without_losing_text(self):
        _, report, _, ids, sizes = self.render(dense=True)
        self.assertEqual(report["preserved"], 0, report["issues"])
        shared = set(size for key in ids for size in sizes[key])
        self.assertEqual(len(shared), 1, sizes)
        self.assertGreaterEqual(min(shared), 8)
        self.assertLess(max(shared), 10)

    def test_fixed_graphic_is_a_barrier_to_shared_space(self):
        _, _, _, ids, sizes = self.render(barrier=True)
        _, _, _, old_ids, old_sizes = self.render(barrier=True, grouped=False)
        self.assertEqual([sizes.get(key) for key in ids], [old_sizes.get(key) for key in old_ids])

    def test_heading_and_body_reflow_reports_use_final_positions(self):
        page, report, _, ids, sizes = self.render(heading=True)
        for key in ids:
            self.assertEqual(sizes[key], {10})
        self.assertTrue(report.get("flow"))
        for move in report["flow"]:
            prior = pdf_layout.source_rect(page, {"bbox": report["placements"][move["previous"][0]]})
            box = pdf_layout.source_rect(page, {"bbox": report["placements"][move["blocks"][0]]})
            self.assertAlmostEqual(move["previous_bottom"], prior.y1, delta=.01)
            self.assertAlmostEqual(box.y0 - prior.y1, move["gap"], delta=.01)

    def test_whole_block_link_follows_the_shared_font_placement(self):
        page, report, _, ids, sizes = self.render(linked=True)
        self.assertEqual(sizes[ids[0]], {10})
        placed = pdf_layout.source_rect(page, {"bbox": report["placements"][ids[0]]})
        links = [item for item in report.get("placed_links", []) if item.get("uri") == "https://example.org/paper"]
        self.assertEqual(len(links), 1)
        self.assertTrue(placed.contains(pymupdf.Rect(links[0]["rect"])))
        self.assertFalse(any(item["kind"] == "link" for item in report["issues"]))


if __name__ == "__main__":
    unittest.main()
