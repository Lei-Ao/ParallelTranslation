"""Source geometry and readable typography must survive translation."""
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pymupdf
import app as service
import pdf_layout
from pdf_flow import reflow_columns

PROSE = "The method is useful for the control of a robot."


class ParagraphSpacingTests(unittest.TestCase):
    def style(self, boxes, size=10):
        return {"size": size, "lines": [
            {"spans": [{"bbox": box, "origin": (box[0], box[1] + size),
                         "size": size, "text": PROSE}]} for box in boxes]}

    def test_heading_and_body_move_together_when_fixed_content_is_below(self):
        def item(identifier, text, rect):
            return ({"id": identifier, "text": text},
                    {"size": 10, "lines": [], "spans": []}, {"rect": list(rect)})
        source_rects = {"a": pymupdf.Rect(300, 100, 560, 200),
                        "h": pymupdf.Rect(300, 207, 420, 219),
                        "b": pymupdf.Rect(300, 224, 560, 300)}
        prepared = [item("a", PROSE, pymupdf.Rect(300, 100, 560, 150)),
                    item("h", "A. Data Collection", pymupdf.Rect(300, 207, 420, 219)),
                    item("b", PROSE, pymupdf.Rect(300, 224, 560, 300))]
        # A preserved formula lies inside the body's old box but below the new
        # destination: it must not block pulling the body up under its heading.
        adjustments = reflow_columns(prepared, [], [pymupdf.Rect(300, 285, 560, 295)],
                                     source_rects, 600, pdf_layout.is_prose)
        self.assertTrue(adjustments)
        heading, body = prepared[1][2]["rect"], prepared[2][2]["rect"]
        self.assertLess(body[1], 224)
        self.assertAlmostEqual(body[1], heading[3] + 5.0, places=2)

        # Content that really sits between the old and the new position still
        # blocks the move.
        prepared = [item("a", PROSE, pymupdf.Rect(300, 100, 560, 150)),
                    item("h", "A. Data Collection", pymupdf.Rect(300, 207, 420, 219)),
                    item("b", PROSE, pymupdf.Rect(300, 224, 560, 300))]
        reflow_columns(prepared, [], [pymupdf.Rect(300, 180, 560, 195)],
                       source_rects, 600, pdf_layout.is_prose)
        self.assertEqual(prepared[2][2]["rect"], [300, 224, 560, 300])

    def test_full_width_two_line_body_is_not_centered(self):
        rect = pymupdf.Rect(100, 100, 500, 123)
        style = self.style([[100, 100, 500, 110], [100, 113, 498, 123]])
        alignment, indent, leading = pdf_layout.source_paragraph_style(style, rect, PROSE, 600)
        self.assertEqual(alignment, "justify")
        self.assertEqual(indent, 0)
        self.assertAlmostEqual(leading, 1.3)

    def test_centered_lines_require_shared_axis_and_different_widths(self):
        style = self.style([[100, 100, 500, 110], [180, 115, 420, 125]])
        self.assertEqual(pdf_layout.source_paragraph_style(
            style, pymupdf.Rect(100, 100, 500, 125), "A centered heading")[0], "center")
        style = self.style([[100, 100, 500, 110], [100, 115, 350, 125]])
        self.assertNotEqual(pdf_layout.source_paragraph_style(
            style, pymupdf.Rect(100, 100, 500, 125), PROSE)[0], "center")

    def test_first_line_indent_and_leading_scale_with_source_font(self):
        for size in (7, 10, 18):
            boxes = [[40 + size, 50, 260, 50 + size],
                     [40, 50 + size * 1.5, 260, 50 + size * 2.5],
                     [40, 50 + size * 3, 180, 50 + size * 4]]
            alignment, indent, leading = pdf_layout.source_paragraph_style(
                self.style(boxes, size), pymupdf.Rect(40, 50, 260, 50 + size * 4), PROSE)
            self.assertEqual(alignment, "justify")
            self.assertAlmostEqual(indent, size)
            self.assertAlmostEqual(leading, 1.5)

    def test_inline_fragments_and_superscripts_do_not_create_false_lines(self):
        style = self.style([[40, 50, 90, 60], [100, 50, 260, 60], [40, 65, 260, 75]])
        style["lines"].insert(1, {"spans": [{"text": "2", "size": 6,
                                               "bbox": [90, 47, 94, 53], "origin": (90, 53)}]})
        self.assertAlmostEqual(pdf_layout.source_paragraph_style(
            style, pymupdf.Rect(40, 47, 260, 75), PROSE)[2], 1.5)

    def test_single_line_body_on_page_axis_remains_left_aligned(self):
        style = self.style([[100, 100, 500, 110]])
        self.assertEqual(pdf_layout.source_paragraph_style(
            style, pymupdf.Rect(100, 100, 500, 110), PROSE, 600)[0], "left")
        style = self.style([[250, 100, 350, 110]])
        self.assertEqual(pdf_layout.source_paragraph_style(
            style, pymupdf.Rect(250, 100, 350, 110), "III. METHOD", 600)[0], "center")

    def test_only_short_strokes_inside_inline_math_are_owned(self):
        run = {"rect": pymupdf.Rect(70, 200, 100, 215), "chars": [{"size": 10}]}
        short = {"rect": pymupdf.Rect(80, 212, 83, 212), "type": "s", "items": [("l",)]}
        rule = {**short, "rect": pymupdf.Rect(70, 212, 100, 212)}
        foreign = {**short, "rect": pymupdf.Rect(80, 180, 83, 180)}
        self.assertEqual(pdf_layout.inline_math_graphics([short, rule, foreign], [run]), [tuple(short["rect"])])

    def test_columns_and_small_print_keep_their_source_positions(self):
        for columns in ([(40, 560)], [(40, 270), (330, 560)],
                        [(25, 175), (225, 375), (425, 575)]):
            for size in (7, 10, 12):
                with self.subTest(columns=len(columns), size=size), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    with patch.object(service, "DATA", root), pymupdf.open() as pdf:
                        page = pdf.new_page(width=600, height=700)
                        for left, _ in columns:
                            for top in (60, 240):
                                page.insert_text((left, top), "\n".join(["The method is useful."] * 8),
                                                 fontname="tiro", fontsize=size, lineheight=1.3)
                        doc_id, document = service.extract_document(pdf.tobytes(), "columns.pdf")
                        blocks = document["pages"][0]["blocks"]
                        self.assertEqual(len(blocks), len(columns) * 2)
                        before = [pdf_layout.source_rect(page, b) for b in blocks]
                        for block in blocks:
                            block.update(translation="译文保留原文的段落位置。", target="简体中文")
                        raw, report = pdf_layout.typeset(root / doc_id / "source.pdf", document, "简体中文")
                        self.assertEqual(report["replaced"], len(blocks), report)
                        with pymupdf.open(stream=raw, filetype="pdf") as result:
                            for block, source in zip(blocks, before):
                                placed = pdf_layout.source_rect(result[0], {"bbox": report["placements"][block["id"]]})
                                self.assertAlmostEqual(placed.x0, source.x0, places=2)
                                self.assertLess(abs(placed.y0 - source.y0), 5)

    def test_safe_expansion_is_tried_before_reducing_font_size(self):
        with pymupdf.open() as source, pymupdf.open() as overlays:
            page = source.new_page(width=400, height=400)
            rect = pymupdf.Rect(40, 40, 260, 57)
            page.insert_text((40, 51), "An explanation of the method.", fontsize=10)
            native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            style, reason = pdf_layout.inspect_block(native, rect)
            self.assertIsNone(reason)
            result = pdf_layout.fitted_overlay(overlays, page,
                {"text": "An explanation of the method.", "translation": "这是用于验证排版字号不被无谓缩小的中文说明。" * 2},
                rect, style, [])
            self.assertIsNotNone(result)
            self.assertEqual(result["scale"], 1)
            self.assertGreater(result["rect"][3], rect.y1)

    def test_short_translations_retain_source_gaps_and_intentional_whitespace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for rotation in (0, 90, 180, 270):
                with self.subTest(rotation=rotation), patch.object(service, "DATA", root):
                    with pymupdf.open() as source:
                        page = source.new_page(width=600, height=600)
                        page.insert_text((40, 60), "\n".join([PROSE] * 10), fontname="tiro", fontsize=10, lineheight=1.2)
                        page.insert_text((100, 190), "I. INTRODUCTION", fontname="tiro", fontsize=8)
                        page.insert_text((40, 213), "\n".join([PROSE] * 8), fontname="tiro", fontsize=10, lineheight=1.2)
                        page.insert_text((100, 340), "q = 1", fontsize=10)
                        page.set_rotation(rotation)
                        raw = source.tobytes()
                    doc_id, document = service.extract_document(raw, "spacing.pdf")
                    blocks = document["pages"][0]["blocks"]
                    translations = ["第一段译文用于验证原文位置与留白。" * 3, "一、引言",
                                    "第二段译文保持在原段落的位置。" * 2, "q = 1"]
                    self.assertEqual(len(blocks), len(translations))
                    for block, translation in zip(blocks, translations):
                        block.update(translation=translation, target="简体中文")
                    with pymupdf.open(stream=raw, filetype="pdf") as original:
                        before = [pdf_layout.source_rect(original[0], block) for block in blocks]
                        original[0].set_rotation(0)
                        source_ink = [pdf_layout.text_ink_bounds(original[0].get_bboxlog(), box)
                                      for box in before]
                        formula_pixels = original[0].get_pixmap(clip=pymupdf.Rect(95, 325, 140, 350)).samples
                    output, report = pdf_layout.typeset(root / doc_id / "source.pdf", document, "简体中文")
                    self.assertEqual(report["replaced"], 4, report)
                    with pymupdf.open(stream=output, filetype="pdf") as result:
                        page = result[0]
                        self.assertEqual(page.rotation, rotation)
                        expected = Counter(c for c in "".join(translations) if not c.isspace())
                        visible = Counter(c for c in page.get_text() if not c.isspace())
                        self.assertFalse(expected - visible)
                        positions = [pdf_layout.source_rect(page, {"bbox": report["placements"][b["id"]]}) for b in blocks[:3]]
                        self.assertLess(abs(positions[0].y0 - before[0].y0), 5)
                        for actual, source in zip(positions, before):
                            self.assertLessEqual(actual.y0, source.y0 + 5)
                            self.assertAlmostEqual(actual.x0, source.x0, places=2)
                        page.set_rotation(0)
                        translated_ink = [pdf_layout.text_ink_bounds(page.get_bboxlog(), box)
                                          for box in positions]
                        for index in (1, 2):
                            expected_gap = max(2, before[index].y0 - before[index - 1].y1)
                            if all(box is not None for box in source_ink[index-1:index+1]
                                   + translated_ink[index-1:index+1]):
                                # Compare visible strokes when both fonts can
                                # be measured, rather than empty font frames.
                                visible_gap = source_ink[index].y0 - source_ink[index-1].y1
                                padding = (positions[index-1].y1 - translated_ink[index-1].y1
                                           + translated_ink[index].y0 - positions[index].y0)
                                expected_gap = max(2, visible_gap - padding)
                            self.assertAlmostEqual(positions[index].y0 - positions[index - 1].y1,
                                                   expected_gap, delta=.01)
                        self.assertGreaterEqual(len(report["flow"]), 2)
                        page.set_rotation(0)
                        self.assertEqual(page.get_pixmap(clip=pymupdf.Rect(95, 325, 140, 350)).samples, formula_pixels)

    def flow_fixture(self, lanes=((40, 260),), size=10, source_gap=10):
        prepared, native, rects = [], [], {}
        for column, (left, right) in enumerate(lanes):
            for row, (top, height, ink_height) in enumerate(((50, 100, 30), (150 + source_gap, 90, 25))):
                name = f"{column}-{row}"
                box = [left, top, right, top + height]
                span = {"bbox": box, "text": PROSE}
                prepared.append(({"id": name, "text": PROSE},
                                 {"size": size, "spans": [span]},
                                 {"rect": [left, top, right, top + ink_height],
                                  "clip": [left, top, right, top + ink_height]}))
                native.append({"lines": [{"spans": [span]}]})
                rects[name] = pymupdf.Rect(box)
        return prepared, native, rects

    def test_reflow_uses_measured_source_spacing_in_every_column_and_font_size(self):
        for lanes in (((40, 560),), ((40, 260), (330, 550)),
                      ((25, 175), (225, 375), (425, 575))):
            for size in (7, 10, 18):
                for gap in (0, 5, 15):
                    with self.subTest(columns=len(lanes), size=size, gap=gap):
                        prepared, native, rects = self.flow_fixture(lanes, size, gap)
                        prepared.reverse()
                        changes = reflow_columns(prepared, native, [], rects, 600, pdf_layout.is_prose)
                        self.assertEqual(len(changes), len(lanes))
                        for change in changes:
                            self.assertAlmostEqual(change["gap"], max(gap, 1.5, size * .2))

    def test_artwork_and_untranslated_text_prevent_reflow_across_them(self):
        for mode in ("image", "rule", "source_gap", "untranslated", "overlay"):
            with self.subTest(mode=mode):
                prepared, native, rects = self.flow_fixture(source_gap=50 if mode == "source_gap" else 10)
                graphics = []
                if mode == "image":
                    graphics.append(pymupdf.Rect(50, 100, 250, 120))
                elif mode == "rule":
                    graphics.append(pymupdf.Rect(50, 110, 250, 110))
                elif mode == "untranslated":
                    native.append({"lines": [{"spans": [{"text": "Untranslated text", "bbox": [50, 100, 250, 120]}]}]})
                elif mode == "overlay":
                    prepared.append(({"id": "label", "text": "x"}, {"size": 10, "spans": []},
                                     {"rect": [50, 100, 250, 120]}))
                before = prepared[1][2]["rect"][:]
                self.assertFalse(reflow_columns(prepared, native, graphics, rects, 600, pdf_layout.is_prose))
                self.assertEqual(prepared[1][2]["rect"], before)

    def test_headings_stay_with_fixed_following_content(self):
        prepared, native, rects = self.flow_fixture()
        prepared[1][0]["text"] = "III. METHOD"
        rects["0-1"].y1 = 172
        prepared[1][2]["rect"] = [100, 160, 200, 172]
        native.append({"lines": [{"spans": [{"text": PROSE, "bbox": [40, 180, 260, 210]}]}]})
        self.assertFalse(reflow_columns(prepared, native, [], rects, 600, pdf_layout.is_prose))
        self.assertEqual(prepared[1][2]["rect"][1], 160)


if __name__ == "__main__":
    unittest.main()
