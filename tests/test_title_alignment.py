"""Title alignment follows source geometry, including independently extracted lines."""
import tempfile
import unittest
from pathlib import Path

import pymupdf

import pdf_layout


class TitleAlignmentTests(unittest.TestCase):
    def render(self, *, width=612, height=792, rotation=0, font="tiro", size=24,
               split=True, centered=True, later_page=False, cropped=False, columns=1,
               wrapped=False, single=False, single_word=False):
        with tempfile.TemporaryDirectory() as temp, pymupdf.open() as source:
            if later_page:
                source.new_page(width=width, height=height)
            page = source.new_page(width=width, height=height)
            if cropped:
                page.set_cropbox(pymupdf.Rect(12, 10, width - 12, height - 10))
            width, height = page.rect.width, page.rect.height
            axis = width / 2
            titles = ["General Methods for Learning and Control", "Under Changing Conditions"]
            if single:
                titles = titles[:1]
            if single_word:
                titles = ["Robotics"]
            title_boxes = []
            for index, text in enumerate(titles):
                length = pymupdf.get_text_length(text, fontname=font, fontsize=size)
                left = axis - length / 2 if centered else 40
                page.insert_text((left, 65 + size * 1.5 * index), text, fontname=font, fontsize=size)
            body = "The method is useful for the control of a robot. " * 8
            for column in range(columns):
                left = 40 + column * (width - 80) / columns
                right = 40 + (column + 1) * (width - 80) / columns - 12
                page.insert_textbox((left, 65 + size * 4, right, height - 60), body,
                                    fontname="tiro", fontsize=size / 2.4)
            native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            for block in native:
                for line in block["lines"]:
                    text = "".join(s["text"] for s in line["spans"])
                    if text in titles:
                        title_boxes.append((text, pymupdf.Rect(line["bbox"])))
            self.assertEqual(len(title_boxes), len(titles))
            page.set_rotation(rotation)
            saved = []
            for index, (text, box) in enumerate(title_boxes):
                displayed = box * page.rotation_matrix
                saved.append({"id": f"random-title-{index}", "text": text,
                              "translation": ("通用学习方法及其在多种条件下的控制和应用" if wrapped
                                              else ("通用学习与控制方法", "适用于变化条件")[index]),
                              "target": "简体中文",
                              "bbox": [displayed.x0 / page.rect.width, displayed.y0 / page.rect.height,
                                       displayed.x1 / page.rect.width, displayed.y1 / page.rect.height]})
            if not split:
                rect = pymupdf.Rect(saved[0]["bbox"]) | pymupdf.Rect(saved[1]["bbox"])
                saved = [{**saved[0], "text": "\n".join(titles), "translation": "通用学习与控制方法\n适用于变化条件",
                          "bbox": list(rect)}]
            page_number = len(source) - 1
            path = Path(temp) / "source.pdf"
            source.save(path)
            document = {"pages": [{"number": page_number + 1, "blocks": saved}]}
            raw, report = pdf_layout.typeset(path, document, "简体中文", page_numbers=[page_number])
        output = pymupdf.open(stream=raw, filetype="pdf")
        self.addCleanup(output.close)
        return output[0], report, saved, title_boxes

    def assert_centered(self, page, report, saved, source_boxes):
        self.assertFalse(report["issues"], report)
        self.assertEqual(report["replaced"], len(saved))
        expected_axis = (source_boxes[0][1].x0 + source_boxes[0][1].x1) / 2
        for block in saved:
            region = pdf_layout.source_rect(page, {"bbox": report["placements"][block["id"]]})
            rows = [line for b in page.get_text("dict", clip=region)["blocks"]
                    if b["type"] == 0 for line in b["lines"]]
            self.assertTrue(rows)
            for row in rows:
                self.assertAlmostEqual((row["bbox"][0] + row["bbox"][2]) / 2, expected_axis, delta=1.2)

    def test_split_regular_and_bold_titles_remain_centered(self):
        for font in ("tiro", "hebo"):
            with self.subTest(font=font):
                self.assert_centered(*self.render(font=font))

    def test_alignment_survives_page_sizes_rotation_crop_and_later_pages(self):
        for width, height, size in ((612, 792, 24), (420, 595, 14), (842, 595, 28), (300, 450, 10)):
            for rotation in (0, 90, 180, 270):
                with self.subTest(width=width, rotation=rotation):
                    self.assert_centered(*self.render(width=width, height=height, size=size,
                                                     rotation=rotation, later_page=True, cropped=True, columns=2))

    def test_combined_multiline_and_wrapped_translation_remain_centered(self):
        for options in ({"single": True}, {"single_word": True}, {"split": False},
                        {"wrapped": True, "size": 18, "width": 500}):
            with self.subTest(options=options):
                self.assert_centered(*self.render(**options))

    def test_left_aligned_titles_keep_their_source_edge(self):
        for font in ("tiro", "hebo"):
            page, report, saved, source_boxes = self.render(centered=False, font=font)
            self.assertFalse(report["issues"], report)
            for block, (_, source) in zip(saved, source_boxes):
                region = pdf_layout.source_rect(page, {"bbox": report["placements"][block["id"]]})
                lines = [line for b in page.get_text("dict", clip=region)["blocks"]
                         if b["type"] == 0 for line in b["lines"]]
                self.assertAlmostEqual(lines[0]["bbox"][0], source.x0, delta=1.2)

    def test_full_first_line_of_left_title_is_not_centered(self):
        def span(text, box, size):
            return {"text": text, "bbox": box, "size": size, "origin": (box[0], box[1] + size)}
        first = span("A long title filling its available width", [40, 50, 560, 74], 24)
        following = span("A shorter continuation", [40, 86, 300, 110], 24)
        body = span("The method is useful for the control of a robot. " * 10, [40, 150, 560, 162], 10)
        for current in (first, following):
            style = {"size": 24, "spans": [current], "lines": [{"spans": [current]}]}
            self.assertEqual(pdf_layout.source_paragraph_style(
                style, pymupdf.Rect(current["bbox"]), current["text"], 600, [first, following, body])[0], "left")

    def test_single_body_line_with_small_print_is_not_a_display_title(self):
        body = {"text": "A brief body sentence aligned to the left.", "bbox": [100, 100, 500, 110],
                "size": 10, "origin": (100, 110)}
        footnote = {"text": "A small footnote", "bbox": [100, 300, 200, 307], "size": 7, "origin": (100, 307)}
        style = {"size": 10, "spans": [body], "lines": [{"spans": [body]}]}
        self.assertEqual(pdf_layout.source_paragraph_style(
            style, pymupdf.Rect(body["bbox"]), body["text"], 600, [body, footnote])[0], "left")

    def test_display_heading_uses_column_axis_and_keeps_formula_alignment(self):
        body = {"text": "The method is useful for the control of a robot. " * 10,
                "bbox": [40, 150, 280, 162], "size": 10, "origin": (40, 160)}
        for text, expected in (("A Column Heading", "center"), ("velocity = acceleration", "left")):
            title = {"text": text, "bbox": [100, 80, 220, 98], "size": 18, "origin": (100, 98)}
            style = {"size": 18, "spans": [title], "lines": [{"spans": [title]}]}
            self.assertEqual(pdf_layout.source_paragraph_style(
                style, pymupdf.Rect(title["bbox"]), text, 600, [title, body])[0], expected)


if __name__ == "__main__":
    unittest.main()
