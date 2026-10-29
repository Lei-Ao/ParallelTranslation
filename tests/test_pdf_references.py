import asyncio
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pymupdf
from fastapi import HTTPException
import app as service
import pdf_layout
from pdf_references import (clean_text, discover, format_translation,
                            normalize_document, reference_fallback_pages, split_reference,
                            translation_source)


def fixture(rotation=0):
    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    for point, text in [
        ((54, 110), 'Existing prose stays unchanged.'),
        ((140, 250), 'REFERENCES'),
        ((54, 280), '[1] A. Example, "Reverse curri-'),
        ((68, 291), 'culum generation," Example Journal, 2020.'),
        ((54, 330), '[2] B. Sample, "A title that'),
        ((68, 341), 'continues'),
        ((313, 70), 'across columns," Example Conference, 2021.'),
        ((313, 120), '[3] C. Sample, "Third title," Example Journal, 2022.'),
    ]:
        page.insert_text(point, text, fontsize=8)
    page.set_rotation(rotation)
    blocks = []
    for i, native in enumerate(page.get_text('blocks')):
        box = pymupdf.Rect(native[:4]) * page.rotation_matrix
        blocks.append({'id': f'p1-b{i}', 'text': native[4].strip(),
                       'bbox': [box.x0/page.rect.width, box.y0/page.rect.height, box.x1/page.rect.width, box.y1/page.rect.height],
                       'translation': '', 'target': '', 'edited': False})
    document = {'id': 'a'*32, 'name': 'references.pdf', 'pages': [{'number': 1, 'blocks': blocks}], 'total_blocks': len(blocks)}
    return pdf, document


