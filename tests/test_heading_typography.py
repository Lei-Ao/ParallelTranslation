"""Headings inherit independent source weight and slant, including run-ins."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

import app as service
import pdf_layout
import pdf_typography


class HeadingTypographyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.data = patch.object(service, 'DATA', self.directory)
        self.data.start()
        self.addCleanup(self.data.stop)

    def spans(self, output, report, block):
        rect = pdf_layout.source_rect(output, {'bbox': report['placements'][block['id']]})
        return [s for b in output.get_text('dict', clip=rect)['blocks'] if b.get('type') == 0
                for line in b['lines'] for s in line['spans'] if s['text'].strip()]

    def test_heading_weight_and_italic_follow_source_across_columns_and_rotation(self):
        for columns, width, rotation in ((1, 420, 0), (2, 595, 90), (1, 500, 180), (2, 650, 270)):
            for font, bold, italic in (('tiro', False, False), ('tiit', False, True),
                                       ('tibo', True, False), ('tibi', True, True),
                                       ('heit', False, True)):
                with self.subTest(columns=columns, width=width, rotation=rotation, font=font):
                    with pymupdf.open() as source:
                        page = source.new_page(width=width, height=360)
                        for column in range(columns):
                            left = 30 + column * width / columns
                            page.insert_text((left, 70), 'B. Controller Design', fontname=font, fontsize=10)
                            page.insert_textbox((left, 90, left + width / columns - 55, 180),
                                'The controller follows the desired trajectory. '
                                'The results are compared with the reference.', fontname='tiro', fontsize=10)
                        page.set_rotation(rotation)
                        raw = source.tobytes()
                    doc_id, document = service.extract_document(raw, 'heading-style.pdf')
                    for block in service.all_blocks(document):
                        block.update(translation='B. 控制器设计' if block['text'].startswith('B.') else
                                     '控制器跟踪期望轨迹，并将结果与参考值进行比较。', target='简体中文')
                    raw, report = pdf_layout.typeset(self.directory / doc_id / 'source.pdf', document, '简体中文')
                    self.assertFalse(report['issues'], report)
                    with pymupdf.open(stream=raw, filetype='pdf') as output:
                        for block in service.all_blocks(document):
                            spans = self.spans(output[0], report, block)
                            self.assertTrue(spans)
                            heading = block['text'].startswith('B.')
                            self.assertTrue(all(bool(s['flags'] & 16) == (bold if heading else False) for s in spans), spans)
                            self.assertTrue(all(bool(s['flags'] & 2) == (italic if heading else False) for s in spans), spans)
                            self.assertTrue(all('ParallelBook' in s['font'] for s in spans), spans)

    def test_italic_run_in_label_does_not_slant_or_bolden_following_prose(self):
        with pymupdf.open() as source:
            page = source.new_page(width=460, height=250)
            page.insert_htmlbox((35, 45, 425, 200), '<div style="font:10pt Times,serif">'
                '1) <i>Learning the control policy:</i> The controller is trained in simulation '
                'with the same objective and data. Its behavior is evaluated on several tasks.</div>')
            raw = source.tobytes()
        doc_id, document = service.extract_document(raw, 'run-in-style.pdf')
        for block in service.all_blocks(document):
            block.update(translation='1) 控制策略学习：控制器在仿真中使用相同目标与数据进行训练，随后在多种任务上评估。', target='简体中文')
        raw, report = pdf_layout.typeset(self.directory / doc_id / 'source.pdf', document, '简体中文')
        self.assertFalse(report['issues'], report)
        with pymupdf.open(stream=raw, filetype='pdf') as output:
            spans = [s for b in output[0].get_text('dict')['blocks'] if b.get('type') == 0
                     for line in b['lines'] for s in line['spans']]
            italics = ''.join(s['text'] for s in spans if s['flags'] & 2)
            self.assertEqual(italics, '控制策略学习：')
            self.assertTrue(all(not s['flags'] & 16 for s in spans), spans)

    def test_oblique_outlines_preserve_weight_unicode_and_advance(self):
        from fontTools.ttLib import TTFont
        source = pdf_typography.font_source()
        if source is None:
            self.skipTest('No local CJK font')
        for upright in pdf_typography.subset_fonts('中文AB', source[0], source[1]):
            with TTFont(io.BytesIO(upright)) as regular, TTFont(io.BytesIO(pdf_typography.oblique_font(upright))) as italic:
                self.assertEqual(regular.getBestCmap(), italic.getBestCmap())
                self.assertEqual(regular['OS/2'].usWeightClass, italic['OS/2'].usWeightClass)
                self.assertEqual(italic['post'].italicAngle, -12)
                self.assertTrue(italic['OS/2'].fsSelection & 1)
                name = regular.getBestCmap()[ord('中')]
                self.assertEqual(regular['hmtx'][name][0], italic['hmtx'][name][0])
                before = list(regular['glyf'][name].getCoordinates(regular['glyf'])[0])
                after = list(italic['glyf'][name].getCoordinates(italic['glyf'])[0])
                self.assertNotEqual(before, after)

    def test_long_italic_run_in_keeps_short_description_upright(self):
        with pymupdf.open() as source:
            page = source.new_page(width=460, height=250)
            page.insert_htmlbox((35, 45, 425, 200), '<div style="font:10pt Times,serif">'
                '1) <i>Learning the Control Policy in Several Challenging Environments:</i> We train.</div>')
            raw = source.tobytes()
        doc_id, document = service.extract_document(raw, 'long-run-in.pdf')
        for block in service.all_blocks(document):
            block.update(translation='1) 复杂环境中的控制策略学习：随后训练。', target='简体中文')
        raw, report = pdf_layout.typeset(self.directory / doc_id / 'source.pdf', document, '简体中文')
        self.assertFalse(report['issues'], report)
        with pymupdf.open(stream=raw, filetype='pdf') as output:
            spans = [s for b in output[0].get_text('dict')['blocks'] if b.get('type') == 0
                     for line in b['lines'] for s in line['spans']]
            self.assertEqual(''.join(s['text'] for s in spans if s['flags'] & 2), '复杂环境中的控制策略学习：')
            self.assertTrue(all(not s['flags'] & 16 for s in spans))

    def test_leading_math_font_does_not_supply_an_italic_label(self):
        style = {'lines': [{'spans': [
            {'text': 'state:', 'font': 'CMMI10', 'flags': 2},
            {'text': ' The reference is measured.', 'font': 'Times-Roman', 'flags': 0}]}]}
        self.assertEqual(pdf_layout.inline_bold_hints(style, '', emphasis_flag=2), [])


if __name__ == '__main__':
    unittest.main()
