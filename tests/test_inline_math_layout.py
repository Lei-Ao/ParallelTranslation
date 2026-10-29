import unittest
import xml.etree.ElementTree as ET

import pymupdf

import pdf_layout as layout
from pdf_typography import font_resources


class InlineMathLayoutTests(unittest.TestCase):
    def asset_fixture(self, page):
        page.insert_text((50, 80), "x", fontsize=10, fontname="tiit")
        page.insert_text((55, 82), "0", fontsize=7, fontname="tiro")
        # Neighboring ink must never be captured along with the variable.
        page.insert_text((50, 90), "foreign", fontsize=10)
        spans = [span for block in page.get_text("rawdict")["blocks"]
                 for line in block["lines"] for span in line["spans"]]
        chars = [{**char, "size": span["size"], "font": span["font"]}
                 for span in spans[:2] for char in span["chars"]]
        run = layout._inline_math_run(chars, True)
        return layout.inline_math_assets(page, [run])[0]

    def test_asset_contains_only_owned_glyphs_and_preserves_baseline(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            asset = self.asset_fixture(page)
            root = ET.fromstring(asset["svg"])
            glyphs = list(root.iter("{http://www.w3.org/2000/svg}use"))
            self.assertEqual([node.get("data-text") for node in glyphs], ["x", "0"])
            self.assertGreater(asset["descent"], 0)
            self.assertLess(asset["height"], 11)
            self.assertAlmostEqual(float(root.get("width")) / asset["width"], 6)

    def test_markup_matches_whole_variables_and_keeps_unmatched_text(self):
        with pymupdf.open() as pdf:
            asset = self.asset_fixture(pdf.new_page())
            archive = pymupdf.Archive()
            markup, plain, labels, _ = layout.inline_math_markup("保留 ax0，变量 x_0 的说明。", [asset], archive)
            self.assertIn("ax0", markup)
            self.assertEqual(markup.count("<img"), 1)
            self.assertEqual(labels, ["x_0"])
            self.assertEqual(plain, "保留 ax0，变量  的说明。")

    def math_run(self, text):
        chars = [{"c": char, "bbox": [50 + index * 5, 50, 54 + index * 5, 60],
                  "origin": (50 + index * 5, 60), "size": 10, "font": "CMMI10"}
                 for index, char in enumerate(text)]
        return layout._inline_math_run(chars, True)

    def test_basis_vector_lists_keep_accents_and_scripts_across_translated_separators(self):
        runs = [self.math_run("ˆe1,ˆe2,"), self.math_run("(ˆbi,ˆei)")]
        translated = "坐标轴为 ˆe1、ˆe2 和 ˆe3，基向量为（ˆbi、ˆei）。"
        result = layout.translated_math_runs(runs, translated)
        self.assertEqual([r["normalized"] for r in result], ["ˆe1", "ˆe2", "(ˆbi,ˆei)"])
        self.assertEqual([c["c"] for c in result[0]["chars"]], ["ˆ", "e", "1"])

    def test_function_arguments_and_operators_are_not_split_to_match_changed_math(self):
        for source in ("f(x,y)", "x=[a,b]", "x+y,z"):
            run = self.math_run(source)
            self.assertEqual(layout.translated_math_runs([run], "x 和 y，以及 a 和 b，z"), [run])

    def test_long_expressions_match_before_variables_and_survive_clause_reordering(self):
        with pymupdf.open() as pdf:
            asset = self.asset_fixture(pdf.new_page())
            assets = [{**asset, "normalized": key} for key in ("u", "u=M(Ω)T", "T")]
            markup, plain, labels, _ = layout.inline_math_markup(
                "由 T 通过 u=M(Ω)T 得到控制量 u。", assets, pymupdf.Archive())
            self.assertEqual(labels, ["T", "u=M(Ω)T", "u"])
            self.assertEqual(markup.count("<img"), 3)
            self.assertEqual(plain, "由  通过  得到控制量 。")

    def test_unsupported_symbol_falls_back_without_changing_chinese_body_font(self):
        text = "正文保持统一字体。\n符号 ˆei 使用独立字形。"
        typography = font_resources(text)
        if typography["archive"] is None:
            self.skipTest("当前系统没有可嵌入的中文字体")
        with pymupdf.open() as source, pymupdf.open() as overlays:
            page = source.new_page(width=400, height=300)
            style = {"size": 10, "lines": [], "bold": False, "italic": False, "color": 0}
            result = layout.fitted_overlay(overlays, page,
                {"text": "The vector is used for control of the robot.", "translation": text},
                pymupdf.Rect(40, 50, 200, 150), style, [], typography)
            self.assertIsNotNone(result)
            spans = [s for b in overlays[result["overlay"]].get_text("dict")["blocks"] if b["type"] == 0
                     for line in b["lines"] for s in line["spans"] if any("\u3400" <= c <= "\u9fff" for c in s["text"])]
            self.assertTrue(spans)
            self.assertTrue(all(s["font"].replace("-", " ") == "ParallelBook Regular" and not s["flags"] & 16 for s in spans))
            self.assertEqual(len({round(s["origin"][1], 1) for s in spans}), 2)

    def test_inline_image_keeps_aspect_ratio_and_searchable_spelling(self):
        with pymupdf.open() as source, pymupdf.open() as overlays:
            page = source.new_page(width=400, height=400)
            asset = self.asset_fixture(page)
            translation = "测试变量 x0 应保持原始比例与上下标。"
            block = {"id": "math", "text": "The variable x0 is used in the robot state.",
                     "translation": translation, "_inline_assets": [asset]}
            style = {"size": 10, "lines": [], "bold": False, "italic": False, "color": 0}
            result = layout.fitted_overlay(overlays, page, block, pymupdf.Rect(40, 120, 280, 220),
                                           style, [], {"css": "", "family": "serif", "archive": None})
            self.assertIsNotNone(result)
            rendered = overlays[result["overlay"]]
            image = rendered.get_image_info()[0]
            box = pymupdf.Rect(image["bbox"])
            self.assertAlmostEqual(box.width / box.height, asset["width"] / asset["height"], places=4)
            self.assertIn("x0", rendered.get_text())
            self.assertGreater(image["width"], box.width * 5)

    def test_zero_descender_font_redaction_intersects_actual_ascender(self):
        strip = layout.span_strip({"bbox": (50, 70, 64, 80), "origin": (50, 80),
                                   "size": 10, "ascender": .464, "descender": 0})
        self.assertGreater(strip.y0, 75.36)
        self.assertLess(strip.y1, 80)

    def test_cover_roles_use_geometry_and_typography_instead_of_block_ids(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=800)
            title = {"id": "unrelated-id", "text": "A Paper About Adaptive Typesetting"}
            style = {"bold": True, "size": 20, "lines": [{}, {}]}
            self.assertEqual(layout.cover_text_role(title, page, pymupdf.Rect(70, 40, 530, 80), style), "title")
            self.assertIsNone(layout.cover_text_role({**title, "_source_page": 1}, page,
                                                     pymupdf.Rect(70, 40, 530, 80), style))
            authors = {"id": "another-id", "text": "John Doe, Jane Roe and Alice Smith"}
            self.assertEqual(layout.cover_text_role(authors, page, pymupdf.Rect(100, 100, 500, 115),
                                                    {"bold": False, "size": 10, "lines": [{}]}), "authors")


if __name__ == "__main__":
    unittest.main()
