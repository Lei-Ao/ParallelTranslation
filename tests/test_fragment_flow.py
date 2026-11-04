"""Overlapping extraction fragments must typeset as one flow.

A paragraph is often extracted twice: once as the full block and once as a
short remainder (a line ending in inline math, a lone subscript). Both boxes
overlap, so typesetting them independently prints two translations over the
same lines. The grouping rule must join such a remainder to its paragraph and
still leave separate structures — display equations, table cells, short glyph
pieces — on their own.
"""
import unittest

import pymupdf

import pdf_layout as layout

PROSE = ("The reference trajectory is specified by the rotation matrix and both of them are "
         "expressed in the world frame, where the observation is flattened into a vector.")
PROSE_CN = "参考轨迹由旋转矩阵指定，二者均在世界坐标系中表示，观测被展平为一个向量。"


def text_block(box, text, font="Times-Roman", size=11):
    return {"bbox": box, "lines": [{"bbox": box, "dir": (1, 0), "spans": [
        {"bbox": box, "text": text, "font": font, "size": size, "color": 0, "flags": 0}]}]}


def saved(block_id, text, translation, box, target="简体中文"):
    return {"id": block_id, "text": text, "translation": translation, "target": target, "bbox": box}


def blank_page():
    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    return pdf, page


class OverlappingFragmentTests(unittest.TestCase):
    def test_remainder_inside_a_paragraph_joins_it(self):
        pdf, page = blank_page()
        with pdf:
            paragraph = saved("p1", PROSE, PROSE_CN, [.51, .44, .92, .52])
            remainder = saved("p2", "specified by WRB", "由 WRB 指定", [.51, .444, .64, .456])
            native = [text_block([312, 351.6, 392, 361.2], "W RB", font="CMMI10")]
            result = layout.layout_blocks(page, {"blocks": [paragraph, remainder]}, native, "简体中文")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["_members"], ["p1", "p2"])
        self.assertIn("由 WRB 指定", result[0]["translation"])
        self.assertIn(PROSE_CN, result[0]["translation"])

    def test_glyph_piece_that_merely_touches_a_paragraph_stays_separate(self):
        pdf, page = blank_page()
        with pdf:
            paragraph = saved("p1", PROSE, PROSE_CN, [.51, .44, .92, .52])
            glyph = saved("p2", "specified by WRB", "由 WRB 指定", [.90, .51, .95, .525])
            native = [text_block([312, 355, 392, 365], "W RB", font="CMMI10")]
            result = layout.layout_blocks(page, {"blocks": [paragraph, glyph]}, native, "简体中文")
        self.assertEqual([block["id"] for block in result], ["p1", "p2"])

    def test_formula_only_remainder_is_not_absorbed_into_prose(self):
        pdf, page = blank_page()
        with pdf:
            paragraph = saved("p1", PROSE, PROSE_CN, [.51, .44, .92, .52])
            formula = saved("p2", "E = mc2", "E = mc2", [.60, .45, .75, .462])
            native = [text_block([367, 356.4, 459, 365.9], "E=mc2", font="CMMI10")]
            result = layout.layout_blocks(page, {"blocks": [paragraph, formula]}, native, "简体中文")
        self.assertEqual([block["id"] for block in result], ["p1", "p2"])

    def test_two_overlapping_prose_blocks_share_one_flow(self):
        # The pre-existing rule: overlapping prose cannot be printed twice on
        # the same lines, so both halves join one flow even without a remainder.
        pdf, page = blank_page()
        with pdf:
            upper = saved("p1", PROSE, PROSE_CN, [.51, .44, .92, .52])
            lower = saved("p2", PROSE, PROSE_CN, [.51, .45, .92, .53])
            native = [text_block([312, 356, 563, 420], "W RB", font="CMMI10")]
            result = layout.layout_blocks(page, {"blocks": [upper, lower]}, native, "简体中文")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["_members"], ["p1", "p2"])

    def test_helper_rejects_a_remainder_without_running_words(self):
        small = pymupdf.Rect(0, 0, 10, 10)
        large = pymupdf.Rect(0, 0, 100, 100)
        self.assertFalse(layout.overlapping_fragment("WRB", small, PROSE, large))
        self.assertFalse(layout.overlapping_fragment(PROSE, small, PROSE, large))
        self.assertFalse(layout.overlapping_fragment("specified by WRB", small, "E = mc2", large))
        self.assertTrue(layout.overlapping_fragment("specified by WRB", small, PROSE, large))


if __name__ == "__main__":
    unittest.main()
