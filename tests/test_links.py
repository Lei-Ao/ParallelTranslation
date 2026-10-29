"""Synthetic-PDF regression tests for link preservation in the layout engine.

The translation layout redacts the original glyphs and typesets the translation
into the same region. Annotations therefore cannot simply stay where they were:
they must follow the translated text. Covered here (contract of ENGINE_VERSION
55 and the reader link report):

* URI annotations re-anchored onto translated URL text (``source_page`` in the
  reader report, normalized coordinates, no annotation over erased source);
* citation ``goto`` annotations re-anchored after edge punctuation is trimmed
  from the needle ("[19]," -> "[19]");
* fully linked blocks (titles) fall back to the whole translated block;
* external links whose text was translated away are dropped and reported as a
  per-page ``kind == "link"`` issue;
* annotations over preserved artwork (images) stay at their original rectangle
  without duplication;
* rotated pages keep working annotations in displayed coordinates;
* translated link text keeps the source span color and is not underlined.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

import app as service
import pdf_layout
from pdf_layout import build_layout


URL = "https://example.org/docs"


def normalized(page, rect):
    bounds = page.rect
    return [rect.x0 / bounds.width, rect.y0 / bounds.height,
            rect.x1 / bounds.width, rect.y1 / bounds.height]


def text_spans(page):
    return [span for block in page.get_text("dict")["blocks"] if block.get("type") == 0
            for line in block["lines"] for span in line["spans"]]


# URL fragments as they may be split across rendered lines.
URL_PARTS = ("https://", "example", "org/", "docs")


def span_boxes(page, *needles):
    """Bounding boxes of extracted spans whose text contains any needle."""
    return [pymupdf.Rect(span["bbox"]) for span in text_spans(page)
            if any(needle in span["text"] for needle in needles)]


def uri_rects(page, uri=None):
    return [pymupdf.Rect(link["from"]) for link in page.get_links()
            if link.get("kind") == pymupdf.LINK_URI
            and (uri is None or (link.get("uri") or "") == uri)]


def goto_rects(page):
    return [pymupdf.Rect(link["from"]) for link in page.get_links()
            if link.get("kind") in {pymupdf.LINK_GOTO, pymupdf.LINK_NAMED}]


def intersects(box, targets):
    """True when the box overlaps at least one target rectangle."""
    return any(pdf_layout.overlap(box, target) > .5 for target in targets)


def same_rect(first, second, tolerance=.5):
    return all(abs(a - b) <= tolerance for a, b in zip(first, second))


def underline_strokes(page, span_box):
    """Thin horizontal strokes drawn through a span's lower half.

    MuPDF renders an HTML link underline as a zero-height stroke exactly at the
    baseline; the layout CSS must suppress it so translated links do not look
    like new hyperlinks.
    """
    hits = []
    for drawing in page.get_drawings(extended=True):
        rect = drawing.get("rect")
        if drawing.get("type") not in {"s", "f", "fs"} or rect is None:
            continue
        box = pymupdf.Rect(rect)
        if box.height > 2.5 or box.width < span_box.width * .45:
            continue
        if span_box.y0 + span_box.height * .5 <= box.y0 <= span_box.y1 + 2.5:
            hits.append(box)
    return hits


class LinkLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.data_patch = patch.object(service, "DATA", self.directory)
        self.data_patch.start()
        self.export_patch = patch.object(service, "EXPORT_DIR", self.directory / "exports")
        self.export_patch.start()

    def tearDown(self):
        self.export_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    def build(self, raw, name, translations):
        doc_id, doc = service.extract_document(raw, name)
        for block in service.all_blocks(doc):
            block.update(translation=translations.get(block["id"], "测试译文"), target="简体中文")
        directory = self.directory / doc_id
        report = build_layout(directory, doc, "简体中文")
        return directory / "layout" / (report["revision"] + ".pdf"), report

    def reader_entries(self, report, kind):
        return [entry for entry in report.get("links") or [] if entry.get("kind") == kind]

    def assert_reader_entry(self, page, entry):
        """Validate the reader link schema and that it names this page's annotation."""
        values = entry.get("from")
        self.assertIsInstance(values, list)
        self.assertEqual(len(values), 4)
        for value in values:
            self.assertIsInstance(value, (int, float))
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertLessEqual(values[0], values[2])
        self.assertLessEqual(values[1], values[3])
        self.assertIsInstance(entry.get("source_page"), int)
        self.assertEqual(entry["source_page"], page.number + 1)
        kind = entry.get("kind")
        self.assertIn(kind, {"uri", "goto"})
        links = page.get_links()
        if kind == "uri":
            self.assertTrue(entry.get("uri"))
            matches = [link for link in links
                       if link.get("kind") == pymupdf.LINK_URI
                       and (link.get("uri") or "") == entry["uri"]]
        else:
            self.assertIsInstance(entry.get("page"), int)
            self.assertGreaterEqual(entry["page"], 1)
            self.assertGreaterEqual(entry.get("fraction", 1.0), 0.0)
            self.assertLessEqual(entry.get("fraction", 1.0), 1.0)
            matches = [link for link in links
                       if link.get("kind") in {pymupdf.LINK_GOTO, pymupdf.LINK_NAMED}
                       and isinstance(link.get("page"), int) and link["page"] + 1 == entry["page"]]
        for link in matches:
            actual = normalized(page, pymupdf.Rect(link["from"]))
            if all(abs(a - b) <= .02 for a, b in zip(actual, values)):
                return
        self.fail(f"reader link {entry} does not match an annotation on page {page.number + 1}: {links}")

    def test_url_annotation_reanchors_to_translated_url(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            page.insert_textbox(
                pymupdf.Rect(40, 40, 360, 120),
                "Project documentation and worked examples are available at "
                + URL + " for reference.", fontsize=11)
            found = page.search_for(URL)
            self.assertTrue(found)
            source_rect = found[0]
            page.insert_link({"kind": pymupdf.LINK_URI, "from": source_rect, "uri": URL})
            raw = pdf.tobytes()
        path, report = self.build(raw, "url.pdf", {
            "p1-b1": "项目文档与示例可在 " + URL + " 查阅，欢迎参考。"})
        entries = [entry for entry in self.reader_entries(report, "uri")
                   if entry.get("uri") == URL]
        self.assertTrue(entries, report.get("links"))
        self.assertTrue(entries[0].get("group"), entries)
        with pymupdf.open(path) as pdf:
            page = pdf[0]
            for entry in entries:
                self.assert_reader_entry(page, entry)
            translated = span_boxes(page, *URL_PARTS)
            self.assertTrue(translated, page.get_text())
            annotations = uri_rects(page, URL)
            self.assertEqual(len(annotations), len(entries),
                             (annotations, entries))
            for box in annotations:
                self.assertTrue(intersects(box, translated), (box, translated))
                # The annotation must sit on the new glyphs, never merely on the
                # erased source rectangle.
                self.assertFalse(same_rect(box, source_rect), (box, source_rect))

    def test_wrapped_url_yields_multiple_annotation_rects(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            page.insert_textbox(
                pymupdf.Rect(40, 40, 360, 140),
                "Documentation:\n" + URL + "\nand more.", fontsize=11)
            page.insert_link({"kind": pymupdf.LINK_URI,
                              "from": page.search_for(URL)[0], "uri": URL})
            raw = pdf.tobytes()
        path, report = self.build(raw, "wrapped.pdf", {
            "p1-b1": "说明：\n请访问 " + URL + " 获取完整文档与示例。"})
        entries = [entry for entry in self.reader_entries(report, "uri")
                   if entry.get("uri") == URL]
        self.assertTrue(entries, report.get("links"))
        # Fragments of one wrapped link share a group so the reader can
        # highlight every line segment together.
        groups = {entry.get("group") for entry in entries}
        self.assertEqual(len(groups), 1, entries)
        self.assertTrue(all(groups), entries)
        with pymupdf.open(path) as pdf:
            page = pdf[0]
            for entry in entries:
                self.assert_reader_entry(page, entry)
            translated = span_boxes(page, *URL_PARTS)
            self.assertTrue(translated, page.get_text())
            annotations = uri_rects(page, URL)
            self.assertGreaterEqual(len(annotations), 2, page.get_links())
            self.assertEqual(len(annotations), len(entries), (annotations, entries))
            for box in annotations:
                self.assertTrue(intersects(box, translated), (box, translated))

    def test_citation_goto_reanchors_after_punctuation_trim(self):
        with pymupdf.open() as pdf:
            pdf.new_page(width=400, height=300)
            pdf.new_page(width=400, height=300)
            page = pdf[0]
            page.insert_textbox(
                pymupdf.Rect(40, 40, 360, 120),
                "As shown in the earlier study [19], the proposed method works well.",
                fontsize=11)
            found = page.search_for("[19],")
            self.assertTrue(found)
            source_rect = found[0]
            page.insert_link({"kind": pymupdf.LINK_GOTO, "from": source_rect,
                              "page": 1, "to": pymupdf.Point(40, 60)})
            pdf[1].insert_text((40, 80), "Target page content.", fontsize=11)
            raw = pdf.tobytes()
        path, report = self.build(raw, "citation.pdf", {
            "p1-b1": "如先前研究[19]所示，所提出的方法效果良好。",
            "p2-b1": "目标页面内容。"})
        entries = self.reader_entries(report, "goto")
        self.assertEqual(len(entries), 1, report.get("links"))
        entry = entries[0]
        self.assertEqual(entry["page"], 2)
        with pymupdf.open(path) as pdf:
            page = pdf[0]
            self.assert_reader_entry(page, entry)
            self.assertAlmostEqual(entry["fraction"], 60 / 300, places=2)
            translated = page.search_for("[19]") or span_boxes(page, "19")
            self.assertTrue(translated, page.get_text())
            annotations = goto_rects(page)
            self.assertEqual(len(annotations), 1, page.get_links())
            box = annotations[0]
            center = (box.tl + box.br) * .5
            self.assertTrue(any(target.contains(center) or intersects(box, [target])
                                for target in translated), (box, translated))
            # Not merely restored at the erased source position.
            self.assertFalse(source_rect.contains(center), (box, source_rect))
            for link in page.get_links():
                self.assertEqual(link["page"], 1)

    def test_whole_block_linked_title_uses_translated_placement(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            page.insert_text((60, 140), "Understanding Deep Networks",
                             fontsize=18, fontname="hebo")
            source_rect = page.search_for("Understanding Deep Networks")[0]
            page.insert_link({"kind": pymupdf.LINK_URI, "from": source_rect,
                              "uri": "https://example.org/paper"})
            raw = pdf.tobytes()
        path, report = self.build(raw, "title.pdf", {"p1-b1": "深度网络理解"})
        uri = "https://example.org/paper"
        entries = [entry for entry in self.reader_entries(report, "uri")
                   if entry.get("uri") == uri]
        self.assertTrue(entries, report.get("links"))
        self.assertFalse([issue for issue in report.get("issues") or []
                          if issue.get("kind") == "link"], report.get("issues"))
        placement = report["placements"]["p1-b1"]
        with pymupdf.open(path) as pdf:
            page = pdf[0]
            for entry in entries:
                self.assert_reader_entry(page, entry)
            annotations = uri_rects(page, uri)
            self.assertTrue(annotations, page.get_links())
            translated = span_boxes(page, "深度网络")
            self.assertTrue(translated, page.get_text())
            placement_rect = pymupdf.Rect(placement[0] * page.rect.width,
                                          placement[1] * page.rect.height,
                                          placement[2] * page.rect.width,
                                          placement[3] * page.rect.height)
            for box in annotations:
                self.assertGreaterEqual(pdf_layout.overlap(box, placement_rect),
                                        box.get_area() * .9, (box, placement_rect))
                self.assertTrue(intersects(box, translated), (box, translated))

    def test_dropped_external_link_is_reported(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            page.insert_textbox(pymupdf.Rect(40, 40, 360, 120),
                                "The related work by Smith et al. provides useful context.",
                                fontsize=11)
            page.insert_link({"kind": pymupdf.LINK_URI,
                              "from": page.search_for("Smith et al.")[0],
                              "uri": "https://example.org/smith"})
            raw = pdf.tobytes()
        path, report = self.build(raw, "dropped.pdf", {
            "p1-b1": "相关研究为这项工作提供了有用的背景。"})
        uri = "https://example.org/smith"
        self.assertFalse([entry for entry in report.get("links") or []
                          if entry.get("uri") == uri], report.get("links"))
        issues = [issue for issue in report.get("issues") or []
                  if issue.get("kind") == "link"]
        self.assertTrue(issues, report.get("issues"))
        self.assertEqual(issues[0]["page"], 1)
        self.assertIn("链接", issues[0]["message"])
        with pymupdf.open(path) as pdf:
            page = pdf[0]
            self.assertEqual(uri_rects(page, uri), [])
            self.assertNotIn("Smith", page.get_text())

    def test_preserved_image_annotation_keeps_original_rect(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 32), False)
            image.set_rect(image.irect, (60, 120, 190))
            page.insert_image(pymupdf.Rect(150, 120, 250, 220),
                              stream=image.tobytes("png"))
            page.insert_text((40, 60), "Figure caption below.", fontsize=11)
            page.insert_link({"kind": pymupdf.LINK_URI,
                              "from": pymupdf.Rect(150, 120, 250, 220),
                              "uri": "https://example.org/image"})
            raw = pdf.tobytes()
        path, report = self.build(raw, "image.pdf", {"p1-b1": "下方图注。"})
        uri = "https://example.org/image"
        entries = [entry for entry in self.reader_entries(report, "uri")
                   if entry.get("uri") == uri]
        self.assertEqual(len(entries), 1, report.get("links"))
        self.assertFalse([entry for entry in report.get("placed_links") or []
                          if entry.get("uri") == uri], report.get("placed_links"))
        with pymupdf.open(path) as pdf:
            page = pdf[0]
            self.assert_reader_entry(page, entries[0])
            annotations = uri_rects(page, uri)
            self.assertEqual(len(annotations), 1, page.get_links())
            self.assertTrue(same_rect(annotations[0], pymupdf.Rect(150, 120, 250, 220)),
                            annotations[0])

    def test_rotated_page_link_follows_translated_url(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            page.insert_textbox(
                pymupdf.Rect(40, 40, 360, 140),
                "See the project page\n" + URL + "\nfor documentation.", fontsize=11)
            page.insert_link({"kind": pymupdf.LINK_URI,
                              "from": page.search_for(URL)[0], "uri": URL})
            page.set_rotation(90)
            raw = pdf.tobytes()
        path, report = self.build(raw, "rotated.pdf", {
            "p1-b1": "请访问项目页面\n" + URL + "\n查看文档。"})
        entries = [entry for entry in self.reader_entries(report, "uri")
                   if entry.get("uri") == URL]
        self.assertTrue(entries, report.get("links"))
        with pymupdf.open(path) as pdf:
            page = pdf[0]
            self.assertEqual(page.rotation, 90)
            for entry in entries:
                self.assert_reader_entry(page, entry)
            translated = span_boxes(page, *URL_PARTS)
            self.assertTrue(translated, page.get_text())
            annotations = uri_rects(page, URL)
            self.assertTrue(annotations, page.get_links())
            for box in annotations:
                self.assertTrue(all(0 <= value <= 1 for value in normalized(page, box)),
                                normalized(page, box))
                # Text extraction uses unrotated space; get_links uses displayed.
                raw_box = box * page.derotation_matrix
                self.assertTrue(intersects(raw_box, translated), (raw_box, translated))

    def test_link_text_keeps_source_color_and_no_underline(self):
        blue = "https://example.org/docs"
        prefix = "The official documentation is at "
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            page.insert_text((40, 100), prefix, fontsize=11, fontname="helv")
            x = 40 + pymupdf.get_text_length(prefix, fontname="helv", fontsize=11)
            page.insert_text((x, 100), blue, fontsize=11, fontname="helv", color=(0, 0, 1))
            link_rect = page.search_for(blue)[0]
            source_color = next(span["color"] for span in text_spans(
                page) if blue in span["text"])
            self.assertEqual(source_color, 0xff)
            page.insert_link({"kind": pymupdf.LINK_URI, "from": link_rect, "uri": blue})
            raw = pdf.tobytes()
        path, report = self.build(raw, "blue.pdf", {
            "p1-b1": "官方文档位于 " + blue + " 供查阅。"})
        with pymupdf.open(path) as pdf:
            page = pdf[0]
            spans = [span for span in text_spans(page)
                     if any(part in span["text"] for part in URL_PARTS)]
            self.assertTrue(spans, page.get_text())
            for span in spans:
                self.assertEqual(span["color"], source_color, (span["text"], span["color"]))
                self.assertEqual(underline_strokes(page, pymupdf.Rect(span["bbox"])), [],
                                 span["text"])
            self.assertTrue(uri_rects(page, blue), page.get_links())

        # Black source text stays black: the HTML default link styling must not
        # sneak a blue, underlined anchor into the translated page.
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=400, height=300)
            page.insert_textbox(
                pymupdf.Rect(40, 40, 360, 140),
                "The official documentation is at\n" + blue + "\nfor reference.",
                fontsize=11)
            page.insert_link({"kind": pymupdf.LINK_URI,
                              "from": page.search_for(blue)[0], "uri": blue})
            raw = pdf.tobytes()
        path, report = self.build(raw, "black.pdf", {
            "p1-b1": "官方文档位于\n" + blue + "\n供查阅。"})
        with pymupdf.open(path) as pdf:
            page = pdf[0]
            spans = [span for span in text_spans(page)
                     if any(part in span["text"] for part in URL_PARTS)]
            self.assertTrue(spans, page.get_text())
            for span in spans:
                self.assertEqual(span["color"], 0, (span["text"], span["color"]))
                self.assertEqual(underline_strokes(page, pymupdf.Rect(span["bbox"])), [],
                                 span["text"])
            self.assertTrue(uri_rects(page, blue), page.get_links())


if __name__ == "__main__":
    unittest.main()