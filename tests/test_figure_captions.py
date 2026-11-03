"""Caption emphasis and compact, natural mixed-script caption typesetting."""
import copy
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest.mock import patch

import pymupdf

import app as service
import pdf_layout


class CaptionEmphasisTests(unittest.TestCase):
    def style(self, prefix="Figure 7:", punctuation="."):
        return {"lines": [{"spans": [
            {"text": prefix + " ", "flags": 0},
            {"text": "A compact system overview", "flags": 16},
            {"text": punctuation + " The remaining explanation is ordinary text with several details.", "flags": 0},
        ]}]}

    def test_title_after_caption_number_keeps_only_its_source_emphasis(self):
        for source_label, translated_label in (("Figure 7:", "图 7："), ("Fig. 7:", "图7："),
                                                ("Table 7:", "表 7："), ("Figure S7:", "图 S7：")):
            with self.subTest(source_label=source_label):
                text = source_label + " A compact system overview. The remaining explanation is ordinary text."
                hints = pdf_layout.inline_bold_hints(self.style(source_label), text)
                translated = translated_label + "系统概览。其余解释保持正常字重。"
                ranges = pdf_layout.bold_ranges(translated, hints)
                self.assertEqual([translated[a:b] for a, b in ranges], ["系统概览"])

    def test_unreliable_caption_title_boundary_does_not_bolden_explanation(self):
        hints = pdf_layout.inline_bold_hints(self.style(), "Figure 7: A compact system overview. More details.")
        translated = "图7：系统概览与解释没有可确定的句子边界"
        self.assertEqual(pdf_layout.bold_ranges(translated, hints), [])

    def test_regular_text_prefix_does_not_become_a_caption_title(self):
        text = "Our system: A compact system overview. The remaining explanation is ordinary text."
        self.assertEqual(pdf_layout.inline_bold_hints(self.style("Our system:"), text), [])

    def test_separate_space_spans_preserve_sentence_boundary_and_complete_bold_label(self):
        style = self.style()
        style["lines"][0]["spans"][2:] = [{"text": ".", "flags": 0},
                                               {"text": " ", "flags": 0},
                                               {"text": "An ordinary explanation follows.", "flags": 0}]
        text = "Figure 7: A compact system overview. An ordinary explanation follows."
        translated = "图 7：系统框架概览。其余解释。"
        hints = pdf_layout.inline_bold_hints(style, text)
        self.assertEqual([translated[a:b] for a, b in pdf_layout.bold_ranges(translated, hints)], ["系统框架概览"])

        label_style = {"lines": [
            {"spans": [{"text": "FIGURE", "flags": 16}, {"text": " ", "flags": 0},
                       {"text": "7", "flags": 16}, {"text": " ", "flags": 0}]},
            {"spans": [{"text": "An ordinary explanation follows.", "flags": 0}]},
        ]}
        hints = pdf_layout.inline_bold_hints(label_style, "FIGURE 7 An ordinary explanation follows.")
        self.assertEqual([translated[a:b] for a, b in pdf_layout.bold_ranges(translated, hints)], ["图 7"])

    def test_wrapped_title_follows_source_bold_run_to_its_actual_delimiter(self):
        style = {"lines": [
            {"spans": [{"text": "Figure 7: ", "flags": 0},
                       {"text": "An explanation of a system", "flags": 16}]},
            {"spans": [{"text": "whose title wraps onto a second line", "flags": 16},
                       {"text": ". More details follow in ordinary type.", "flags": 0}]},
        ]}
        text = "Figure 7: An explanation of a system whose title wraps onto a second line. More details follow."
        hints = pdf_layout.inline_bold_hints(style, text)
        translated = "图7：系统及其工作流程的概览。其余解释保持常规。"
        self.assertEqual([translated[a:b] for a, b in pdf_layout.bold_ranges(translated, hints)],
                         ["系统及其工作流程的概览"])
        style["lines"][1]["spans"][1]["text"] = " More details follow without a punctuation boundary"
        self.assertEqual(pdf_layout.inline_bold_hints(style, text), [])


