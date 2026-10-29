import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient

import app as service
from pdf_layout import build_layout
import pdf_layout


def layout_fixture(rotation=0, cropped=False):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=595, height=842)
        page.insert_text((55, 70), "Reading across languages", fontsize=22, fontname="hebo", color=(.1, .35, .28))
        page.insert_textbox((55, 110, 282, 200), "An agent uses context to translate a document.\nTerminology stays consistent across paragraphs.", fontsize=11)
        page.insert_textbox((315, 110, 540, 200), "The original figures and page layout are preserved.\nTranslated text remains searchable and selectable.", fontsize=11)
        page.draw_rect((55, 240, 282, 350), color=(.1, .5, .3), fill=(.9, .96, .9))
        page.insert_text((70, 275), "Figure 1: Original vector artwork", fontsize=10)
        page.insert_text((340, 210), "E = mc2", fontsize=18)
        image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 32), False)
        image.set_rect(image.irect, (60, 120, 190))
        page.insert_image(pymupdf.Rect(340, 330, 440, 430), stream=image.tobytes("png"))
        page.insert_text((55, 500), "A short note.", fontsize=11)
        if cropped:
            page.set_cropbox(pymupdf.Rect(20, 20, 575, 820))
        page.set_rotation(rotation)
        pdf.new_page(width=595, height=842)
        return pdf.tobytes()


