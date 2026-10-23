import io
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pymupdf

import app as service
import pdf_layout
import pdf_typography
from pdf_flow import original_formula_units


class FormulaFlowTests(unittest.TestCase):
    def test_equation_fraction_number_and_explanation_move_as_intact_unit(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), tempfile.TemporaryDirectory() as temp, patch.object(service, "DATA", Path(temp)):
                with pymupdf.open() as source:
                    page = source.new_page(width=600, height=700)
                    prose = "The method is useful for the control of a robot."
                    page.insert_text((40, 60), "\n".join([prose] * 8), fontname="tiro", fontsize=10, lineheight=1.2)
                    page.insert_text((100, 175), "III. METHOD", fontname="tiro", fontsize=8)
                    page.insert_text((40, 194), "A. Robot Dynamics", fontname="tiro", fontsize=10)
                    page.insert_text((40, 215), "The state of the robot is given by the equation:", fontname="tiro", fontsize=10)
                    page.insert_text((80, 244), "q = 1", fontname="tiro", fontsize=10)
                    page.draw_line((97, 248), (124, 248), width=.5)
                    page.insert_text((104, 260), "2", fontname="tiro", fontsize=8)
                    page.insert_text((235, 244), "(1)", fontname="tiro", fontsize=10)
                    page.insert_text((40, 292), "Here the state of the robot is given by q.", fontname="tiro", fontsize=10)
                    page.set_rotation(rotation)
                    original = source.tobytes()
                doc_id, document = service.extract_document(original, "formula.pdf")
                expected = []
                for block in document["pages"][0]["blocks"]:
                    text = block["text"]
                    if text.startswith("The method"):
                        translation = "前文译文，后续标题和公式应连续排版。"
                    elif text.startswith("III."):
                        translation = "三、方法"
                    elif text.startswith("A."):
                        translation = "A. 动力学"
                    elif text.startswith("The state"):
                        translation = "机器人的状态由下式给出："
                    elif text.startswith("Here"):
                        translation = "这里 q 表示机器人的状态。"
                    else:
                        translation = text
                    block.update(translation=translation, target="简体中文")
                    expected.append(translation)
                output, report = pdf_layout.typeset(Path(temp) / doc_id / "source.pdf", document, "简体中文")
                with pymupdf.open(stream=output, filetype="pdf") as result:
                    page = result[0]
                    normalize = lambda text: Counter(c for c in text if not c.isspace())
                    self.assertEqual(normalize(page.get_text()), normalize("".join(expected)))
                    self.assertEqual(page.rotation, rotation)
                    page.set_rotation(0)
                    bars = [drawing["rect"] for drawing in page.get_drawings() if abs(drawing["rect"].width - 27) < .01]
                    self.assertEqual(len(bars), 1)
                    self.assertLess(bars[0].y0, 200)
                    equation = next(block for block in document["pages"][0]["blocks"] if "q = 1" in block["text"])
                    denominator = next(block for block in document["pages"][0]["blocks"] if block["text"].strip() == "2")
                    self.assertEqual(report["placements"][equation["id"]], report["placements"][denominator["id"]])
                    self.assertNotIn("q", page.get_text(clip=pymupdf.Rect(70, 230, 260, 270)))
                    self.assertTrue(report["flow"])

    def test_isolated_numeric_labels_are_not_treated_as_equations(self):
        with pymupdf.open() as source, pymupdf.open() as overlays:
            page = source.new_page()
            page.insert_text((50, 100), "2")
            native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            block = {"id": "tick", "text": "2", "translation": "2", "_math_fragment": True}
            self.assertEqual(original_formula_units(page, overlays, [(block, pymupdf.Rect(native[0]["bbox"]))], native, []), [])

    def test_equations_and_labels_in_three_columns_are_never_merged(self):
        with pymupdf.open() as source, pymupdf.open() as overlays:
            page = source.new_page(width=600, height=700)
            for left, right in ((25, 175), (225, 375), (425, 575)):
                page.insert_text((left + 20, 120), "q = 1", fontsize=10)
                page.insert_text((right - 15, 120), "(1)", fontsize=10)
            native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            spans = [s for b in native for line in b["lines"] for s in line["spans"]]
            candidates = [({"id": str(i), "text": s["text"]}, pymupdf.Rect(s["bbox"])) for i, s in enumerate(spans)]
            units = original_formula_units(page, overlays, candidates, native, [],
                                           lanes=[(25, 175), (225, 375), (425, 575)])
            self.assertEqual(len(units), 3)
            self.assertTrue(all(pymupdf.Rect(result["clip"]).width < 150 for _, _, result in units))

    def test_formula_crop_never_copies_spans_owned_by_a_translation(self):
        with pymupdf.open() as source, pymupdf.open() as overlays:
            page = source.new_page()
            page.insert_text((50, 100), "q = 1", fontsize=10)
            native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            span = native[0]["lines"][0]["spans"][0]
            candidates = [({"id": "equation", "text": "q = 1"}, pymupdf.Rect(span["bbox"]))]
            self.assertEqual(original_formula_units(page, overlays, candidates, native, [],
                                                   occupied_spans=[span]), [])

    def test_font_padding_does_not_pin_a_formula_when_actual_strokes_are_separate(self):
        with pymupdf.open() as source, pymupdf.open() as overlays:
            page = source.new_page(width=400, height=300)
            page.insert_text((50, 100), "q = 1", fontname="tiro", fontsize=10)
            page.insert_text((50, 114), "Following prose remains unchanged.", fontname="tiro", fontsize=10)
            native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            raw_native = page.get_text("rawdict")["blocks"]
            span = next(s for b in native for line in b["lines"] for s in line["spans"] if s["text"] == "q = 1")
            candidates = [({"id": "equation", "text": "q = 1"}, pymupdf.Rect(span["bbox"]))]
            self.assertEqual(original_formula_units(page, overlays, candidates, native, []), [])
            units = original_formula_units(page, overlays, candidates, native, [],
                glyph_bounds=lambda spans: pdf_layout.formula_glyph_bounds(page, spans, raw_native))
            self.assertEqual(len(units), 1)
            self.assertLess(units[0][2]["clip"][3], 103.47)


