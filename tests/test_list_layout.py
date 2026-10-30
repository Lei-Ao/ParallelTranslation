"""Hanging list indents come from source geometry, including mixed text blocks."""
import copy
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import pymupdf
import pdf_layout
from pdf_lists import source_list_structure, translated_list_layout, ListLayoutError


def visual_rows(page, clip):
    rows = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    if not char["c"].strip():
                        continue
                    if not clip.contains(pymupdf.Point(char["origin"])):
                        continue
                    baseline = char["origin"][1]
                    row = next((r for r in rows if abs(r[0] - baseline) < .5), None)
                    if row is None:
                        row = [baseline, []]
                        rows.append(row)
                    row[1].append(char)
    return [(y, sorted(chars, key=lambda c: c["bbox"][0])) for y, chars in sorted(rows)]


class ListLayoutTests(unittest.TestCase):
    def fixture(self, *, columns=2, size=10, rotation=0, cropped=False,
                markers=("2)", "3)", "4)"), tail=True, broken=False, gap=0):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "source.pdf"
        blocks, axes = [], {}
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=840 if columns == 3 else 612, height=792)
            if cropped:
                page.set_cropbox(pymupdf.Rect(12, 10, page.rect.width - 12, page.rect.height - 10))
            width = (page.rect.width - 80 - 24 * (columns - 1)) / columns
            for column in range(columns):
                left = 40 + column * (width + 24)
                marker_widths = [pymupdf.get_text_length(marker, fontname="china-s" if any(ord(c) > 127 for c in marker) else "tiro", fontsize=size)
                                 for marker in markers]
                body = left + max(size * 2.6, max(marker_widths) + size)
                line = ""
                for word in ("The method is useful for the control of a robot. " * 30).split():
                    candidate = (line + " " + word).strip()
                    if pymupdf.get_text_length(candidate, fontname="tiro", fontsize=size) > left + width - body:
                        break
                    line = candidate
                source_lines, translations = [], []
                baseline = 60
                marker_axes = []
                length = int(pymupdf.get_text_length(line, fontname="tiro", fontsize=size) / size * 2.2)
                for index, marker in enumerate(markers):
                    if index:
                        baseline += gap
                    marker_font = "china-s" if any(ord(c) > 127 for c in marker) else "tiro"
                    marker_left = body - pymupdf.get_text_length(marker, fontname=marker_font, fontsize=size) - size * .5
                    marker_axes.append(marker_left)
                    page.insert_text((marker_left, baseline), marker, fontname=marker_font, fontsize=size)
                    for row in range(4):
                        text = line if row < 3 else "The result is useful."
                        page.insert_text((body, baseline), text, fontname="tiro", fontsize=size)
                        source_lines.append((marker + " " if row == 0 else "") + text)
                        baseline += size * 1.2
                    translations.append(marker + " " + ("条目正文换行时应始终与编号后的正文起点对齐" * 30)[:length] + "。")
                if tail:
                    for row in range(4):
                        page.insert_text((left + (size if row == 0 else 0), baseline), line,
                                         fontname="tiro", fontsize=size)
                        source_lines.append(line)
                        baseline += size * 1.2
                    translations.append(("普通段落应恢复原文的首行缩进及正文左边界" * 30)[:length] + "。")
                region = pymupdf.Rect(left, 60 - size * 1.1, left + width, baseline - size * .8)
                key = f"column-{column}"
                axes[key] = {"markers": marker_axes, "body": body, "left": left}
                if broken:
                    translations[0] = "编号丢失后的译文。"
                blocks.append({"id": key, "text": "\n".join(source_lines), "translation": "\n".join(translations),
                               "target": "简体中文", "bbox": list(region), "_members": [key]})
            page.set_rotation(rotation)
            for block in blocks:
                box = pymupdf.Rect(block["bbox"]) * page.rotation_matrix
                block["bbox"] = [box.x0 / page.rect.width, box.y0 / page.rect.height,
                                 box.x1 / page.rect.width, box.y1 / page.rect.height]
            pdf.save(path)
        doc = {"pages": [{"number": 1, "blocks": blocks}]}
        before = copy.deepcopy(doc)
        raw, report = pdf_layout.typeset(path, doc, "简体中文")
        self.assertEqual(doc, before)
        result = pymupdf.open(stream=raw, filetype="pdf")
        self.addCleanup(result.close)
        self.assertEqual(result[0].rotation, rotation)
        for block in blocks:
            if block["id"] in report["placements"]:
                axes[block["id"]]["placed"] = pdf_layout.source_rect(result[0], {"bbox": report["placements"][block["id"]]})
        result[0].set_rotation(0)
        return path, result[0], report, blocks, axes

    def test_numbered_items_and_following_prose_keep_their_own_axes(self):
        for columns, size, rotation, cropped in ((1, 10, 0, False), (2, 10, 90, True),
                                                  (3, 12, 180, False), (1, 7, 270, True)):
            with self.subTest(columns=columns, size=size, rotation=rotation):
                _, page, report, blocks, axes = self.fixture(columns=columns, size=size, rotation=rotation, cropped=cropped)
                self.assertEqual(report["preserved"], 0, report["issues"])
                self.assertEqual(report["scaled"], 0, report)
                for block in blocks:
                    axis = axes[block["id"]]
                    local = [chars for _, chars in visual_rows(page, axis["placed"])]
                    markers = [i for i, chars in enumerate(local) if chars[0]["c"] in "234" and chars[1]["c"] == ")"]
                    self.assertEqual(len(markers), 3)
                    prose = next(i for i, chars in enumerate(local) if "".join(c["c"] for c in chars).startswith("普通段落"))
                    inset = local[prose + 1][0]["bbox"][0] - axis["left"]
                    self.assertLessEqual(abs(inset), 1.2)
                    for item, row_index in enumerate(markers):
                        chars = local[row_index]
                        self.assertAlmostEqual(chars[0]["bbox"][0], axis["markers"][item] + inset, delta=.1)
                        first_body = next(c for c in chars if "\u3400" <= c["c"] <= "\u9fff")
                        self.assertAlmostEqual(first_body["bbox"][0], axis["body"] + inset, delta=.1)
                        self.assertAlmostEqual(local[row_index + 1][0]["bbox"][0], axis["body"] + inset, delta=.1)
                    self.assertAlmostEqual(local[prose][0]["bbox"][0], axis["left"] + size + inset, delta=.1)
                expected = Counter(c for b in blocks for c in b["translation"] if not c.isspace())
                self.assertFalse(expected - Counter(c for c in page.get_text() if not c.isspace()))

    def test_ambiguous_numbering_preserves_the_complete_source_block(self):
        path, page, report, _, _ = self.fixture(columns=1, broken=True)
        self.assertEqual(report["preserved"], 1)
        self.assertIn("列表", report["issues"][0]["message"])
        with pymupdf.open(path) as original:
            self.assertEqual(page.get_text(), original[0].get_text())
            self.assertEqual(page.get_pixmap().samples, original[0].get_pixmap().samples)

    def test_marker_variants_restore_source_spelling_and_keep_soft_wraps(self):
        for source, translated in (("2)", "(2)"), ("1)", "1）"), ("a)", "a."), ("(iv)", "iv."), ("•", "·"), ("一、", "一、")):
            with self.subTest(source=source):
                structure = [{"marker": source, "left": 0, "body": 15, "indent": 0,
                              "alignment": "left", "marker_bold": False}]
                text, parts = translated_list_layout(translated + " 第一行\n续行。", structure, pdf_layout.join_soft_lines)
                self.assertEqual(text, source + " 第一行 续行。")
                self.assertEqual(parts[0]["prefix_end"], len(source) + 1)

    def test_wrong_or_duplicate_item_ids_are_not_silently_reassigned(self):
        structure = [{"marker": marker} for marker in ("2)", "3)")]
        for translation in ("2) 一\n2) 二", "3) 一\n2) 二", "2) 一", "2) 一\n3) 二\n4) 三"):
            with self.subTest(translation=translation), self.assertRaises(ListLayoutError):
                translated_list_layout(translation, structure, pdf_layout.join_soft_lines)

    def test_letter_roman_and_multidigit_markers_use_source_geometry(self):
        for markers in (("a)", "b)", "c)"), ("(i)", "(ii)", "(iii)"), ("9.", "10.", "11."),
                        ("（一）", "（二）", "（三）")):
            with self.subTest(markers=markers):
                _, page, report, _, _ = self.fixture(columns=1, markers=markers, tail=False)
                self.assertEqual(report["preserved"], 0, report["issues"])
                text = page.get_text()
                for marker in markers:
                    self.assertIn(marker, text)
                chinese = [s for b in page.get_text("dict")["blocks"] if b["type"] == 0
                           for line in b["lines"] for s in line["spans"]
                           if any("\u3400" <= c <= "\u9fff" for c in s["text"])]
                self.assertTrue(chinese)
                self.assertTrue(all(not s["flags"] & 16 for s in chinese))

    def test_link_crossing_a_paragraph_boundary_is_reported_as_ambiguous(self):
        structure = [{"marker": marker, "left": 0, "body": 15, "indent": 0,
                      "alignment": "left", "marker_bold": False} for marker in ("2)", "3)")]
        text, paragraphs = translated_list_layout("2) First item.\n3) Next item.", structure, pdf_layout.join_soft_lines)
        with self.assertRaises(ListLayoutError):
            pdf_layout.inline_math_markup(text, [], pymupdf.Archive(), paragraphs=paragraphs,
                                          links=[{"kind": "uri", "uri": "https://example.org",
                                                  "needle": pdf_layout.link_needle(text)}])

    def test_decimals_abbreviations_and_run_in_headings_are_not_lists(self):
        cases = [((40, "0.5 metres is the distance used in this experiment."),
                  (40, "0.6 metres is the next distance used in the experiment.")),
                 ((40, "(EKF) is an abbreviation used in this paragraph."),
                  (40, "(MCS) is another abbreviation used in this paragraph.")),
                 ((50, "1) Methods for the control of the robot"),
                  (40, "The paragraph continues at the normal column margin."),
                  (50, "2) Results for the control of the robot"),
                  (40, "The next paragraph also uses the normal margin."))]
        for lines in cases:
            with self.subTest(lines=lines), pymupdf.open() as pdf:
                page = pdf.new_page(width=400, height=300)
                for index, (left, text) in enumerate(lines):
                    page.insert_text((left, 60 + index * 12), text, fontname="tiro", fontsize=10)
                native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
                rect = pymupdf.Rect(40, 48, 370, 110)
                style, reason = pdf_layout.inspect_block(native, rect, combine=True)
                self.assertIsNone(reason)
                self.assertIsNone(source_list_structure(page, style, rect))

    def test_extra_source_spacing_between_items_is_retained(self):
        _, baseline, before_report, _, _ = self.fixture(columns=1)
        _, spaced, after_report, _, _ = self.fixture(columns=1, gap=8)
        self.assertEqual(before_report["preserved"], 0, before_report["issues"])
        self.assertEqual(after_report["preserved"], 0, after_report["issues"])
        for order, marker in enumerate(("2)", "3)", "4)")):
            before = next(word for word in baseline.get_text("words") if word[4] == marker)
            after = next(word for word in spaced.get_text("words") if word[4] == marker)
            self.assertAlmostEqual(after[1] - before[1], order * 8, delta=.1)


if __name__ == "__main__":
    unittest.main()
