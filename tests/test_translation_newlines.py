"""One source paragraph must stay one translated paragraph.

Translation agents sometimes return a single source paragraph as several
lines. Those breaks are layout artefacts, not paragraph boundaries; they must
not force a break inside the translated paragraph. Line-structured blocks
(lists, notation tables) keep their breaks.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

import app as service
import pdf_layout


class SoftTranslationTests(unittest.TestCase):
    def test_join_soft_lines_keeps_sentence_flow(self):
        self.assertEqual(pdf_layout.join_soft_lines("第一句。\n第二句。"), "第一句。第二句。")
        self.assertEqual(pdf_layout.join_soft_lines("摘要\n本文提出"), "摘要 本文提出")
        self.assertEqual(pdf_layout.join_soft_lines("First line.\nSecond line."),
                         "First line. Second line.")

    def test_soft_translation_only_merges_prose_blocks(self):
        prose = {"text": "This paragraph contains enough ordinary words and the common "
                          "stop words to be recognized as prose for this test.",
                 "translation": "第一句。\n第二句。"}
        self.assertEqual(pdf_layout.soft_translation(prose), "第一句。第二句。")
        listing = {"text": "• first item\n• second item",
                   "translation": "• 第一项\n• 第二项"}
        self.assertEqual(pdf_layout.soft_translation(listing), "• 第一项\n• 第二项")
        notation = {"text": "xA ∈ R3 世界坐标\nvA ∈ R3 速度",
                    "translation": "xA ∈ R3 世界坐标\nvA ∈ R3 速度"}
        self.assertEqual(pdf_layout.soft_translation(notation),
                         "xA ∈ R3 世界坐标\nvA ∈ R3 速度")
        # A block that already contains a real blank line keeps paragraph breaks.
        multi = {"text": "This block contains two ordinary paragraphs with enough words "
                         "and the common stop words to count as prose.\n\n"
                         "The second paragraph also has enough words and the common stop words.",
                 "translation": "第一段。\n第二段。"}
        self.assertEqual(pdf_layout.soft_translation(multi), "第一段。\n第二段。")


class LayoutParagraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.data_patch = patch.object(service, "DATA", self.directory)
        self.data_patch.start()
        self.export_patch = patch.object(service, "EXPORT_DIR", self.directory / "exports")
        self.export_patch.start()

    def tearDown(self):
        self.export_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    def test_prose_translation_newline_does_not_split_the_paragraph(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=500, height=300)
            page.insert_textbox(
                pymupdf.Rect(40, 40, 460, 140),
                "This source paragraph contains enough ordinary words and the common "
                "stop words to be recognized as prose. It wraps over several visual "
                "lines inside one extraction block, so the translation must reuse one paragraph.",
                fontsize=11)
            raw = pdf.tobytes()
        doc_id, doc = service.extract_document(raw, "paragraph.pdf")
        self.assertTrue(doc["pages"][0]["blocks"])
        block = doc["pages"][0]["blocks"][0]
        self.assertTrue(pdf_layout.is_prose(block["text"]), block["text"])
        block.update(translation="第一句。\n第二句。", target="简体中文")
        result = pdf_layout.build_layout(self.directory / doc_id, doc, "简体中文")
        self.assertEqual(result["replaced"], 1, result["issues"])
        with pymupdf.open(self.directory / doc_id / "layout" / (result["revision"] + ".pdf")) as pdf:
            lines = ["".join(span["text"] for span in line["spans"])
                     for item in pdf[0].get_text("dict")["blocks"] if item.get("type") == 0
                     for line in item["lines"]]
            matched = [line for line in lines if "第一句" in line or "第二句" in line]
            self.assertEqual(len(matched), 1, lines)
            self.assertIn("第一句", matched[0])
            self.assertIn("第二句", matched[0])


if __name__ == "__main__":
    unittest.main()
