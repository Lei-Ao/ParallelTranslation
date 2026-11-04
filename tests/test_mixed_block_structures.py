"""Recover structural boundaries inside existing saved extraction blocks."""
import copy
import tempfile
import unittest
from pathlib import Path

import pymupdf

import pdf_layout as layout


PROSE = ('The method is useful for the control of a robot. We compare the results with '
         'the reference in several environments. The experiments use the same objective and data.')
TRANSLATION = '这些方法用于机器人的控制。我们在多种环境中将结果与参考值进行比较，实验使用相同的目标与数据。'


def saved(page, key, text, translation, rect):
    box = pymupdf.Rect(rect) * page.rotation_matrix
    return {'id': key, 'text': text, 'translation': translation, 'target': '简体中文',
            'bbox': [box.x0 / page.rect.width, box.y0 / page.rect.height,
                     box.x1 / page.rect.width, box.y1 / page.rect.height]}


class MixedBlockStructureTests(unittest.TestCase):
    def heading_fixture(self, columns=1, rotation=0, width=520, translation=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with pymupdf.open() as source:
            page = source.new_page(width=width, height=450)
            source_rows = []
            for column in range(columns):
                left, right = 35 + column * width / columns, (column + 1) * width / columns - 35
                text = 'II. RELATED METHODS'
                length = pymupdf.get_text_length(text, fontname='tiro', fontsize=10)
                page.insert_text(((left + right - length) / 2, 70), text, fontname='tiro', fontsize=10)
                page.insert_textbox((left, 86, right, 250), PROSE, fontname='tiro', fontsize=10)
                source_rows.append((left, right))
            native = [b for b in page.get_text('dict')['blocks'] if b['type'] == 0]
            regions = []
            for left, right in source_rows:
                lines = [line for b in native for line in b['lines']
                         if left - .1 <= line['bbox'][0] and line['bbox'][2] <= right + .1]
                region = pymupdf.Rect(lines[0]['bbox'])
                for line in lines[1:]:
                    region |= pymupdf.Rect(line['bbox'])
                regions.append(region)
            page.set_rotation(rotation)
            blocks = []
            for index, region in enumerate(regions):
                block = saved(page, f'paragraph-{index}', 'II. RELATED METHODS\n' + PROSE,
                              translation or 'II. 相关方法\n' + TRANSLATION, region)
                # Existing merged extraction blocks use the same combine path;
                # storage remains one stable paragraph with one translation.
                block['_members'] = [block['id']]
                blocks.append(block)
            source_path = Path(temp.name) / 'source.pdf'
            source.save(source_path)
        return source_path, {'pages': [{'number': 1, 'blocks': blocks}]}, source_rows

    def test_heading_inside_prose_is_centered_and_separated_across_page_geometry(self):
        for columns, width, rotation in ((1, 520, 0), (2, 650, 90), (1, 480, 180), (2, 620, 270)):
            with self.subTest(columns=columns, width=width, rotation=rotation):
                path, document, axes = self.heading_fixture(columns, rotation, width)
                before = copy.deepcopy(document)
                raw, report = layout.typeset(path, document, '简体中文')
                self.assertEqual(document, before)
                self.assertFalse(report['issues'], report)
                self.assertEqual(report['replaced'], columns)
                with pymupdf.open(stream=raw, filetype='pdf') as output:
                    page = output[0]
                    page.set_rotation(0)
                    rows = [line for b in page.get_text('dict')['blocks'] if b.get('type') == 0 for line in b['lines']]
                    titles = [line for line in rows if '相关方法' in ''.join(s['text'] for s in line['spans'])]
                    self.assertEqual(len(titles), columns)
                    for title, (left, right) in zip(sorted(titles, key=lambda l: l['bbox'][0]), axes):
                        text = ''.join(s['text'] for s in title['spans'])
                        self.assertEqual(text.replace(' ', ''), 'II.相关方法')
                        self.assertAlmostEqual((title['bbox'][0] + title['bbox'][2]) / 2, (left + right) / 2, delta=1)
                        body = [line for line in rows if left - 1 <= line['bbox'][0] < right
                                and line['bbox'][1] > title['bbox'][1] + .1]
                        self.assertTrue(body)
                        self.assertGreater(min(line['bbox'][1] for line in body), title['bbox'][3])
                    self.assertEqual(page.get_text().count('这些方法'), columns)

    def test_unmatched_heading_boundary_retains_complete_source_and_reports_reason(self):
        path, document, _ = self.heading_fixture(translation='II. 相关方法 ' + TRANSLATION)
        raw, report = layout.typeset(path, document, '简体中文')
        self.assertEqual(report['preserved'], 1)
        self.assertTrue(any('标题换行' in item['message'] for item in report['issues']))
        with pymupdf.open(stream=raw, filetype='pdf') as output:
            self.assertIn('II. RELATED METHODS', output[0].get_text())
            self.assertIn('We compare', output[0].get_text())
            self.assertNotIn('这些方法', output[0].get_text())

    def test_equation_label_extracted_with_prose_travels_with_equation(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), tempfile.TemporaryDirectory() as temp, pymupdf.open() as source:
                page = source.new_page(width=520, height=420)
                page.insert_text((40, 45), 'The controller is evaluated with the same reference and objective.', fontsize=10)
                page.insert_text((200, 100), 'u = ax + by', fontname='tiit', fontsize=10)
                page.insert_text((400, 106), '(7)', fontname='tiro', fontsize=10)
                page.insert_textbox((40, 125, 420, 210), PROSE, fontname='tiro', fontsize=10)
                native = [b for b in page.get_text('dict')['blocks'] if b['type'] == 0]
                lines = [line for b in native for line in b['lines']]
                line_text = lambda line: ''.join(s['text'] for s in line['spans'])
                equation = next(line for line in lines if line_text(line).startswith('u ='))
                label = next(line for line in lines if line_text(line) == '(7)')
                intro = next(line for line in lines if line_text(line).startswith('The controller'))
                body = [line for line in lines if line['bbox'][1] > label['bbox'][3]]
                body_rect = pymupdf.Rect(label['bbox'])
                for line in body:
                    body_rect |= pymupdf.Rect(line['bbox'])
                page.set_rotation(rotation)
                document = {'pages': [{'number': 1, 'blocks': [
                    saved(page, 'intro', line_text(intro), '控制器采用相同参考值和目标。', intro['bbox']),
                    saved(page, 'equation', 'u = ax + by', 'u = ax + by', equation['bbox']),
                    saved(page, 'explanation', '(7)\n' + PROSE, '(7)\n' + TRANSLATION, body_rect)]}]}
                before = copy.deepcopy(document)
                path = Path(temp) / 'source.pdf'
                source.save(path)
                raw, report = layout.typeset(path, document, '简体中文')
                self.assertEqual(document, before)
                self.assertFalse(report['issues'], report)
                self.assertTrue(set(report['placements']) <= {'intro', 'equation', 'explanation'})
                source_equation = layout.source_rect(source[0], {'bbox': report['source_placements']['equation']})
                source_body = layout.source_rect(source[0], {'bbox': report['source_placements']['explanation']})
                label_box = pymupdf.Rect(label['bbox'])
                label_center = (label_box.tl + label_box.br) / 2
                self.assertTrue(source_equation.contains(label_center))
                self.assertFalse(source_body.contains(label_center))
                with pymupdf.open(stream=raw, filetype='pdf') as output:
                    page = output[0]
                    page.set_rotation(0)
                    words = page.get_text('words')
                    tag = [w for w in words if w[4] == '(7)']
                    self.assertEqual(len(tag), 1)
                    first = next(w for w in words if w[4] == 'u')
                    self.assertAlmostEqual(tag[0][0] - first[0], label['bbox'][0] - equation['bbox'][0], delta=.1)
                    self.assertAlmostEqual(tag[0][1] - first[1], label['bbox'][1] - equation['bbox'][1], delta=.1)
                    self.assertIn('这些方法', page.get_text())


if __name__ == '__main__':
    unittest.main()
