"""Translated list markers must keep the source bullet's size and weight."""
import tempfile
import unittest
from pathlib import Path

import pymupdf

import pdf_layout
from pdf_typography import font_resources


def ink_bounds(page, clip, scale=10):
    """Return the painted bounds of one tiny region in PDF points."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
    width, height, stride = pix.width, pix.height, pix.stride
    channels, samples = pix.n, pix.samples
    x0, y0, x1, y1 = width, height, -1, -1
    for y in range(height):
        row = y * stride
        for x in range(width):
            if samples[row + x * channels] < 200:
                x0, y0 = min(x0, x), min(y0, y)
                x1, y1 = max(x1, x), max(y1, y)
    if x1 < 0:
        return None
    return pymupdf.Rect(x0 / scale, y0 / scale, (x1 + 1) / scale, (y1 + 1) / scale)


def marker_char(page, prefix):
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    if char["c"] == prefix:
                        return pymupdf.Rect(char["bbox"])
    return None


class ListMarkerTests(unittest.TestCase):
    def style(self, marker="•", size=10, bold=False):
        return {"size": size, "spans": [
            {"text": marker + " source item", "size": size, "flags": 20 if bold else 4}]}

    def test_leading_bullet_is_recognized_but_numbering_is_not(self):
        for value in ("• item", "  ▪ item", "● item", "◆ item"):
            with self.subTest(value=value):
                self.assertIsNotNone(pdf_layout.lead_list_marker(value))
        for value in ("1. item", "(2) item", "a) item", ""):
            with self.subTest(value=value):
                self.assertIsNone(pdf_layout.lead_list_marker(value))

    def test_translation_marker_uses_source_glyph_size_and_weight(self):
        display, marker = pdf_layout.list_marker_style(
            {"text": "• Source item", "translation": "· 中文条目"}, self.style(), 10)
        self.assertEqual(display, "• 中文条目")
        self.assertIsNotNone(marker)
        self.assertAlmostEqual(marker["size"], 10)
        self.assertEqual(marker["weight"], 400)
        _, bold = pdf_layout.list_marker_style(
            {"text": "• Source item", "translation": "• 中文条目"}, self.style(bold=True), 12)
        self.assertAlmostEqual(bold["size"], 12)
        self.assertEqual(bold["weight"], 600)

    def test_marker_markup_keeps_the_space_after_the_bullet(self):
        marker = {"family": "'Times New Roman',serif", "size": 10, "weight": 400}
        contents, plain, labels, _ = pdf_layout.inline_math_markup(
            "• 中文条目", [], pymupdf.Archive(), marker_style=marker)
        self.assertIn("Times New Roman", contents)
        self.assertIn("&nbsp;", contents)
        self.assertEqual(plain, "• 中文条目")
        self.assertEqual(labels, [])

    def test_bold_range_after_bullet_keeps_its_first_character(self):
        marker = {"family": "'Times New Roman',serif", "size": 10, "weight": 400}
        contents, _, _, _ = pdf_layout.inline_math_markup(
            "• 无机体坐标系：正文", [], pymupdf.Archive(), bold=[(2, 8)], marker_style=marker)
        self.assertIn("<b>无机体坐标系</b>", contents)
        self.assertNotIn("<b>机体坐标系", contents)

    def test_typeset_matches_source_bullet_and_leaves_numbering_regular(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.pdf"
            with pymupdf.open() as pdf:
                page = pdf.new_page(width=420, height=320)
                page.insert_htmlbox(
                    pymupdf.Rect(40, 60, 400, 120),
                    "<div>• The controller tracks a reference trajectory with bounded velocity and acceleration.</div>",
                    css="*{margin:0;padding:0}body{font-family:'Times New Roman',serif;font-size:10pt;line-height:1.3}",
                    scale_low=1)
                page.insert_htmlbox(
                    pymupdf.Rect(40, 160, 400, 210),
                    "<div>1. The first numbered item remains ordinary body text.</div>",
                    css="*{margin:0;padding:0}body{font-family:'Times New Roman',serif;font-size:10pt;line-height:1.3}",
                    scale_low=1)
                pdf.save(source)

            with pymupdf.open(source) as pdf:
                page = pdf[0]
                native = [block for block in page.get_text("dict")["blocks"] if block["type"] == 0]
                blocks = []
                for index, block in enumerate(native):
                    text = "\n".join("".join(span["text"] for span in line["spans"]).strip()
                                     for line in block["lines"]).strip()
                    if not text:
                        continue
                    rect = pymupdf.Rect(block["bbox"])
                    blocks.append({
                        "id": f"p1-b{len(blocks) + 1}", "text": text,
                        "bbox": [rect.x0 / page.rect.width, rect.y0 / page.rect.height,
                                 rect.x1 / page.rect.width, rect.y1 / page.rect.height],
                        "translation": ("• 控制器跟踪一条具有有界速度和加速度的参考轨迹。"
                                        if text.startswith("•") else "1. 第一项保持常规正文。"),
                        "target": "简体中文", "edited": False})
                document = {"pages": [{"number": 1, "width": page.rect.width, "height": page.rect.height,
                                       "blocks": blocks, "needs_ocr": False}]}
                source_marker = marker_char(page, "•")
                self.assertIsNotNone(source_marker)
                source_ink = ink_bounds(page, source_marker + (-1, -1, 1, 1))
                self.assertIsNotNone(source_ink)

            typography = font_resources("• 控制器跟踪一条具有有界速度和加速度的参考轨迹。")
            raw, report = pdf_layout.typeset(source, document, "简体中文", typography=typography)
            self.assertEqual(report["replaced"], 2, report)

            with pymupdf.open(stream=raw, filetype="pdf") as pdf:
                page = pdf[0]
                spans = [span for block in page.get_text("dict")["blocks"] if block.get("type") == 0
                         for line in block["lines"] for span in line["spans"] if span["text"].strip()]
                marker = next(span for span in spans if span["text"] == "•")
                body = next(span for span in spans if "控制器" in span["text"])
                numbered = next(span for span in spans if span["text"].startswith("1."))
                self.assertRegex(marker["font"], r"Nimbus ?Roman|Charis|Noto")
                self.assertAlmostEqual(marker["size"], 10, places=1)
                self.assertGreater(marker["bbox"][2], marker["bbox"][0] + 2)
                self.assertGreater(body["bbox"][0], marker["bbox"][2] + .4)
                self.assertNotRegex(marker["font"], r"ParallelBook")
                # The source bullet is a large filled dot, not the tiny CJK
                # middle dot inherited from the translated text font.
                output_ink = ink_bounds(page, pymupdf.Rect(marker["bbox"]) + (-1, -1, 1, 1))
                self.assertIsNotNone(output_ink)
                self.assertGreaterEqual(output_ink.height, source_ink.height * .8)
                self.assertLessEqual(output_ink.height, source_ink.height * 1.4)
                self.assertNotRegex(numbered["font"], r"Nimbus ?Roman")


if __name__ == "__main__":
    unittest.main()