class CaptionTypesettingTests(unittest.TestCase):
    def test_l_shaped_prose_preserves_source_glyphs_while_caption_and_neighbor_translate(self):
        cases = ((1, 8, 0), (2, 10, 90), (3, 11, 270))
        for columns, size, rotation in cases:
            with self.subTest(columns=columns, size=size, rotation=rotation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.object(service, "DATA", root), pymupdf.open() as original:
                    page = original.new_page(width=columns * 480, height=520)
                    for column in range(columns):
                        left = 30 + column * 480
                        # Consecutive left-side lines and a full-width final
                        # line extract as one L-shaped paragraph. Its union
                        # bbox encloses the separate right-side figure caption.
                        page.insert_text((left, 60), "\n".join(["The controller observes the robot."] * 9),
                                         fontsize=size, lineheight=1.3)
                        page.insert_text((left, 60 + 9 * size * 1.3),
                                         "The state and control signals are combined into a complete description of the method.", fontsize=size)
                        page.draw_rect((left + 230, 40, left + 410, 102), fill=(.7, .8, .9))
                        page.insert_text((left + 230, 130), "Figure 4: A compact view.", fontsize=size)
                        page.insert_text((left, 260),
                                         "An ordinary paragraph below the illustration is translated as usual.", fontsize=size)
                    page.set_rotation(rotation)
                    doc_id, document = service.extract_document(original.tobytes(), "wrapping-caption.pdf")
                    bodies, captions, neighbors = [], [], []
                    for block in document["pages"][0]["blocks"]:
                        if block["text"].startswith("Figure 4:"):
                            captions.append(block)
                            translated = "图4：局部示意。"
                        elif block["text"].startswith("The controller"):
                            bodies.append(block)
                            translated = "绕排正文译文不得覆盖图注。" * 12
                        else:
                            neighbors.append(block)
                            translated = "普通正文正常翻译。"
                        block.update(translation=translated, target="简体中文")
                    self.assertEqual((len(bodies), len(captions), len(neighbors)), (columns, columns, columns))
                    unchanged = copy.deepcopy(document)
                    raw, report = pdf_layout.typeset(root / doc_id / "source.pdf", document, "简体中文")
                    self.assertEqual(document, unchanged)
                    self.assertEqual(report["preserved"], columns, report)
                    self.assertEqual(report["replaced"], columns * 2, report)
                    with pymupdf.open(stream=raw, filetype="pdf") as output, \
                            pymupdf.open(root / doc_id / "source.pdf") as source:
                        self.assertEqual(output[0].rotation, rotation)
                        output[0].set_rotation(0)
                        source[0].set_rotation(0)
                        native = [b for b in source[0].get_text("dict")["blocks"] if b["type"] == 0]
                        body_lines = [line for b in native
                                      if "".join(s["text"] for line in b["lines"] for s in line["spans"])
                                      .startswith("The controller") for line in b["lines"]]
                        output_text = "".join(output[0].get_text().split())
                        for block in bodies:
                            self.assertIn("".join(block["text"].split()), output_text)
                            self.assertNotIn(block["id"], report["placements"])
                            self.assertTrue(any(issue.get("block_id") == block["id"]
                                                and "绕排" in issue["message"] for issue in report["issues"]))
                        for line in body_lines:
                            # Independently compare the actual source line
                            # pixels, rather than merely trusting report boxes.
                            clip = pymupdf.Rect(line["bbox"])
                            self.assertEqual(source[0].get_pixmap(clip=clip).samples,
                                             output[0].get_pixmap(clip=clip).samples)
                        for block in captions + neighbors:
                            box = pdf_layout.source_rect(page, {"bbox": report["placements"][block["id"]]})
                            rendered = "".join(output[0].get_text(clip=box).split())
                            self.assertIn("".join(block["translation"].split()), rendered)
                            if block in captions:
                                spans = [span for b in output[0].get_text("dict", clip=box)["blocks"]
                                         if b["type"] == 0 for line in b["lines"] for span in line["spans"]]
                                for span in spans:
                                    for body_line in body_lines:
                                        self.assertLessEqual(pdf_layout.overlap(
                                            pymupdf.Rect(span["bbox"]), pymupdf.Rect(body_line["bbox"])), .1)

    def test_wrapping_prose_with_inline_math_does_not_absorb_separate_figure_caption(self):
        body = "The controller is used for the robot and the following state is estimated from observations."
        caption = "Figure 4: The small diagram shows the components of the controller and the signal flow."
        with pymupdf.open() as original:
            page = original.new_page(width=460, height=400)
            def line(text, box, font="Times-Roman"):
                return {"bbox": box, "dir": (1, 0), "spans": [{"text": text, "bbox": box,
                    "size": 10, "font": font, "color": 0, "flags": 0}]}
            native = [{"bbox": [30, 50, 430, 200], "lines": [
                line("The controller uses q", [30, 50, 220, 65], "STIXTwoMath"),
                line("for the robot and the following state is estimated from observations.", [30, 185, 430, 200])]},
                {"bbox": [250, 120, 430, 140], "lines": [line(caption, [250, 120, 430, 140])]}]
            for rotation in (0, 90, 180, 270):
                with self.subTest(rotation=rotation):
                    page.set_rotation(rotation)
                    blocks = []
                    for index, (text, item) in enumerate(zip((body, caption), native)):
                        displayed = pymupdf.Rect(item["bbox"]) * page.rotation_matrix
                        blocks.append({"id": str(index), "text": text, "translation": "正文译文" if index == 0 else "图注译文",
                                       "target": "简体中文", "bbox": [displayed.x0 / page.rect.width,
                                       displayed.y0 / page.rect.height, displayed.x1 / page.rect.width,
                                       displayed.y1 / page.rect.height]})
                    unchanged = copy.deepcopy(blocks)
                    grouped = pdf_layout.layout_blocks(page, {"blocks": blocks}, native, "简体中文")
                    self.assertEqual(len(grouped), 2)
                    self.assertEqual(blocks, unchanged)
                    for block in grouped:
                        self.assertEqual(block.get("_members", [block["id"]]), [block["id"]])
                    self.assertEqual({block["translation"] for block in grouped}, {"正文译文", "图注译文"})

    def test_unnumbered_caption_words_inside_artwork_remain_figure_labels(self):
        for label in ("Figure features", "Table lookup", "Algorithm selection"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.object(service, "DATA", root), pymupdf.open() as original:
                    page = original.new_page(width=460, height=400)
                    page.draw_rect((30, 40, 430, 160), fill=(.7, .8, .9))
                    page.insert_text((50, 80), label, fontsize=10)
                    page.insert_text((30, 180), "Figure 1: A system overview.", fontsize=9)
                    doc_id, document = service.extract_document(original.tobytes(), "caption-words.pdf")
                    for block in document["pages"][0]["blocks"]:
                        block.update(translation="图1：系统概览。" if block["text"].startswith("Figure 1:")
                                     else "图内标签不应替换", target="简体中文")
                    unchanged = copy.deepcopy(document)
                    raw, report = pdf_layout.typeset(root / doc_id / "source.pdf", document, "简体中文")
                    self.assertEqual(document, unchanged)
                    self.assertEqual(report["preserved"], 1, report)
                    self.assertEqual(report["replaced"], 1, report)
                    with pymupdf.open(stream=raw, filetype="pdf") as output:
                        self.assertIn(label, output[0].get_text())
                        self.assertIn("系统概览", output[0].get_text())
                        self.assertNotIn("图内标签不应替换", output[0].get_text())

    def test_english_and_entirely_bold_captions_keep_source_alignment_and_weight(self):
        prose = ("Figure 7: A system overview. The illustration summarizes the controller and its "
                 "evaluation environments. Several modules cooperate to complete a demanding task, "
                 "while the arrows show the flow of observations and control signals.")
        for fully_bold in (False, True):
            with self.subTest(fully_bold=fully_bold), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.object(service, "DATA", root), pymupdf.open() as original:
                    page = original.new_page(width=460, height=400)
                    page.draw_rect((30, 40, 430, 130), fill=(.7, .8, .9))
                    page.insert_htmlbox((30, 145, 430, 205), "<b>" + prose + "</b>" if fully_bold else prose,
                                        css="*{margin:0;padding:0}body{font:9pt Times;line-height:1.1;text-align:justify}")
                    doc_id, document = service.extract_document(original.tobytes(), "english-caption.pdf")
                    block, = document["pages"][0]["blocks"]
                    block.update(translation=prose, target="English")
                    style, issue = pdf_layout.inspect_block(page.get_text("dict")["blocks"],
                                                            pdf_layout.source_rect(page, block))
                    self.assertIsNone(issue)
                    self.assertEqual(pdf_layout.source_paragraph_style(
                        style, pdf_layout.source_rect(page, block), block["text"], page.rect.width)[0], "justify")
                    raw, report = pdf_layout.typeset(root / doc_id / "source.pdf", document, "English")
                    self.assertEqual(report["replaced"], 1, report)
                    with pymupdf.open(stream=raw, filetype="pdf") as output:
                        spans = [span for b in output[0].get_text("dict")["blocks"] if b["type"] == 0
                                 for line in b["lines"] for span in line["spans"] if span["text"].strip()]
                        self.assertTrue(all(abs(span["size"] - 9) < .1 for span in spans))
                        self.assertTrue(all(bool(span["flags"] & 16 or "SemiBold" in span["font"]) == fully_bold
                                            for span in spans))
                        self.assertEqual("".join(output[0].get_text().split()), "".join(prose.split()))

    def test_cjk_caption_preserves_size_and_safe_spacing_across_columns_and_rotation(self):
        source = ("Figure 7: <b>A compact system overview</b>. The framework combines several control "
                  "modules and evaluation environments. Each component communicates with the others, "
                  "and the diagram shows how the controller coordinates the complete process.")
        translation = ("图 7：系统框架概览。该框架采用模块化结构，在多个控制模块与测试环境之间交换信息。"
                       "AM-Bench 中的各个组件共同完成控制任务，图示描述了数据传递、控制分配及整个运行过程。")
        for columns, size, rotation in ((1, 8, 0), (2, 9, 90), (3, 11, 270), (1, 10, 180)):
            with self.subTest(columns=columns, size=size, rotation=rotation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.object(service, "DATA", root), pymupdf.open() as original:
                    page = original.new_page(width=columns * 420 + 40, height=500)
                    for column in range(columns):
                        left = 30 + column * 420
                        page.draw_rect((left, 40, left + 380, 130), fill=(.7, .8, .9))
                        page.insert_htmlbox((left, 145, left + 380, 205), source,
                            css=f"*{{margin:0;padding:0}}body{{font: {size}pt Times;line-height:1.1;text-align:justify}}")
                        page.draw_rect((left, 225, left + 380, 285), fill=(.8, .7, .6))
                    page.set_rotation(rotation)
                    doc_id, document = service.extract_document(original.tobytes(), "captions.pdf")
                    captions = document["pages"][0]["blocks"]
                    self.assertEqual(len(captions), columns)
                    for block in captions:
                        block.update(translation=translation, target="简体中文")
                    unchanged = copy.deepcopy(document)
                    raw, report = pdf_layout.typeset(root / doc_id / "source.pdf", document, "简体中文")
                    self.assertEqual(report["replaced"], columns, report)
                    self.assertEqual(document, unchanged)
                    with pymupdf.open(stream=raw, filetype="pdf") as output:
                        self.assertEqual(output[0].rotation, rotation)
                        output[0].set_rotation(0)
                        for block in captions:
                            placement = pdf_layout.source_rect(page, {"bbox": report["placements"][block["id"]]})
                            self.assertLess(placement.y1, 225)
                            # Read the rendered output, independently of the CSS
                            # construction, to catch justification and size changes.
                            spans = [span for b in output[0].get_text("dict", clip=placement)["blocks"]
                                     if b["type"] == 0 for line in b["lines"] for span in line["spans"]]
                            self.assertTrue(spans)
                            self.assertTrue(all(abs(span["size"] - size) < .1 for span in spans), spans)
                            chars = [char for b in output[0].get_text("rawdict", clip=placement)["blocks"]
                                     if b["type"] == 0 for line in b["lines"]
                                     for span in line["spans"] for char in span["chars"]]
                            self.assertTrue(any(char["c"] == "图" for char in chars))
                            first = next(i for i, char in enumerate(chars) if char["c"] == "图")
                            seven = next(char for char in chars[first + 1:] if char["c"] == "7")
                            self.assertLess(seven["bbox"][0] - chars[first]["bbox"][2], size * .7)
                            baselines = sorted(set(round(span["origin"][1], 2) for span in spans))
                            for a, b in zip(baselines, baselines[1:]):
                                self.assertGreaterEqual(b - a, size * 1.15)
                                self.assertLessEqual(b - a, size * 1.3)
                            rendered = "".join(char["c"] for char in chars)
                            normal = lambda value: "".join(unicodedata.normalize("NFKC", value).split())
                            self.assertEqual(normal(rendered), normal(translation))
                            bold = "".join(span["text"] for span in spans if span["flags"] & 16
                                           or "SemiBold" in span["font"])
                            self.assertIn("系统框架概览", bold)
                            self.assertNotIn("该框架", bold)


if __name__ == "__main__":
    unittest.main()
