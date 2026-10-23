"""Caption alignment, mixed-script terms and visible paragraph whitespace."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
import app as service
import pdf_layout
from pdf_flow import text_ink_bounds
from pdf_typography import space_cjk_latin


class TermSpacingTests(unittest.TestCase):
    def test_touching_terms_and_compound_names_without_splitting_tokens(self):
        cases = {
            "基于Hopf纤维化，在JAX中训练BLCM模型。": "基于 Hopf 纤维化，在 JAX 中训练 BLCM 模型。",
            "这是3D模型及SE3控制和ResNet-50网络。": "这是 3D 模型及 SE3 控制和 ResNet-50 网络。",
            "变量x_i的说明与café模型。": "变量 x_i 的说明与 café 模型。",
            "已有 Hopf 空格，图1、32%、（RMSE）和0.35m。": "已有 Hopf 空格，图1、32%、（RMSE）和0.35m。",
            "公式x+y=z，引用[12]与纯中文。": "公式 x+y=z，引用[12]与纯中文。",
            "中文`x_i中文`代码 https://example.org/中文/path user@example.com":
                "中文`x_i中文`代码 https://example.org/中文/path user@example.com",
            "An English paragraph with Hopf and JAX.": "An English paragraph with Hopf and JAX.",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(space_cjk_latin(original), expected)
                self.assertEqual(space_cjk_latin(expected), expected)


class CaptionSpacingTests(unittest.TestCase):
    def test_single_caption_uses_figure_or_column_axis_not_its_own_center(self):
        for center in (160, 300, 440):
            for centered in (True, False):
                rect = pymupdf.Rect(center - 55 if centered else center - 100, 170,
                                    center + 55 if centered else center + 10, 180)
                style = {"size": 10, "lines": [{"spans": [{"text": "Figure 1: A view of the method.",
                    "bbox": list(rect), "origin": (rect.x0, 178), "size": 10}]}]}
                graphic = pymupdf.Rect(center - 100, 60, center + 100, 160)
                alignment, _, _ = pdf_layout.source_paragraph_style(
                    style, rect, "Figure 1: A view of the method.", 600, graphics=[graphic])
                self.assertEqual(alignment, "center" if centered else "left")

    def test_caption_center_survives_page_rotation_and_translation_length(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.object(service, "DATA", root), pymupdf.open() as original:
                    page = original.new_page(width=500, height=500)
                    page.draw_rect((40, 40, 220, 140), fill=(.4, .6, .8))
                    caption = "Fig. 1: A view of the robot."
                    width = pymupdf.get_text_length(caption, fontsize=8)
                    page.insert_text((130 - width / 2, 155), caption, fontsize=8)
                    page.set_rotation(rotation)
                    doc_id, doc = service.extract_document(original.tobytes(), "caption.pdf")
                    block = doc['pages'][0]['blocks'][0]
                    block.update(translation="图1：Hopf模型。", target="简体中文")
                    unchanged = copy.deepcopy(doc)
                    raw, report = pdf_layout.typeset(root/doc_id/'source.pdf', doc, "简体中文")
                    self.assertEqual(report['replaced'], 1, report)
                    self.assertEqual(doc, unchanged)
                    with pymupdf.open(stream=raw, filetype='pdf') as result:
                        self.assertEqual(result[0].rotation, rotation)
                        result[0].set_rotation(0)
                        spans = [s for b in result[0].get_text('dict')['blocks'] if b['type']==0
                                 for line in b['lines'] for s in line['spans']]
                        self.assertAlmostEqual((min(s['bbox'][0] for s in spans)
                                                + max(s['bbox'][2] for s in spans))/2, 130, delta=1)
                        self.assertIn('Hopf ', result[0].get_text())

    def test_ambiguous_paint_operations_do_not_measure_neighbor_text(self):
        region = pymupdf.Rect(40, 50, 240, 100)
        self.assertIsNone(text_ink_bounds([('fill-text', (40, 50, 500, 70))], region))
        self.assertIsNone(text_ink_bounds([('ignore-text', (40, 50, 230, 90))], region))

    def test_visible_gaps_match_original_across_columns_sizes_and_rotation(self):
        prose = "The method is used for control of a robot in flight."
        for columns, size, rotation in ((1, 8, 0), (2, 10, 90), (3, 12, 270)):
            with self.subTest(columns=columns), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch.object(service, 'DATA', root), pymupdf.open() as source:
                    page = source.new_page(width=columns*340, height=600)
                    for col in range(columns):
                        for top in (60, 60+size*1.3*6+20):
                            page.insert_text((30+col*340, top), '\n'.join([prose]*6),
                                             fontname='tiro', fontsize=size, lineheight=1.3)
                    page.set_rotation(rotation)
                    doc_id, doc = service.extract_document(source.tobytes(), 'gaps.pdf')
                    for b in doc['pages'][0]['blocks']:
                        b.update(translation='基于Hopf的控制方法保持段落间距。'*2, target='简体中文')
                    raw, report = pdf_layout.typeset(root/doc_id/'source.pdf', doc, '简体中文')
                    self.assertEqual(report['replaced'], columns*2, report)
                    self.assertEqual(len(report['flow']), columns)
                    source_bounds = {b['id']:pdf_layout.source_rect(page,b) for b in doc['pages'][0]['blocks']}
                    page.set_rotation(0)
                    with pymupdf.open(stream=raw, filetype='pdf') as output:
                        placements = {key:pdf_layout.source_rect(output[0], {'bbox':value})
                                      for key,value in report['placements'].items()}
                        output[0].set_rotation(0)
                        def painted_bounds(p, box):
                            # Independent raster measurement catches font-frame
                            # padding even if report calculations agree internally.
                            pix = p.get_pixmap(matrix=pymupdf.Matrix(3,3), clip=box,
                                               colorspace=pymupdf.csGRAY)
                            pixels = pix.samples
                            rows = [y for y in range(pix.height)
                                    if min(pixels[y*pix.stride:y*pix.stride+pix.width]) < 100]
                            return ((pix.y+min(rows))/3, (pix.y+max(rows)+1)/3)
                        for change in report['flow']:
                            before, after = change['previous'][0], change['blocks'][0]
                            source_gap = painted_bounds(page,source_bounds[after])[0]-painted_bounds(page,source_bounds[before])[1]
                            output_gap = painted_bounds(output[0],placements[after])[0]-painted_bounds(output[0],placements[before])[1]
                            self.assertAlmostEqual(source_gap,output_gap,delta=.8)


if __name__ == '__main__':
    unittest.main()