class ReferenceTests(unittest.TestCase):
    def document(self, rotation=0):
        pdf, document = fixture(rotation)
        self.addCleanup(pdf.close)
        return pdf, document

    def normalized(self, rotation=0):
        pdf, document = self.document(rotation)
        self.assertTrue(normalize_document(document, pdf))
        return pdf, document

    def entries(self, document):
        return [block for block in document['pages'][0]['blocks'] if block.get('kind') == 'reference']

    def test_clean_broken_word_accents_url_and_page_range(self):
        self.assertEqual(clean_text('Re-\nverse M. M¨uller https://example.org/\nabc 2666– 2671'),
                         'Reverse M. Müller https://example.org/abc 2666–2671')

    def test_split_title_preserves_original_authors_and_publication(self):
        ref = split_reference('[12] A. Example and B. Test, “Paper title,” Nature, vol. 620, pp. 982–987, 2023.')
        self.assertEqual(ref['title'], 'Paper title')
        self.assertEqual(ref['authors'], 'A. Example and B. Test')
        self.assertEqual(ref['publication'], 'Nature, vol. 620, pp. 982–987, 2023.')
        self.assertEqual(ref['number'], 12)
        self.assertIsNone(split_reference('[1] No safely delimited title, 2020.'))

    def test_discovery_rejoins_title_and_cross_column_entry(self):
        pdf, document = self.document()
        result = discover(pdf)
        self.assertEqual([entry['number'] for entry in result['entries']], [1, 2, 3])
        self.assertEqual(result['entries'][0]['reference']['title'], 'Reverse curriculum generation')
        second = result['entries'][1]
        self.assertEqual(second['reference']['title'], 'A title that continues across columns')
        self.assertEqual({part['column'] for part in second['parts']}, {0, 1})

    def test_noncontiguous_numbers_and_multipage_bibliography_fail_closed(self):
        pdf, document = self.document()
        pdf.new_page()
        self.assertIsNone(discover(pdf))
        pdf.delete_page(-1)
        original = pdf[0].get_text
        # A missing number must not silently eat a bibliography entry.
        with patch('pdf_references._visual_lines', return_value=[
            {'text': 'REFERENCES', 'rect': pymupdf.Rect(50, 50, 150, 60)},
            {'text': '[1] A, "First," Journal, 2020.', 'rect': pymupdf.Rect(50, 80, 200, 90)},
            {'text': '[3] B, "Third," Journal, 2021.', 'rect': pymupdf.Rect(50, 100, 200, 110)},
        ]):
            self.assertIsNone(discover(pdf))

    def test_normalization_backs_up_fragments_and_is_idempotent(self):
        pdf, document = self.normalized()
        self.assertEqual(len(self.entries(document)), 3)
        self.assertTrue(document['reference_legacy']['blocks'])
        before = copy.deepcopy(document)
        self.assertFalse(normalize_document(document, pdf))
        self.assertEqual(document, before)
        self.assertEqual(len({block['id'] for block in document['pages'][0]['blocks']}), document['total_blocks'])

    def test_existing_translations_require_explicit_reviewed_migration(self):
        pdf, document = self.document()
        for block in document['pages'][0]['blocks']:
            if '[1]' in block['text']:
                block.update(translation='已有碎片译文', target='简体中文')
        before = copy.deepcopy(document)
        self.assertFalse(normalize_document(document, pdf))
        self.assertEqual(document, before)
        self.assertTrue(normalize_document(document, pdf, force=True))
        self.assertTrue(any(block['translation'] == '已有碎片译文' for block in document['reference_legacy']['blocks']))

    def test_formatter_only_replaces_title_and_handles_nonchinese(self):
        _, document = self.normalized()
        block = self.entries(document)[0]
        self.assertEqual(translation_source(block), 'Reverse curriculum generation')
        self.assertEqual(format_translation(block, '反向课程生成'), '[1] A. Example. 反向课程生成。Example Journal, 2020.')
        self.assertEqual(format_translation(block, 'Translated title.'), '[1] A. Example. Translated title. Example Journal, 2020.')
        for bad in ('', '题名\n[2] 另一个条目', '[2] 串条', 'A. Example. 作者不应重译', '题名 Example Journal, 2020.'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                format_translation(block, bad)

    def test_prompt_only_exposes_current_title_as_translatable_source(self):
        _, document = self.normalized()
        block = self.entries(document)[1]
        index = service.find_block(document, block['id'])[1]
        prompt = service.build_prompt(document, index, service.TranslateRequest(agent=service.AgentConfig()))
        context = json.loads(prompt.split('以下 JSON 为待翻译内容与参考资料：\n')[1])
        self.assertEqual(context['current_source'], block['reference']['title'])
        self.assertEqual(context['previous'], [])
        self.assertEqual(context['next'], [])
        self.assertEqual(context['read_only_citation'], block['text'])

    def test_single_translation_sends_title_and_restores_metadata(self):
        _, document = self.normalized()
        block = self.entries(document)[0]
        agent = AsyncMock(return_value='反向课程生成')
        with patch.object(service, 'load_doc', return_value=document), patch.object(service, 'doc_dir', return_value=Path('.')), patch.object(service, 'save_doc') as save, patch.object(service, 'invoke_agent', agent):
            result = asyncio.run(service.translate(document['id'], block['id'], service.TranslateRequest(agent=service.AgentConfig())))
        self.assertEqual(agent.await_args.args[2], block['reference']['title'])
        self.assertIn('A. Example. 反向课程生成。Example Journal, 2020.', result['translation'])
        save.assert_called_once()

    def test_batch_translation_only_sends_titles_and_restores_metadata(self):
        _, document = self.normalized()
        blocks = self.entries(document)[:2]
        agent = AsyncMock(return_value=json.dumps({'blocks': [{'id': block['id'], 'translation': '校订题名'} for block in blocks]}))
        with patch.object(service, 'load_doc', return_value=document), patch.object(service, 'doc_dir', return_value=Path('.')), patch.object(service, 'save_doc'), patch.object(service, 'invoke_agent', agent):
            result = asyncio.run(service.translate_batch(document['id'], service.BatchTranslateRequest(agent=service.AgentConfig(), block_ids=[block['id'] for block in blocks])))
        context = json.loads(agent.await_args.args[1].split('参考资料与本次待翻译段落：\n')[1])
        self.assertEqual([item['source'] for item in context['current_blocks']], [block['reference']['title'] for block in blocks])
        for block in result['blocks']:
            self.assertIn(block['reference']['authors'], block['translation'])
            self.assertIn(block['reference']['publication'], block['translation'])

    def test_bad_title_does_not_overwrite_existing_translation(self):
        _, document = self.normalized()
        block = self.entries(document)[0]
        block['translation'] = '旧译文'
        with patch.object(service, 'load_doc', return_value=document), patch.object(service, 'save_doc') as save, patch.object(service, 'invoke_agent', AsyncMock(return_value='[2] 串条')):
            with self.assertRaises(HTTPException):
                asyncio.run(service.translate(document['id'], block['id'], service.TranslateRequest(agent=service.AgentConfig())))
        save.assert_not_called()
        self.assertEqual(block['translation'], '旧译文')

    def test_bad_batch_does_not_save_even_valid_preceding_item(self):
        _, document = self.normalized()
        blocks = self.entries(document)[:2]
        for block in blocks:
            block['translation'] = '旧译文'
        payload = {'blocks': [{'id': blocks[0]['id'], 'translation': '正确题名'},
                              {'id': blocks[1]['id'], 'translation': '[3] 串条'}]}
        agent = AsyncMock(return_value=json.dumps(payload))
        with patch.object(service, 'load_doc', return_value=document), patch.object(service, 'save_doc') as save, patch.object(service, 'invoke_agent', agent):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(service.translate_batch(document['id'], service.BatchTranslateRequest(agent=service.AgentConfig(), block_ids=[block['id'] for block in blocks])))
        self.assertEqual(error.exception.headers['X-Translation-Fallback'], 'single')
        save.assert_not_called()
        self.assertEqual([block['translation'] for block in blocks], ['旧译文', '旧译文'])

    def render(self, rotation=0, overflow=False):
        pdf, document = self.normalized(rotation)
        for block in document['pages'][0]['blocks']:
            if block.get('kind') == 'reference':
                block['translation'] = format_translation(block, 'Translated title') if not overflow else 'X ' * 6000
            else:
                block['translation'] = block['text']
            block['target'] = '简体中文'
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source.pdf'
            pdf.save(source)
            raw, report = pdf_layout.typeset(source, document, '简体中文', typography={'css': '', 'family': 'serif', 'archive': None})
        output = pymupdf.open(stream=raw, filetype='pdf')
        self.addCleanup(output.close)
        return output, report

    def test_layout_has_every_number_once_and_preserves_body(self):
        output, report = self.render()
        self.assertEqual(report['bibliography'][0]['entries'], 3)
        text = output[0].get_text()
        self.assertEqual(sorted(map(int, re.findall(r'^\[(\d+)\]', text, re.M))), [1, 2, 3])
        self.assertEqual(text.count('Translated title'), 3)
        self.assertIn('Existing prose stays unchanged.', text)
        self.assertNotIn('Reverse curri-', text)
        self.assertNotIn('Third title', text)

    def test_reference_layout_preserves_all_rotations(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                output, report = self.render(rotation)
                self.assertEqual(output[0].rotation, rotation)
                self.assertEqual(report['bibliography'][0]['entries'], 3)
                self.assertEqual(output[0].get_text().count('Translated title'), 3)

    def test_overflow_preserves_complete_original_references(self):
        output, report = self.render(overflow=True)
        self.assertFalse(report.get('bibliography'))
        text = output[0].get_text()
        self.assertIn('Reverse curri-', text)
        self.assertIn('across columns', text)
        self.assertIn('Third title', text)
        self.assertTrue(any(item['kind'] == 'preserved' for item in report['issues']))


class ReferenceFallbackTests(unittest.TestCase):
    def test_unrebuildable_bibliography_is_preserved_as_one_region(self):
        with tempfile.TemporaryDirectory() as temp, pymupdf.open() as pdf:
            page = pdf.new_page(width=612, height=792)
            page.insert_textbox((48, 80, 286, 300),
                                "Left column prose stays translated. " * 12, fontsize=9)
            page.insert_text((330, 70), "REFERENCES", fontsize=8)
            entries = ['[1] A. Author, "A paper title," Venue, 2020.',
                       '[2] B. Author, A book title without quotes. Publisher, 2021.',
                       '[3] C. Author, "Another paper," Venue, 2022.']
            for index, line in enumerate(entries):
                page.insert_text((330, 90 + index * 12), line, fontsize=8)
            blocks = []
            for index, native in enumerate(page.get_text("blocks")):
                text = native[4].strip()
                if text.startswith("Left"):
                    translation = "左栏正文译文。" * 6
                elif text.startswith("REFERENCES"):
                    translation = "参考文献"
                else:
                    translation = "参考文献条目译文。" * 8
                blocks.append({"id": f"p1-b{index}", "text": text, "translation": translation,
                               "target": "简体中文", "edited": False,
                               "bbox": [native[0] / 612, native[1] / 792,
                                        native[2] / 612, native[3] / 792]})
            path = Path(temp) / "source.pdf"
            pdf.save(path)
            raw, report = pdf_layout.typeset(path, {"pages": [{"number": 1, "blocks": blocks}]},
                                             "简体中文")
        words = [block["text"] for block in blocks]
        self.assertTrue(any("A book title" in text for text in words))
        self.assertTrue(any(issue["kind"] == "reference" for issue in report["issues"]), report["issues"])
        self.assertGreaterEqual(report["preserved"], 1)
        output = pymupdf.open(stream=raw, filetype="pdf")
        self.addCleanup(output.close)
        rendered = output[0]
        right = rendered.get_text(clip=pymupdf.Rect(306, 85, 612, 420))
        left = rendered.get_text(clip=pymupdf.Rect(0, 80, 300, 420))
        self.assertIn("A book title without quotes", right)
        self.assertNotRegex(right, r"[\u3400-\u9fff]")
        self.assertIn("左栏", left)


    def test_continuation_pages_use_the_heading_page_fallback(self):
        with tempfile.TemporaryDirectory() as temp, pymupdf.open() as pdf:
            first = pdf.new_page(width=612, height=792)
            first.insert_textbox((48, 80, 286, 300), "Left column prose. " * 10, fontsize=9)
            first.insert_text((330, 70), "REFERENCES", fontsize=8)
            first.insert_text((330, 90), '[1] A. Author, "First," Venue, 2020.', fontsize=8)
            first.insert_text((330, 102), '[2] B. Author, "Second," Venue, 2021.', fontsize=8)
            second = pdf.new_page(width=612, height=792)
            for left, number in ((60, 3), (330, 5)):
                second.insert_text((left, 80), f'[{number}] C. Author, "Third," Venue, 2022.', fontsize=8)
                second.insert_text((left, 92), f'[{number + 1}] D. Author, "Fourth," Venue, 2023.', fontsize=8)
            mapping = reference_fallback_pages(pdf)
            self.assertIn(0, mapping)
            self.assertIn(1, mapping)
            blocks = []
            for index, native in enumerate(second.get_text("blocks")):
                blocks.append({"id": f"p2-b{index}", "text": native[4].strip(),
                               "translation": "参考文献条目译文。" * 8, "target": "简体中文",
                               "edited": False,
                               "bbox": [native[0] / 612, native[1] / 792,
                                        native[2] / 612, native[3] / 792]})
            path = Path(temp) / "source.pdf"
            pdf.save(path)
            raw, report = pdf_layout.typeset(
                path, {"pages": [{"number": 2, "blocks": blocks}]}, "简体中文",
                page_numbers=[1], reference_fallbacks=mapping)
        self.assertTrue(any(issue["kind"] == "reference" for issue in report["issues"]), report["issues"])
        output = pymupdf.open(stream=raw, filetype="pdf")
        self.addCleanup(output.close)
        text = output[0].get_text()
        self.assertIn("[3] C. Author", text)
        self.assertNotRegex(text, r"[\u3400-\u9fff]")


if __name__ == '__main__':
    unittest.main()
