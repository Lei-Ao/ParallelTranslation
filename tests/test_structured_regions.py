"""Visual-structure regressions: anchors, source weight and typography hierarchy."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
import app as service
import pdf_layout
from pdf_structures import anchored_lines, grid_members


class StructuredRegionTests(unittest.TestCase):
    def render(self, pdf, translate):
        pages = []
        for n, page in enumerate(pdf):
            blocks = []
            for i, raw in enumerate(page.get_text('blocks')):
                if raw[6] != 0:
                    continue
                box = pymupdf.Rect(raw[:4]) * page.rotation_matrix
                blocks.append({'id': f'{n}-{i}', 'text': raw[4].strip(),
                               'translation': translate(raw[4].strip()), 'target': '简体中文',
                               'bbox': [box.x0 / page.rect.width, box.y0 / page.rect.height,
                                        box.x1 / page.rect.width, box.y1 / page.rect.height]})
            pages.append({'number': n + 1, 'blocks': blocks})
        document = {'pages': pages}
        before = copy.deepcopy(document)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.pdf'
            source.write_bytes(pdf.tobytes())
            raw, report = pdf_layout.typeset(source, document, '简体中文')
        self.assertEqual(document, before)
        return pymupdf.open(stream=raw, filetype='pdf'), report

    @staticmethod
    def spans(page):
        return [s for b in page.get_text('dict')['blocks'] if b['type'] == 0
                for l in b['lines'] for s in l['spans']]

    def test_ticks_and_unchanged_legends_keep_anchors_in_rotated_pages(self):
        for rotation, width, size in [(0, 600, 8), (90, 740, 10), (180, 600, 8), (270, 740, 10)]:
            with self.subTest(rotation=rotation), pymupdf.open() as pdf:
                page = pdf.new_page(width=width, height=700)
                page.draw_rect((70, 80, 350, 200), fill=(.9, .9, .9))
                for x, label in [(80, '0.0'), (200, '2.0'), (320, '4.0')]:
                    page.insert_text((x, 214), label, fontsize=size)
                page.insert_text((190, 214 + size * 1.3), 'Time (s)', fontsize=size)
                page.draw_rect((70, 250, 350, 275), fill=(.95, .95, .95))
                for x, label in [(90, 'Alpha'), (190, 'Beta'), (290, 'Gamma')]:
                    page.insert_text((x, 264), label, fontsize=size)
                expected = {s['text']: s['origin'] for s in self.spans(page)
                            if s['text'] in {'0.0', '2.0', '4.0', 'Alpha', 'Beta', 'Gamma'}}
                page.set_rotation(rotation)
                output, report = self.render(pdf, lambda t: t.replace('Time (s)', '时间 (s)'))
                with output:
                    self.assertIn('时间', output[0].get_text(), report)
                    for s in self.spans(output[0]):
                        if s['text'] in expected:
                            self.assertEqual(s['origin'], expected[s['text']])
                    self.assertEqual(output[0].rotation, rotation)

    def test_subplot_titles_keep_separate_centers(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=500, height=500)
            page.draw_rect((50, 90, 220, 250), fill=(.9, .9, .9))
            page.draw_rect((260, 90, 440, 250), fill=(.9, .9, .9))
            page.insert_text((100, 80), 'First Model', fontsize=10)
            page.insert_text((300, 80), 'Second Model', fontsize=10)
            before = self.spans(page)
            output, report = self.render(pdf, lambda t: t.replace('First Model', '第一模型').replace('Second Model', '第二模型'))
            with output:
                after = self.spans(output[0])
                for old, word in zip(before, ['第一模型', '第二模型']):
                    new = next(s for s in after if s['text'] == word)
                    self.assertAlmostEqual((old['bbox'][0] + old['bbox'][2]) / 2,
                                           (new['bbox'][0] + new['bbox'][2]) / 2, delta=.3)
                    self.assertAlmostEqual(old['origin'][1], new['origin'][1], delta=.1)

    def test_linked_figure_labels_keep_complete_source_and_working_links(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                with patch.object(service, 'DATA', directory), pymupdf.open() as pdf:
                    page = pdf.new_page(width=500, height=400)
                    page.draw_rect((50, 90, 220, 250), fill=(.9, .9, .9))
                    page.draw_rect((260, 90, 440, 250), fill=(.9, .9, .9))
                    page.insert_text((100, 80), 'First Model', fontsize=10)
                    page.insert_text((300, 80), 'Second Model', fontsize=10)
                    label = page.search_for('First Model')[0]
                    page.insert_link({'kind': pymupdf.LINK_URI, 'from': label, 'uri': 'https://example.org/model'})
                    page.set_rotation(rotation)
                    key, document = service.extract_document(pdf.tobytes(), 'linked-labels.pdf')
                    for block in service.all_blocks(document):
                        block.update(translation=block['text'].replace('First Model', '第一模型')
                                     .replace('Second Model', '第二模型'), target='简体中文')
                    report = pdf_layout.build_layout(directory / key, document, '简体中文')
                    with pymupdf.open(directory / key / 'layout' / (report['revision'] + '.pdf')) as output:
                        self.assertIn('First Model', output[0].get_text())
                        self.assertIn('Second Model', output[0].get_text())
                        self.assertNotIn('第一模型', output[0].get_text())
                        self.assertEqual([link['uri'] for link in output[0].get_links()], ['https://example.org/model'])
                    self.assertTrue(any('链接' in issue['message'] for issue in report['issues']), report)

    def test_ambiguous_label_translation_preserves_whole_group(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=500, height=500)
            page.draw_rect((50, 90, 450, 250), fill=(.9, .9, .9))
            page.insert_text((100, 80), 'First Model', fontsize=10)
            page.insert_text((300, 80), 'Second Model', fontsize=10)
            output, report = self.render(pdf, lambda _: '无法逐项对应的合并译文')
            with output:
                self.assertIn('First Model', output[0].get_text())
                self.assertIn('Second Model', output[0].get_text())
                self.assertTrue(any('逐项' in i['message'] for i in report['issues']))

    def test_author_grid_keeps_original_names_and_monospace_emails(self):
        for columns, width in [(2, 600), (3, 800)]:
            with self.subTest(columns=columns), pymupdf.open() as pdf:
                page = pdf.new_page(width=width, height=650)
                for column in range(columns):
                    center = width * (column + .5) / columns
                    for label, y, size, font in [('Jane Doe', 90, 12, 'tiro'),
                                                ('Example University', 105, 10, 'tiro'),
                                                (f'user{column}@example.org', 120, 8, 'cour')]:
                        length = pymupdf.get_text_length(label, fontname=font, fontsize=size)
                        page.insert_text((center - length / 2, y), label, fontsize=size, fontname=font)
                output, report = self.render(pdf, lambda t: t.replace('Example University', '示例大学'))
                with output:
                    spans = self.spans(output[0])
                    names = [s for s in spans if s['text'] == 'Jane Doe']
                    emails = [s for s in spans if '@' in s['text']]
                    self.assertEqual(len(names), columns)
                    self.assertEqual(len(emails), columns)
                    self.assertEqual(output[0].get_text().count('示例大学'), columns, report)
                    self.assertTrue(all(abs(s['size'] - 12) < .01 for s in names))
                    self.assertTrue(all('Courier' in s['font'] and abs(s['size'] - 8) < .01 for s in emails))
                    self.assertLess(max(s['origin'][1] for s in emails) - min(s['origin'][1] for s in emails), .01)

    def test_bold_prose_preserved_but_long_run_in_label_does_not_bolden_body(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=650)
            page.insert_textbox((50, 50, 550, 150), 'Abstract: The method is useful for the control of a robot. ' * 5,
                                fontsize=10, fontname='tibo')
            page.insert_text((50, 200), 'Important experimental design and implementation details:', fontsize=10, fontname='tibo')
            page.insert_text((50, 213), 'The result is good.', fontsize=10, fontname='tiro')
            output, report = self.render(pdf, lambda t: '摘要：这是完整加粗的研究摘要。' * 6
                                         if t.startswith('Abstract') else '实验说明：这部分正文保持常规字重。')
            with output:
                spans = self.spans(output[0])
                abstract = [s for s in spans if '摘要' in s['text']]
                body = [s for s in spans if '正文' in s['text']]
                self.assertTrue(abstract and body, report)
                self.assertTrue(all(s['flags'] & 16 for s in abstract))
                self.assertTrue(all(not s['flags'] & 16 for s in body))

    def test_split_heading_without_artwork_is_not_a_label_group(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=500, height=500)
            page.insert_text((50, 80), '2.1', fontsize=12)
            page.insert_text((130, 80), 'Methodology', fontsize=12)
            native = [b for b in page.get_text('dict')['blocks'] if b['type'] == 0]
            rect = pymupdf.Rect(40, 60, 400, 90)
            self.assertIsNone(anchored_lines(native, rect, [], set(), pdf_layout.is_prose, lambda _: False))

    def test_math_font_near_artwork_stays_on_formula_path(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=500, height=500)
            page.insert_text((60, 80), 'Alpha', fontsize=8)
            page.insert_text((180, 80), 'Beta', fontsize=8)
            native = [b for b in page.get_text('dict')['blocks'] if b['type'] == 0]
            for b in native:
                for line in b['lines']:
                    for span in line['spans']:
                        span['font'] = 'CMMI10'
            self.assertIsNone(anchored_lines(native, pymupdf.Rect(40, 60, 250, 90),
                                            [pymupdf.Rect(40, 90, 260, 250)], set(),
                                            pdf_layout.is_prose, lambda _: False))

    def test_short_body_recovers_size_without_crossing_fixed_artwork(self):
        for fixed in (False, True):
            with self.subTest(fixed=fixed), pymupdf.open() as pdf:
                page = pdf.new_page(width=600, height=650)
                page.insert_text((50, 70), 'Here we explain the method and its parameters.\n'
                                 'The description continues on this second line.', fontsize=10,
                                 fontname='tiro', lineheight=1.2)
                page.insert_text((50, 100), '\n'.join(['The experiment evaluates the controller and its performance.'] * 12),
                                 fontsize=10, fontname='tiro', lineheight=1.2)
                if fixed:
                    page.draw_rect((48, 86, 295, 87), fill=(0, 0, 0))
                output, report = self.render(pdf, lambda t: '在此我们详细介绍如何利用优化方法为不同控制器选择适当的参数以及明确实验条件和评价标准。'
                                             if t.startswith('Here') else '实验验证了控制器的性能，结果表明这种方法是有效的。' * 2)
                with output:
                    first = [s for s in self.spans(output[0]) if '在此' in s['text']]
                    self.assertTrue(first, report)
                    if not fixed:
                        self.assertAlmostEqual(first[0]['size'], 10, delta=.01)
                    else:
                        self.assertLess(first[0]['size'], 9.9)
                    self.assertIn('适当的参数', output[0].get_text().replace('\n', ''))


if __name__ == '__main__':
    unittest.main()