class LayoutTests(unittest.TestCase):
    def native_block(self, text, box, font="Times-Roman"):
        return {"bbox": box, "lines": [{"bbox": box, "dir": (1, 0), "spans": [
            {"bbox": box, "text": text, "font": font, "size": 11, "color": 0, "flags": 0}]}]}

    def test_inline_math_font_does_not_suppress_prose_or_captions(self):
        native = self.native_block("The mechanism rotates through an angle of 40 degrees in the experiment.", [50, 50, 280, 100])
        native["lines"][0]["spans"].append({"bbox": [90, 50, 95, 60], "text": "◦", "font": "STIXTwoMath", "size": 11, "color": 0, "flags": 0})
        style, reason = pdf_layout.inspect_block([native], pymupdf.Rect(50, 50, 280, 100))
        self.assertIsNone(reason)
        self.assertIsNotNone(style)
        equation = self.native_block("2L sin θ ≤ D", [50, 50, 280, 100], "STIXTwoMath")
        self.assertIn("公式", pdf_layout.inspect_block([equation], pymupdf.Rect(50, 50, 280, 100))[1])

    def test_small_caps_heading_measures_nominal_size(self):
        def span(text, box, size):
            return {"bbox": box, "text": text, "font": "NimbusRomNo9L-Regu", "size": size, "color": 0, "flags": 0}
        caps_line = {"bbox": [50, 50, 265, 62], "dir": (1, 0), "spans": [
            span("IV. M", [50, 50, 72, 62], 10), span("ODEL-BASED", [72, 51.5, 130, 61], 8),
            span(" FOR", [130, 51.5, 160, 61], 8)]}
        native = [{"type": 0, "bbox": caps_line["bbox"], "lines": [caps_line]}]
        style, reason = pdf_layout.inspect_block(native, pymupdf.Rect(caps_line["bbox"]))
        self.assertIsNone(reason)
        self.assertEqual(style["size"], 10)
        lower_line = {"bbox": [50, 80, 200, 92], "dir": (1, 0), "spans": [
            span("Fig. ", [50, 80, 68, 92], 10), span("model measurement", [68, 81.5, 130, 91], 8)]}
        style, reason = pdf_layout.inspect_block(
            [{"type": 0, "bbox": lower_line["bbox"], "lines": [lower_line]}],
            pymupdf.Rect(lower_line["bbox"]))
        self.assertIsNone(reason)
        self.assertEqual(style["size"], 8)

    def test_wrapped_numbered_heading_lines_merge_into_one_block(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            native = [self.native_block("IV. MODEL-BASED META-LEARNING FOR", [50, 50, 350, 62]),
                      self.native_block("QUADCOPTER PAYLOAD TRANSPORT", [60, 64, 340, 76])]
            blocks = [{"id": "0", "text": native[0]["lines"][0]["spans"][0]["text"],
                       "bbox": [.125, .125, .875, .155], "translation": "IV. 基于模型的元学习用于", "target": "简体中文"},
                      {"id": "1", "text": native[1]["lines"][0]["spans"][0]["text"],
                       "bbox": [.15, .16, .85, .19], "translation": "四旋翼飞行器载荷运输", "target": "简体中文"}]
            grouped = pdf_layout.layout_blocks(page, {"blocks": blocks}, native, "简体中文")
            self.assertEqual(len(grouped), 1)
            self.assertEqual(grouped[0]["_members"], ["0", "1"])
            self.assertEqual(grouped[0]["translation"], "IV. 基于模型的元学习用于四旋翼飞行器载荷运输")

    def test_numbered_heading_stays_separate_from_other_structures(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            upper = self.native_block("IV. MODEL-BASED META-LEARNING FOR", [50, 50, 350, 62])
            for lower_text, lower_box in (
                    ("A. Suspending a payload under a quadcopter", [50, 64, 350, 76]),
                    ("The method is useful for the control of a robot here.", [50, 64, 350, 100])):
                with self.subTest(lower=lower_text[:14]):
                    lower = self.native_block(lower_text, lower_box)
                    blocks = [{"id": "0", "text": upper["lines"][0]["spans"][0]["text"],
                               "bbox": [.125, .125, .875, .155], "translation": "IV. 标题", "target": "简体中文"},
                              {"id": "1", "text": lower_text,
                               "bbox": [n / 400 for n in lower_box], "translation": "后续内容。", "target": "简体中文"}]
                    grouped = pdf_layout.layout_blocks(page, {"blocks": blocks}, [upper, lower], "简体中文")
                    self.assertEqual(len(grouped), 2)

    def test_inline_math_normalization_preserves_script_markup_and_operators(self):
        self.assertEqual(pdf_layout.normalized_inline_math("z_B = [0, 0, T_r]^T"), "zB=[0,0,Tr]T")
        self.assertEqual(pdf_layout.normalized_inline_math("g = [0, 0, −9.81]^T"), "g=[0,0,-9.81]T")

    def test_inline_math_runs_keep_original_subscript_glyph_group(self):
        def glyph(char, box):
            return {"c": char, "bbox": box, "origin": (box[0], box[3])}

        line = {"bbox": [50, 50, 130, 70], "spans": [
            {"font": "Times-Roman", "text": "state ", "size": 10, "chars": []},
            {"font": "CMMIB10", "text": "z", "size": 10, "chars": [glyph("z", [80, 50, 86, 60])]},
            {"font": "CMMI7", "text": "B", "size": 7, "chars": [glyph("B", [86, 54, 91, 61])]},
            {"font": "Times-Roman", "text": " frame", "size": 10, "chars": []}]}
        native = [{"type": 0, "bbox": line["bbox"], "lines": [line]}]
        runs = pdf_layout.inline_math_runs(native, pymupdf.Rect(line["bbox"]))
        self.assertEqual([run["normalized"] for run in runs], ["zB"])
        self.assertEqual(runs[0]["rect"], pymupdf.Rect(80, 50, 91, 61))

    def test_inline_math_run_splitting_keeps_target_matches_local(self):
        chars = []
        for index, char in enumerate("O = [O_env, O_ego]"):
            x = 50 + index * 5
            chars.append({"c": char, "bbox": [x, 50, x + 4, 60], "origin": (x, 60),
                          "size": 10, "font": "CMMI10"})
        run = {"chars": chars}
        parts = pdf_layout._split_inline_math_run(run, " \t\r\n,;=[]")
        self.assertEqual([part["normalized"] for part in parts], ["O", "=[Oenv,", "Oego]"])

    def test_inline_math_overlay_transform_tracks_compacted_placement(self):
        clip = pymupdf.Rect(0, 0, 100, 200)
        placement = pymupdf.Rect(25, 10, 125, 210)
        point = pdf_layout._map_overlay_point(pymupdf.Point(75, 110), clip, placement)
        rect = pdf_layout._map_overlay_rect(pymupdf.Rect(10, 20, 30, 40), clip, placement)
        self.assertEqual(point, pymupdf.Point(100, 120))
        self.assertEqual(rect, pymupdf.Rect(35, 30, 55, 50))

    def test_inline_symbol_preservation_skips_non_prose_artifacts(self):
        body = {"kind": "paragraph", "text": "A body paragraph with x = y and surrounding prose.",
                "translation": "正文段落中的数学符号需要保留"}
        self.assertTrue(pdf_layout.should_preserve_inline_symbols(body))
        for kind in ("formula", "equation", "display_math", "algorithm", "figure", "table", "reference"):
            artifact = {"kind": kind, "text": "x = y", "translation": "正文段落中的数学符号需要保留"}
            with self.subTest(kind=kind):
                self.assertFalse(pdf_layout.should_preserve_inline_symbols(artifact))

    def test_split_heading_is_not_misclassified_as_table(self):
        first = self.native_block("2.1", [50, 50, 65, 62])
        title = self.native_block("Bioinspired Design", [100, 50, 220, 62])
        first["bbox"] = [50, 50, 220, 62]
        first["lines"].extend(title["lines"])
        self.assertIsNone(pdf_layout.inspect_block([first], pymupdf.Rect(first["bbox"]))[1])
        second = self.native_block("cell", [50, 70, 65, 82])
        second["lines"].extend(self.native_block("value", [100, 70, 220, 82])["lines"])
        first["lines"].extend(second["lines"])
        first["bbox"] = [50, 50, 220, 82]
        self.assertIn("单元格", pdf_layout.inspect_block([first], pymupdf.Rect(first["bbox"]))[1])

    def test_overlapping_inline_fragments_group_without_changing_saved_blocks(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            native = [self.native_block("where L1", [50, 50, 100, 62], "STIXTwoMath"),
                      self.native_block("leg is the maximum length of the leg tendon", [70, 50, 280, 64])]
            blocks = [{"id": str(i), "text": b["lines"][0]["spans"][0]["text"],
                       "bbox": [n / 400 for n in b["bbox"]], "translation": "测试译文", "target": "简体中文"}
                      for i, b in enumerate(native)]
            grouped = pdf_layout.layout_blocks(page, {"blocks": blocks}, native, "简体中文")
            self.assertEqual(len(grouped), 1)
            self.assertEqual(grouped[0]["_members"], ["0", "1"])
            self.assertEqual(len(blocks), 2)
            self.assertNotIn("_members", blocks[0])
            self.assertIsNone(pdf_layout.inspect_block(native, pdf_layout.source_rect(page, grouped[0]), combine=True)[1])

    def test_split_paragraph_halves_flow_as_one_block(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            upper_text = ("skew-symmetric matrix form of a vector. g = [0, 0, -9.81]T "
                          "is used throughout this model and the next line continues it")
            lower_text = ("is the constant gravitational acceleration. The angular velocity "
                          "denotes the state and the inertia matrix stays constant")
            native = [self.native_block(upper_text, [50, 50, 350, 62]),
                      self.native_block(lower_text, [50, 64, 350, 100])]
            blocks = [{"id": "0", "text": upper_text, "bbox": [.125, .125, .875, .155],
                       "translation": "向量的斜对称矩阵形式。", "target": "简体中文"},
                      {"id": "1", "text": lower_text, "bbox": [.125, .16, .875, .25],
                       "translation": "为恒定重力加速度。", "target": "简体中文"}]
            grouped = pdf_layout.layout_blocks(page, {"blocks": blocks}, native, "简体中文")
            self.assertEqual(len(grouped), 1)
            self.assertEqual(grouped[0]["_members"], ["0", "1"])
            self.assertEqual(grouped[0]["translation"], "向量的斜对称矩阵形式。为恒定重力加速度。")

    def test_short_paragraph_end_does_not_join_next_block(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            upper = self.native_block("A first paragraph whose final line ends early.", [50, 50, 350, 74])
            upper["lines"][0]["bbox"] = [50, 50, 350, 62]
            last = self.native_block("end", [50, 62, 200, 74])["lines"][0]
            upper["lines"].append(last)
            lower = self.native_block("A different paragraph starts here with enough prose to count.", [50, 75.8, 350, 112])
            blocks = [{"id": "0", "text": "A first paragraph whose final line ends early. end",
                       "bbox": [.125, .125, .875, .185], "translation": "第一段。", "target": "简体中文"},
                      {"id": "1", "text": lower["lines"][0]["spans"][0]["text"],
                       "bbox": [.125, .1895, .875, .28], "translation": "第二段。", "target": "简体中文"}]
            grouped = pdf_layout.layout_blocks(page, {"blocks": blocks}, [upper, lower], "简体中文")
            self.assertEqual(len(grouped), 2)

    def test_indented_first_line_does_not_join_previous_block(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            upper = self.native_block("A paragraph ends at the column edge and is followed by an indented paragraph here.",
                                      [50, 50, 350, 62])
            lower = self.native_block("An indented paragraph starts on a new line with enough prose to count.",
                                      [50, 63.8, 350, 100])
            lower["lines"][0]["bbox"] = [62, 63.8, 350, 75]
            blocks = [{"id": "0", "text": upper["lines"][0]["spans"][0]["text"],
                       "bbox": [.125, .125, .875, .155], "translation": "上一段。", "target": "简体中文"},
                      {"id": "1", "text": lower["lines"][0]["spans"][0]["text"],
                       "bbox": [.125, .1595, .875, .25], "translation": "下一段。", "target": "简体中文"}]
            grouped = pdf_layout.layout_blocks(page, {"blocks": blocks}, [upper, lower], "简体中文")
            self.assertEqual(len(grouped), 2)

    def test_captions_and_reference_entries_do_not_join_prose(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            body = ("A body paragraph that reaches the column edge and is followed "
                    "by another block in the same column position here")
            for lower_text in ("Figure 5: A caption that starts with a label and continues.",
                               "[2] A. Author, " + body):
                with self.subTest(lower=lower_text[:12]):
                    upper = self.native_block(body, [50, 50, 350, 62])
                    lower = self.native_block(lower_text, [50, 63.8, 350, 100])
                    blocks = [{"id": "0", "text": body, "bbox": [.125, .125, .875, .155],
                               "translation": "正文。", "target": "简体中文"},
                              {"id": "1", "text": lower_text, "bbox": [.125, .1595, .875, .25],
                               "translation": "后续内容。", "target": "简体中文"}]
                    grouped = pdf_layout.layout_blocks(page, {"blocks": blocks}, [upper, lower], "简体中文")
                    self.assertEqual(len(grouped), 2)

    def test_display_equation_is_not_merged_with_overlapping_prose(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            native = [self.native_block("E = mc2", [50, 50, 280, 62], "STIXTwoMath"),
                      self.native_block("where mass is the measured mass of the body in motion", [50, 61, 280, 100])]
            blocks = [{"id": str(i), "text": b["lines"][0]["spans"][0]["text"],
                       "bbox": [n / 400 for n in b["bbox"]], "translation": "测试译文", "target": "简体中文"}
                      for i, b in enumerate(native)]
            grouped = pdf_layout.layout_blocks(page, {"blocks": blocks}, native, "简体中文")
            self.assertEqual(len(grouped), 2)
            self.assertTrue(all("_members" not in b for b in grouped))

    def test_justified_words_do_not_look_like_table_columns(self):
        native = self.native_block("the", [50, 50, 65, 62])
        native["bbox"] = [50, 50, 200, 82]
        for y in [50, 70]:
            for x, word in [(72, "mass"), (94, "of"), (116, "the"), (138, "body")]:
                native["lines"].extend(self.native_block(word, [x, y, x + 15, y + 12])["lines"])
        self.assertIsNone(pdf_layout.inspect_block([native], pymupdf.Rect(native["bbox"]))[1])

    def test_explanation_next_to_equation_translates_without_altering_equation_pixels(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            page.insert_text((50, 80), "E = mc2", fontsize=11)
            page.insert_text((50, 94), "where m is the mass of the body in motion.", fontsize=11)
            raw = pdf.tobytes()
            lines = [line for block in page.get_text("dict")["blocks"] for line in block["lines"]]
            saved = {"number": 1, "blocks": [{"id": str(i), "text": "".join(s["text"] for s in line["spans"]),
                      "bbox": [n / 400 for n in line["bbox"]], "translation": "能量公式" if i == 0 else "其中 m 为运动物体的质量。", "target": "简体中文"}
                      for i, line in enumerate(lines)]}
        source = self.directory / "adjacent.pdf"
        source.write_bytes(raw)
        output, report = pdf_layout.typeset(source, {"pages": [saved]}, "简体中文")
        self.assertIn("1", report["placements"])
        with pymupdf.open(stream=raw, filetype="pdf") as original, pymupdf.open(stream=output, filetype="pdf") as result:
            clip = pymupdf.Rect(lines[0]["bbox"])
            self.assertEqual(original[0].get_pixmap(clip=clip).samples, result[0].get_pixmap(clip=clip).samples)
            self.assertIn("运动物体", result[0].get_text())
            self.assertNotIn("where m", result[0].get_text())

    def test_equation_number_is_left_in_place_when_explanation_is_translated(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            number = self.native_block("(8)", [280, 50, 300, 62])
            explanation = self.native_block("where the measured mass is the mass of the body", [50, 65, 300, 100])
            number["bbox"] = [50, 50, 300, 100]
            number["lines"].extend(explanation["lines"])
            block = {"id": "body", "text": "(8)\nwhere the measured mass is the mass of the body",
                     "bbox": [.125, .125, .75, .25], "translation": "(8)\n其中质量为物体质量。", "target": "简体中文"}
            grouped = pdf_layout.layout_blocks(page, {"blocks": [block]}, [number], "简体中文")
            self.assertEqual(grouped[0]["translation"], "其中质量为物体质量。")
            self.assertEqual(pdf_layout.source_rect(page, grouped[0]).y0, 65)
            style, reason = pdf_layout.inspect_block([number], pdf_layout.source_rect(page, grouped[0]))
            self.assertIsNone(reason)
            self.assertNotIn("(8)", [s["text"] for s in style["spans"]])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.patch = patch.object(service, "DATA", self.directory)
        self.patch.start()
        self.export_patch = patch.object(service, "EXPORT_DIR", self.directory / "exports")
        self.export_patch.start()

    def tearDown(self):
        self.export_patch.stop()
        self.patch.stop()
        self.temp.cleanup()

    def fixture(self, rotation=0, cropped=False):
        raw = layout_fixture(rotation, cropped)
        doc_id, doc = service.extract_document(raw, "layout.pdf")
        translations = {"Reading": "跨越语言的阅读", "An": "智能体利用上下文翻译文档。不同段落使用统一的术语。",
                        "The": "原始图片和页面布局得以保留。译文依然可以搜索和选择。",
                        "Figure": "图 1：原始矢量图形", "E": "能量公式", "A": "简短的说明。"}
        for block in service.all_blocks(doc):
            translation = translations[block["text"].split()[0]]
            block.update(translation=translation, target="简体中文")
        service.save_doc(self.directory / doc_id, doc)
        return raw, doc

    def test_heading_detection_allows_line_break_after_section_number(self):
        self.assertTrue(pdf_layout.section_heading("1 \nIntroduction"))
        self.assertTrue(pdf_layout.section_heading("2.1 \nBioinspired Design"))
        self.assertTrue(pdf_layout.section_heading("I. INTRODUCTION"))
        self.assertFalse(pdf_layout.section_heading("1Introduction"))

    def test_heading_separator_stroke_is_removed_with_translation(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            page.insert_text((50, 80), "1        Introduction", fontsize=11, fontname="hebo")
            page.draw_line((72, 70), (72, 81), color=(0, 0, 0), width=1)
            page.insert_textbox((50, 110, 350, 170), "Body text stays in place.", fontsize=11)
            raw = pdf.tobytes()
        doc_id, doc = service.extract_document(raw, "heading.pdf")
        for block in service.all_blocks(doc):
            if block["text"].startswith("1"):
                block.update(translation="1 引言", target="简体中文")
            else:
                block.update(translation="正文保持不变。", target="简体中文")
        result = build_layout(self.directory / doc_id, doc, "简体中文")
        with pymupdf.open(self.directory / doc_id / "layout" / (result["revision"] + ".pdf")) as pdf:
            self.assertIn("1 引言", pdf[0].get_text())
            strokes = [pymupdf.Rect(item["rect"]) for item in pdf[0].get_drawings(extended=True)
                       if item.get("level") == 1 and item.get("type") in {"s", "f", "fs"}
                       and item.get("rect") is not None]
            self.assertFalse([box for box in strokes
                              if box.width <= 1.6 and box.height >= 1.5
                              and 65 < box.x0 < 80 and 55 < box.y0 < 90])

    def test_bold_ranges_follow_numbered_run_in_labels(self):
        text = "1) 消融实验：无关内容。1) 稀疏奖励（SP）：直接处理该任务。2) 奖励塑形（RS）：增强训练。"
        hints = [{"kind": "marker", "marker": "1)", "ordinal": 1,
                  "terminator": "colon", "include": False, "limit": 40},
                 {"kind": "marker", "marker": "2)", "ordinal": 0,
                  "terminator": "colon", "include": False, "limit": 40}]
        self.assertEqual([text[start:end] for start, end in pdf_layout.bold_ranges(text, hints)],
                         ["稀疏奖励（SP）", "奖励塑形（RS）"])

    def test_bold_ranges_follow_lead_in_labels(self):
        colon = [{"kind": "lead", "marker": "", "ordinal": 0,
                  "terminator": "colon", "include": True, "limit": 40}]
        self.assertEqual(pdf_layout.bold_ranges("推力建模：本文采用", colon), [(0, 5)])
        dash = [{"kind": "lead", "marker": "", "ordinal": 0,
                 "terminator": "dash", "include": True, "limit": 40}]
        self.assertEqual(pdf_layout.bold_ranges("摘要——本文采用", dash), [(0, 3)])

    def test_inline_bold_labels_survive_translation(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=200)
            page.insert_htmlbox(pymupdf.Rect(40, 40, 360, 160),
                                '<div style="font:10pt Times,serif">1) <b>Sparse Reward (SP)</b>: directly '
                                'address this flight mission. 2) <b>Reward Shaping (RS)</b>: build upon SP.</div>')
            raw = pdf.tobytes()
        doc_id, doc = service.extract_document(raw, "bold.pdf")
        for block in service.all_blocks(doc):
            block.update(translation="1) 稀疏奖励（SP）：直接处理该飞行任务。"
                                     "2) 奖励塑形（RS）：在 SP 的基础上增强。", target="简体中文")
        result = build_layout(self.directory / doc_id, doc, "简体中文")
        with pymupdf.open(self.directory / doc_id / "layout" / (result["revision"] + ".pdf")) as pdf:
            spans = [(span["text"], bool(span["flags"] & 16))
                     for block in pdf[0].get_text("dict")["blocks"] if block.get("type") == 0
                     for line in block["lines"] for span in line["spans"] if span["text"].strip()]
            bold = "".join(text for text, is_bold in spans if is_bold)
            self.assertIn("稀疏奖励（SP）", bold)
            self.assertIn("奖励塑形（RS）", bold)
            self.assertNotIn("直接处理该飞行任务", bold)

    def test_long_bold_label_does_not_set_the_whole_paragraph_weight(self):
        block = self.native_block("Quadrotor Dynamics: ", [50, 50, 300, 80])
        label = block["lines"][0]["spans"][0]
        label["flags"] = 16
        for word in "We model the dynamics of the robot in the world frame using the following state variables".split():
            block["lines"][0]["spans"].append({**label, "text": word + " ", "flags": 0})
        style, reason = pdf_layout.inspect_block([block], pymupdf.Rect(block["bbox"]))
        self.assertIsNone(reason)
        self.assertFalse(style["bold"])
        hints = pdf_layout.inline_bold_hints(style, "Quadrotor Dynamics: We model the dynamics of the robot.")
        text = "四旋翼动力学：我们建立机器人的动力学模型。"
        self.assertEqual([text[start:end] for start, end in pdf_layout.bold_ranges(text, hints)], ["四旋翼动力学："])

    def test_table_cells_stay_original_while_caption_translates(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            for x, text in [(50, "Method"), (150, "Score"), (250, "Time")]:
                page.insert_text((x, 70), text, fontsize=9)
            for y, values in [(85, ["Baseline", "0.5", "1.2"]), (100, ["Ours", "0.9", "0.8"])]:
                for x, text in zip((50, 150, 250), values):
                    page.insert_text((x, y), text, fontsize=9)
            page.insert_text((50, 130), "Table 1: Sample results.", fontsize=9)
            raw = pdf.tobytes()
        doc_id, doc = service.extract_document(raw, "table.pdf")
        for block in service.all_blocks(doc):
            caption = block["text"].startswith("Table 1")
            block.update(translation="表1：示例结果。" if caption else "中文单元格", target="简体中文")
        result = build_layout(self.directory / doc_id, doc, "简体中文")
        self.assertTrue(any(issue.get("kind") == "table" for issue in result["issues"]))
        with pymupdf.open(self.directory / doc_id / "layout" / (result["revision"] + ".pdf")) as pdf:
            text = pdf[0].get_text()
            self.assertIn("表1：示例结果。", text)
            self.assertIn("Method", text)
            self.assertIn("Baseline", text)
            self.assertIn("0.9", text)
            self.assertNotIn("中文单元格", text)

    def test_layout_preserves_graphics_images_formula_and_searchable_chinese(self):
        original, doc = self.fixture()
        directory = self.directory / doc["id"]
        result = build_layout(directory, doc, "简体中文")
        self.assertEqual(result["replaced"], 5, result)
        self.assertEqual(result["preserved"], 1)
        with pymupdf.open(directory / "layout" / (result["revision"] + ".pdf")) as pdf, pymupdf.open(stream=original, filetype="pdf") as source:
            text = pdf[0].get_text()
            self.assertIn("跨越语言的阅读", text)
            self.assertNotIn("Reading across languages", text)
            self.assertIn("E = mc2", text)
            self.assertEqual(len(pdf), len(source))
            self.assertEqual(len(pdf[0].get_images()), len(source[0].get_images()))
            clip = pymupdf.Rect(345, 335, 435, 425)
            self.assertEqual(pdf[0].get_pixmap(clip=clip).samples, source[0].get_pixmap(clip=clip).samples)
            clip = pymupdf.Rect(65, 300, 270, 340)
            self.assertEqual(pdf[0].get_pixmap(clip=clip).samples, source[0].get_pixmap(clip=clip).samples)
        self.assertEqual((directory / "source.pdf").read_bytes(), original)

    def test_overflow_keeps_source_and_reports_problem(self):
        _, doc = self.fixture()
        doc["pages"][0]["blocks"][0]["translation"] = "无法放下的长篇译文。" * 1000
        result = build_layout(self.directory / doc["id"], doc, "简体中文")
        self.assertTrue(any(i["block_id"] == "p1-b1" and "超出" in i["message"] for i in result["issues"]))
        with pymupdf.open(self.directory / doc["id"] / "layout" / (result["revision"] + ".pdf")) as pdf:
            self.assertIn("Reading across languages", pdf[0].get_text())

    def test_mathematical_unicode_in_prose_uses_printable_equivalents(self):
        _, doc = self.fixture()
        doc["pages"][0]["blocks"][1]["translation"] = "其中 𝐿1 表示腿部肌腱的最大长度，θ4 表示初始角度。"
        result = build_layout(self.directory / doc["id"], doc, "简体中文")
        self.assertNotIn("p1-b2", [issue["block_id"] for issue in result["issues"]])
        with pymupdf.open(self.directory / doc["id"] / "layout" / (result["revision"] + ".pdf")) as pdf:
            self.assertIn("肌腱", pdf[0].get_text())
            self.assertIn("L1", pdf[0].get_text())

    def test_printable_fallback_and_unchanged_numeric_blocks(self):
        self.assertEqual(pdf_layout.printable_text("轨迹²⁷ 𝐿1 ﬀ"), "轨迹27 L1 ff")
        self.assertTrue(pdf_layout.unchanged_non_text({"text": "0.4", "translation": "0.4"}))
        self.assertTrue(pdf_layout.unchanged_non_text({"text": "2.0\n1.5\n1.0", "translation": "2.0 1.5 1.0"}))
        self.assertFalse(pdf_layout.unchanged_non_text({
            "text": "Reading across languages with context and terminology",
            "translation": "Reading across languages with context and terminology"}))

    def test_superscript_digit_translation_can_typeset(self):
        _, doc = self.fixture()
        doc["pages"][0]["blocks"][1]["translation"] = "该方法使用分段多项式轨迹²⁷进行规划。"
        result = build_layout(self.directory / doc["id"], doc, "简体中文")
        self.assertNotIn("p1-b2", [issue["block_id"] for issue in result["issues"] if issue["kind"] == "preserved"])
        with pymupdf.open(self.directory / doc["id"] / "layout" / (result["revision"] + ".pdf")) as pdf:
            self.assertIn("27", pdf[0].get_text())

    def test_rotation_cropbox_and_bounding_boxes(self):
        _, doc = self.fixture(90, True)
        result = build_layout(self.directory / doc["id"], doc, "简体中文")
        with pymupdf.open(self.directory / doc["id"] / "layout" / (result["revision"] + ".pdf")) as pdf:
            self.assertEqual(pdf[0].rotation, 90)
            self.assertEqual(pdf[0].cropbox, pymupdf.Rect(20, 20, 575, 820))
            self.assertIn("跨越语言的阅读", pdf[0].get_text())
        for box in result["placements"].values():
            self.assertTrue(all(0 <= n <= 1 for n in box), box)

    def test_revision_changes_after_edit_and_wrong_language_stays_original(self):
        _, doc = self.fixture()
        directory = self.directory / doc["id"]
        first = build_layout(directory, doc, "简体中文")
        self.assertEqual(build_layout(directory, doc, "简体中文"), first)
        doc["pages"][0]["blocks"][0]["translation"] = "修改后的标题"
        second = build_layout(directory, doc, "简体中文")
        self.assertNotEqual(first["revision"], second["revision"])
        third = build_layout(directory, doc, "English")
        self.assertEqual(third["replaced"], 0)
        self.assertEqual(third["untranslated"], doc["total_blocks"])

    def test_api_preview_and_download_use_same_revision(self):
        _, doc = self.fixture()
        with TestClient(service.app) as client:
            client.headers["X-Local-Token"] = client.get("/api/session").json()["token"]
            result = client.post(f"/api/documents/{doc['id']}/layout", json={"target": "简体中文"})
            self.assertEqual(result.status_code, 200, result.text)
            base = f"/api/documents/{doc['id']}/layout/{result.json()['revision']}"
            download = client.get(base + "/download")
            self.assertTrue(download.content.startswith(b"%PDF-"))
            saved = Path(result.json()["saved_pdf"])
            self.assertEqual(saved.parent, (self.directory / "exports").resolve())
            self.assertEqual(saved.read_bytes(), download.content)
            image = client.get(base + "/pages/1.png")
            with pymupdf.open(stream=download.content, filetype="pdf") as pdf:
                page = pdf[0]
                scale = min(1.7, 2200 / max(page.rect.width, page.rect.height))
                expected = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
            self.assertEqual(hashlib.sha256(image.content).digest(), hashlib.sha256(expected).digest())
            self.assertEqual(client.get(base + "/pages/0.png").status_code, 404)
            self.assertEqual(client.get(f"/api/documents/{doc['id']}/layout/not-a-revision/download").status_code, 404)

    def test_edit_retypesets_only_changed_page_and_preserves_original_links(self):
        with pymupdf.open(stream=layout_fixture(), filetype="pdf") as source:
            source[1].insert_text((55, 70), "Second page note.", fontsize=12)
            source[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(55, 65, 120, 80), "page": 1})
            source.set_toc([[1, "First", 1], [1, "Second", 2]])
            raw = source.tobytes()
        doc_id, doc = service.extract_document(raw, "cache.pdf")
        for block in service.all_blocks(doc):
            block.update(translation="测试译文", target="简体中文")
        directory = self.directory / doc_id
        with patch.object(pdf_layout, "typeset", wraps=pdf_layout.typeset) as engine:
            first = build_layout(directory, doc, "简体中文")
            self.assertEqual(engine.call_count, 2)
            doc["pages"][1]["blocks"][0]["translation"] = "第二页更新译文"
            second = build_layout(directory, doc, "简体中文")
            self.assertEqual(engine.call_count, 3)
            self.assertEqual(engine.call_args.kwargs["page_numbers"], [1])
            self.assertEqual(build_layout(directory, doc, "简体中文"), second)
            self.assertEqual(engine.call_count, 3)
        with pymupdf.open(directory / "layout" / (second["revision"] + ".pdf")) as result:
            self.assertIn("第二页更新译文", result[1].get_text())
            self.assertEqual(result[0].get_links()[0]["page"], 1)
            self.assertEqual(result.get_toc(), [[1, "First", 1], [1, "Second", 2]])
        self.assertNotEqual(first["revision"], second["revision"])


if __name__ == "__main__":
    unittest.main()