class TypographyTests(unittest.TestCase):
    def test_fallback_when_no_local_cjk_font_is_available(self):
        with patch.object(pdf_typography, "font_source", return_value=None):
            resources = pdf_typography.font_resources("中文")
            self.assertEqual(resources["family"], "serif")
            self.assertIsNone(resources["archive"])

    def test_variable_font_uses_regular_and_semibold_not_thin_default(self):
        source = pdf_typography.font_source()
        if source is None:
            self.skipTest("当前系统没有可嵌入的中文字体")
        from fontTools.ttLib import TTFont
        regular, semibold = pdf_typography.subset_fonts("中文——3ABC", source[0], source[1])
        for data, weight in ((regular, 400), (semibold, 600)):
            with TTFont(io.BytesIO(data)) as font:
                self.assertNotIn("fvar", font)
                self.assertEqual(font["OS/2"].usWeightClass, weight)
                self.assertTrue(set(map(ord, "中文——3ABC")).issubset(font.getBestCmap()))

    def test_custom_font_keeps_dashes_and_digits_searchable(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            text = "模块化——SE(3)，参考[3]。"
            resources = pdf_typography.font_resources(text)
            spare, _ = page.insert_htmlbox(pymupdf.Rect(30, 30, 300, 100), text,
                                          css=resources["css"] + "body{font-family:" + resources["family"] + ";font-size:11pt}",
                                          archive=resources["archive"])
            self.assertGreaterEqual(spare, 0)
            normalize = lambda value: Counter(c for c in value if not c.isspace())
            self.assertEqual(normalize(page.get_text()), normalize(text))


if __name__ == "__main__":
    unittest.main()
