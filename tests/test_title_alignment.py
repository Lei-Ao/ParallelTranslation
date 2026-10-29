"""Title alignment follows source geometry, including independently extracted lines."""
import tempfile
import unittest
from pathlib import Path

import pymupdf

import pdf_layout
from pdf_typography import font_source

AUTHOR = "Ada Lovelace and Alan Turing"


class TitleAlignmentTests(unittest.TestCase):
    def render(self, *, width=612, height=792, rotation=0, font="tiro", size=24,
               split=True, centered=True, later_page=False, cropped=False, columns=1,
               wrapped=False, single=False, single_word=False, author=False):
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
            if author:
                page.insert_text((axis - 110, 65 + size * 2.6), AUTHOR,
                                 fontname="tiro", fontsize=max(7.0, size * .45))
            body = "The method is useful for the control of a robot. " * 8
            for column in range(columns):
                left = 40 + column * (width - 80) / columns
                right = 40 + (column + 1) * (width - 80) / columns - 12
                page.insert_textbox((left, 65 + size * 4, right, height - 60), body,
                                    fontname="tiro", fontsize=size / 2.4)
            native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            author_boxes = []
            for block in native:
                for line in block["lines"]:
                    text = "".join(s["text"] for s in line["spans"])
                    if text in titles:
                        title_boxes.append((text, pymupdf.Rect(line["bbox"])))
                    elif author and text == AUTHOR:
                        author_boxes.append(pymupdf.Rect(line["bbox"]))
            self.assertEqual(len(title_boxes), len(titles))
            if author:
                self.assertEqual(len(author_boxes), 1)
            page.set_rotation(rotation)
            translations = (("通用学习方法及其在多种条件下的控制和应用", "适用于变化条件并保持稳定")
                            if wrapped else ("通用学习与控制方法", "适用于变化条件"))
            saved = []
            for index, (text, box) in enumerate(title_boxes):
                displayed = box * page.rotation_matrix
                saved.append({"id": f"random-title-{index}", "text": text,
                              "translation": translations[index],
                              "target": "简体中文",
                              "bbox": [displayed.x0 / page.rect.width, displayed.y0 / page.rect.height,
                                       displayed.x1 / page.rect.width, displayed.y1 / page.rect.height]})
            if author:
                displayed = author_boxes[0] * page.rotation_matrix
                saved.append({"id": "author", "text": AUTHOR,
                              "translation": "阿达·洛芙莱斯和艾伦·图灵", "target": "简体中文",
                              "bbox": [displayed.x0 / page.rect.width, displayed.y0 / page.rect.height,
                                       displayed.x1 / page.rect.width, displayed.y1 / page.rect.height]})
            if not split:
                rect = pymupdf.Rect(saved[0]["bbox"]) | pymupdf.Rect(saved[1]["bbox"])
                saved = [{**saved[0], "text": "\n".join(titles),
                          "translation": "\n".join(translations), "bbox": list(rect)}]
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

    def title_spans(self, page, report, block_id):
        region = pdf_layout.source_rect(page, {"bbox": report["placements"][block_id]})
        return [span for block in page.get_text("dict", clip=region)["blocks"]
                if block.get("type") == 0 for line in block["lines"]
                for span in line["spans"] if span["text"].strip()]

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

    def test_split_title_lines_share_one_source_size_and_weight(self):
        for options in ({"font": "hebo"},
                        {"font": "hebo", "width": 420, "height": 595, "size": 18},
                        {"font": "hebo", "width": 842, "height": 595, "size": 28},
                        {"font": "tiro"}):
            with self.subTest(options=options):
                page, report, saved, _ = self.render(**options)
                self.assertFalse(report["issues"], report)
                self.assertEqual(report["replaced"], len(saved))
                # One title object, one placement: no line may be laid out on its own.
                self.assertEqual(report["placements"][saved[0]["id"]],
                                 report["placements"][saved[1]["id"]])
                spans = self.title_spans(page, report, saved[0]["id"])
                sizes = {round(span["size"], 1) for span in spans}
                self.assertEqual(len(sizes), 1, sizes)
                self.assertAlmostEqual(sizes.pop(), options.get("size", 24), delta=.3)
                if font_source() is not None:
                    bold = all(span["flags"] & 16 for span in spans)
                    if options.get("font") == "hebo":
                        self.assertTrue(bold, spans)
                    else:
                        self.assertFalse(bold, spans)
                for line in [line for block in page.get_text("dict", clip=pdf_layout.source_rect(
                        page, {"bbox": report["placements"][saved[0]["id"]]}))["blocks"]
                        if block.get("type") == 0 for line in block["lines"]]:
                    self.assertAlmostEqual((line["bbox"][0] + line["bbox"][2]) / 2,
                                           page.rect.width / 2, delta=1.2)

    def test_left_aligned_split_title_uses_one_size(self):
        page, report, saved, source_boxes = self.render(centered=False, font="hebo")
        self.assertFalse(report["issues"], report)
        self.assertEqual(report["placements"][saved[0]["id"]],
                         report["placements"][saved[1]["id"]])
        spans = self.title_spans(page, report, saved[0]["id"])
        self.assertEqual(len({round(span["size"], 1) for span in spans}), 1)
        self.assertAlmostEqual(min(span["bbox"][0] for span in spans),
                               min(box.x0 for _, box in source_boxes), delta=1.2)

    def test_title_group_does_not_merge_author_or_abstract(self):
        page, report, saved, _ = self.render(author=True, font="hebo")
        self.assertFalse(report["issues"], report)
        self.assertEqual(report["replaced"], 3)
        self.assertEqual(report["placements"]["random-title-0"],
                         report["placements"]["random-title-1"])
        self.assertNotEqual(report["placements"]["random-title-0"],
                            report["placements"]["author"])

    def test_display_title_keeps_bold_weight_with_prose_words(self):
        for split in (True, False):
            with self.subTest(split=split):
                page, report, saved, _ = self.render(font="hebo", split=split)
                self.assertFalse(report["issues"], report)
                if font_source() is not None:
                    spans = self.title_spans(page, report, saved[0]["id"])
                    self.assertTrue(all(span["flags"] & 16 for span in spans), spans)

    def test_single_line_title_uses_source_size(self):
        page, report, saved, _ = self.render(single=True, font="hebo")
        self.assertFalse(report["issues"], report)
        self.assertEqual(report["replaced"], 1)
        spans = self.title_spans(page, report, saved[0]["id"])
        sizes = {round(span["size"], 1) for span in spans}
        self.assertEqual(len(sizes), 1, sizes)
        self.assertAlmostEqual(sizes.pop(), 24, delta=.3)

    def test_small_caps_numbered_heading_merges_and_keeps_body_size(self):
        with tempfile.TemporaryDirectory() as temp, pymupdf.open() as source:
            source.new_page(width=612, height=792)  # The cover page; headings live on the next one.
            page = source.new_page(width=612, height=792)
            axis = page.rect.width / 2
            lines = [("IV. M", "ODEL-BASED META-LEARNING FOR", 70),
                     ("Q", "UADCOPTER PAYLOAD TRANSPORT", 84)]
            source_boxes = []
            for prefix, rest, baseline in lines:
                prefix_width = pymupdf.get_text_length(prefix, fontname="tiro", fontsize=10)
                rest_width = pymupdf.get_text_length(rest, fontname="tiro", fontsize=8)
                left = axis - (prefix_width + rest_width) / 2
                page.insert_text((left, baseline), prefix, fontname="tiro", fontsize=10)
                page.insert_text((left + prefix_width, baseline), rest, fontname="tiro", fontsize=8)
                source_boxes.append(pymupdf.Rect(left, baseline - 8, left + prefix_width + rest_width, baseline + 2.5))
            body = "The method is useful for the control of a robot. " * 6
            page.insert_textbox((60, 110, axis - 20, 210), body, fontname="tiro", fontsize=10)
            native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
            saved = []
            for index, translation in enumerate(("IV. 基于模型的元学习用于", "四旋翼飞行器载荷运输")):
                line = next(line for b in native for line in b["lines"]
                            if ("META-LEARNING" if index == 0 else "PAYLOAD TRANSPORT")
                            in "".join(s["text"] for s in line["spans"]))
                box = pymupdf.Rect(line["bbox"]) * page.rotation_matrix
                saved.append({"id": f"heading-{index}", "text": "".join(s["text"] for s in line["spans"]).strip(),
                              "translation": translation, "target": "简体中文",
                              "bbox": [box.x0 / page.rect.width, box.y0 / page.rect.height,
                                       box.x1 / page.rect.width, box.y1 / page.rect.height]})
            for block in native:
                text = " ".join("".join(s["text"] for s in line["spans"]).strip()
                                for line in block["lines"]).strip()
                if text.startswith("The method"):
                    box = pymupdf.Rect(block["bbox"]) * page.rotation_matrix
                    saved.append({"id": "body", "text": text, "translation": "正文段落需要保持原有字号。",
                                  "target": "简体中文",
                                  "bbox": [box.x0 / page.rect.width, box.y0 / page.rect.height,
                                           box.x1 / page.rect.width, box.y1 / page.rect.height]})
            self.assertEqual(len(saved), 3)
            page_number = len(source) - 1
            path = Path(temp) / "source.pdf"
            source.save(path)
            raw, report = pdf_layout.typeset(path, {"pages": [{"number": page_number + 1, "blocks": saved}]},
                                             "简体中文", page_numbers=[page_number])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["replaced"], 3)
        # Both heading halves share the merged overlay and stay on the heading axis.
        self.assertEqual(report["placements"]["heading-0"], report["placements"]["heading-1"])
        output = pymupdf.open(stream=raw, filetype="pdf")
        self.addCleanup(output.close)
        rendered = output[0]
        region = pdf_layout.source_rect(rendered, {"bbox": report["placements"]["heading-0"]})
        rows = [line for b in rendered.get_text("dict", clip=region)["blocks"]
                if b["type"] == 0 for line in b["lines"]]
        self.assertGreaterEqual(len(rows), 2)
        for row in rows:
            center = (row["bbox"][0] + row["bbox"][2]) / 2
            self.assertAlmostEqual(center, axis, delta=1.5)
        sizes = [s["size"] for b in rendered.get_text("dict", clip=region)["blocks"]
                 if b["type"] == 0 for line in b["lines"] for s in line["spans"] if s["text"].strip()]
        self.assertGreaterEqual(min(sizes), 9.5)

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
