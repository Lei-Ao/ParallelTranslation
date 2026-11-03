"""Whole-figure label preservation across page geometry and text structure."""
import copy
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

import app as service
import pdf_layout
from pdf_figures import figure_regions


def native_text(page):
    return [block for block in page.get_text(
        "dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
            if block["type"] == 0]


def separate_space_spans(page, point, text, size=10):
    """Paint real whitespace spans, as used by some journal PDF producers."""
    left, baseline = point
    for piece in re.split(r"(\s+)", text):
        if not piece:
            continue
        font = "cour" if piece.isspace() else "tiro"
        page.insert_text((left, baseline), piece, fontname=font, fontsize=size)
        left += pymupdf.get_text_length(piece, fontname=font, fontsize=size)


def diagram(page, box, size=8, background=True):
    box = pymupdf.Rect(box)
    step = box.width / 3
    if background:
        for index, color in enumerate(((.8, .9, 1), (1, .85, .7), (1, .8, .85))):
            page.draw_rect((box.x0 + step * index, box.y0, box.x0 + step * (index + 1), box.y1),
                           color=None, fill=color)
    for row in range(2):
        y = box.y0 + size * (2.5 + row * 4)
        for index, label in enumerate(("Visual policy", "Motion control", "Robot action")):
            left = box.x0 + step * index + size
            node = pymupdf.Rect(left, y, left + step - size * 2, y + size * 2)
            page.draw_rect(node, fill=(.9, .8, .9), color=(0, 0, 0))
            page.insert_text((left + 2, y + size * 1.4), label, fontsize=size * .65)
            if index < 2:
                page.draw_line((node.x1, y + size), (node.x1 + size * 2, y + size))
        if row == 0:
            page.draw_line((box.x0 + step * 1.5, y + size * 2),
                           (box.x0 + step * 1.5, y + size * 4))
    return box


class FigureRegionTests(unittest.TestCase):
    def test_caption_anchored_diagram_and_all_horizontal_labels_are_inside(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=700)
            art = diagram(page, (40, 70, 560, 230))
            page.insert_text((40, 246), "Figure 1: A modular control architecture.", fontsize=10)
            page.insert_text((40, 280), "The surrounding text remains a separate paragraph.", fontsize=10)
            regions = figure_regions(page, native_text(page))
            self.assertEqual(len(regions), 1)
            self.assertTrue(regions[0].contains(art))
            for block in native_text(page):
                center = (pymupdf.Rect(block["bbox"]).tl + pymupdf.Rect(block["bbox"]).br) * .5
                text = " ".join(s["text"] for line in block["lines"] for s in line["spans"])
                self.assertEqual(any(region.contains(center) for region in regions),
                                 "Visual policy" in text)

    def test_no_caption_requires_connected_artwork_and_independent_labels(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=700)
            art = diagram(page, (40, 70, 560, 210), background=False)
            regions = figure_regions(page, native_text(page))
            self.assertEqual(len(regions), 1)
            self.assertTrue(regions[0].intersects(art))

    def test_plain_shading_page_background_and_display_formula_are_not_figures(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=700)
            page.draw_rect(page.rect, fill=(1, 1, .95))
            page.draw_rect((30, 50, 570, 170), fill=(.9, .9, 1))
            page.insert_text((40, 75), "This is a highlighted paragraph with its ordinary source text.", fontsize=10)
            page.insert_text((40, 90), "The text is not a diagram label and should still be translated.", fontsize=10)
            page.insert_text((40, 105), "Its paragraph background must not suppress the translation.", fontsize=10)
            page.draw_line((200, 220), (240, 220))
            page.insert_text((200, 215), "q = 1", fontsize=10)
            page.insert_text((200, 232), "2", fontsize=8)
            self.assertEqual(figure_regions(page, native_text(page)), [])

    def test_table_caption_excludes_connected_cell_grid(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=500, height=500)
            for row in range(4):
                for column in range(3):
                    box = pymupdf.Rect(40 + column * 100, 80 + row * 22,
                                       140 + column * 100, 102 + row * 22)
                    page.draw_rect(box)
                    page.insert_text((box.x0 + 4, box.y0 + 15), "Control method", fontsize=8)
            page.insert_text((40, 190), "Table 1: Comparison of control methods.", fontsize=10)
            self.assertEqual(figure_regions(page, native_text(page)), [])

    def test_clipped_hidden_artwork_does_not_cover_other_column(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=600)
            page.draw_rect((-100, -100, 700, 300), fill=(.8, .9, 1))
            xref = page.get_contents()[0]
            content = pdf.xref_stream(xref)
            pdf.update_stream(xref, b"q\n40 360 220 200 re W n\n" + content + b"\nQ")
            page.insert_text((40, 260), "Fig. 2: A clipped illustration.", fontsize=9)
            regions = figure_regions(page, native_text(page))
            self.assertEqual(len(regions), 1)
            self.assertLessEqual(regions[0].x1, 260.01)

    def test_disconnected_panels_include_shared_legend_axis_labels_and_subcaptions(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=600)
            page.draw_rect((70, 50, 530, 62))
            for x, label in ((80, "Baseline"), (200, "Controller"), (330, "Policy"), (450, "Ours")):
                page.insert_text((x, 59), label, fontsize=7)
            for x in (60, 330):
                for y in (80, 135):
                    page.draw_rect((x, y, x + 200, y + 45))
                    page.draw_line((x + 5, y + 35), (x + 190, y + 8))
                page.insert_text((x, 191), "0 1 2 3 4 time (s)", fontsize=7)
            page.insert_text((60, 205), "(a) Single trial", fontsize=8)
            page.insert_text((330, 205), "(b) Parallel trials", fontsize=8)
            page.insert_text((40, 230), "Figure 1: Comparison of the results.", fontsize=9)
            page.insert_text((40, 275), "The body of the document continues below the comparison.", fontsize=10)
            regions = figure_regions(page, native_text(page))
            self.assertEqual(len(regions), 1)
            self.assertTrue(regions[0].contains(pymupdf.Rect(60, 50, 530, 205)))
            self.assertLess(regions[0].y1, 215)

    def test_disconnected_model_cards_and_external_labels_share_one_figure_band(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=600)
            for x in (60, 220, 380):
                page.draw_rect((x, 60, x + 120, 125), fill=(.8, .9, 1))
                page.insert_text((x + 5, 132), "model-file.urdf", fontsize=8)
            page.draw_rect((150, 145, 420, 220))
            page.insert_text((140, 143), "Force", fontsize=8)
            page.insert_text((390, 229), "World axis", fontsize=8)
            page.insert_text((40, 255), "Fig. 3: The models and their dynamics.", fontsize=9)
            regions = figure_regions(page, native_text(page))
            self.assertEqual(len(regions), 1)
            for block in native_text(page):
                text = " ".join(s["text"] for line in block["lines"] for s in line["spans"])
                center = (pymupdf.Rect(block["bbox"]).tl + pymupdf.Rect(block["bbox"]).br) * .5
                self.assertEqual(regions[0].contains(center), not text.startswith("Fig."))

    def test_large_unnumbered_heading_and_body_remain_translatable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(service, "DATA", root), pymupdf.open() as pdf:
                page = pdf.new_page(width=600, height=600)
                page.insert_text((40, 78), "Results Overview", fontsize=18)
                art = diagram(page, (40, 100, 560, 260), size=8)
                page.insert_text((40, 282), "Figure 1: A modular control system.", fontsize=9)
                page.insert_text((40, 315), "The text below the diagram is an ordinary paragraph.", fontsize=10)
                regions = figure_regions(page, native_text(page))
                self.assertEqual(len(regions), 1)
                self.assertGreaterEqual(regions[0].y0, art.y0)
                doc_id, document = service.extract_document(pdf.tobytes(), "display.pdf")
                for block in document["pages"][0]["blocks"]:
                    value = block["text"]
                    translated = ("结果概览" if value.startswith("Results") else
                                  "正文应正常翻译。" if value.startswith("The text") else
                                  "图1：控制系统。" if value.startswith("Figure") else "不得替换图内标签")
                    block.update(translation=translated, target="简体中文")
                raw, _ = pdf_layout.typeset(root / doc_id / "source.pdf", document, "简体中文")
                with pymupdf.open(stream=raw, filetype="pdf") as output:
                    text = output[0].get_text()
                    self.assertIn("结果概览", text)
                    self.assertIn("正文应正常翻译", text)
                    self.assertIn("图1", text)
                    self.assertNotIn("不得替换", text)

    def test_whitespace_only_source_spans_keep_body_paragraph_outside_figure(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=500, height=650)
            for y, value in ((340, "The gain matrices are different for the"),
                             (354, "upright and inverted flight states to"),
                             (368, "improve the tracking performance."),
                             (382, "The control input is then calculated.")):
                separate_space_spans(page, (40, y), value, size=9)
            page.draw_rect((40, 400, 300, 520), fill=(.8, .9, 1))
            page.insert_text((60, 430), "Robot policy", fontsize=8)
            page.insert_text((40, 545), "Figure 1: The coordinate system.", fontsize=9)
            native = native_text(page)
            self.assertTrue(any(span["text"].isspace() for block in native
                                for line in block["lines"] for span in line["spans"]))
            regions = figure_regions(page, native)
            self.assertEqual(regions, [pymupdf.Rect(40, 400, 300, 520)])

    def test_numbered_equation_and_short_explanation_above_figure_are_not_labels(self):
        for numbered in (True, False):
            with self.subTest(numbered=numbered), pymupdf.open() as pdf:
                page = pdf.new_page(width=500, height=650)
                separate_space_spans(page, (40, 355 if numbered else 390), "where k is constant.", size=10)
                if numbered:
                    page.insert_text((110, 380), "f = k x", fontsize=10)
                    page.insert_text((280, 380), "(4)", fontsize=10)
                page.draw_rect((40, 400, 310, 520), fill=(.8, .9, 1))
                page.insert_text((60, 430), "Robot policy", fontsize=8)
                page.insert_text((40, 545), "Figure 1: The control architecture.", fontsize=9)
                regions = figure_regions(page, native_text(page))
                self.assertEqual(regions, [pymupdf.Rect(40, 400, 310, 520)])

    def test_multiple_columns_sizes_rotations_crop_and_preserved_pixels(self):
        cases = ((1, 7, 0, False), (2, 9, 90, True),
                 (3, 11, 180, False), (2, 8, 270, True))
        for columns, size, rotation, cropped in cases:
            with self.subTest(columns=columns, rotation=rotation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.object(service, "DATA", root), pymupdf.open() as pdf:
                    page = pdf.new_page(width=columns * 320 + 40, height=650)
                    artworks = []
                    for index in range(columns):
                        left = 30 + index * 320
                        art = diagram(page, (left, 70, left + 280, 250), size=size)
                        artworks.append(art)
                        page.insert_text((left, 270), f"Figure {index + 1}: A control system.", fontsize=size)
                        page.insert_text((left, 320), "Ordinary text below the figure remains translatable.", fontsize=size)
                    if cropped:
                        page.set_cropbox((10, 20, page.rect.width - 10, 620))
                    page.set_rotation(rotation)
                    doc_id, document = service.extract_document(pdf.tobytes(), "geometry.pdf")
                    for block in document["pages"][0]["blocks"]:
                        caption = block["text"].startswith("Figure")
                        body = block["text"].startswith("Ordinary")
                        translation = "图：控制系统的说明。" if caption else "图外正文正常翻译。" if body else "不得替换图内标签"
                        block.update(translation=translation, target="简体中文")
                    saved = copy.deepcopy(document)
                    raw, report = pdf_layout.typeset(root / doc_id / "source.pdf", document, "简体中文")
                    self.assertEqual(document, saved)
                    self.assertTrue(any(issue["kind"] == "figure" for issue in report["issues"]), report)
                    with pymupdf.open(stream=raw, filetype="pdf") as output, pymupdf.open(root / doc_id / "source.pdf") as source:
                        self.assertEqual(output[0].rotation, rotation)
                        output[0].set_rotation(0)
                        source[0].set_rotation(0)
                        text = output[0].get_text()
                        self.assertNotIn("不得替换", text)
                        self.assertIn("图外正文正常翻译", text)
                        self.assertIn("控制系统的说明", text)
                        for art in artworks:
                            if cropped:
                                art = art + (-10, -20, -10, -20)
                            self.assertEqual(output[0].get_pixmap(clip=art).samples,
                                             source[0].get_pixmap(clip=art).samples)


if __name__ == "__main__":
    unittest.main()
