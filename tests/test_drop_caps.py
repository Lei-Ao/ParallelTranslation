"""Decorative initials belong to a word, independently of extraction grouping."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pymupdf
from fastapi.testclient import TestClient

import app as service
import pdf_layout
from pdf_text import _drop_caps, normalize_drop_caps


def fixture(*, grouping="heading", rotation=0, width=600, height=750, scale=1,
            column=0, initial="D", rest="YNAMIC", translated=True, graphic=False,
            cap_size=28, cropped=False, gap=2):
    pdf = pymupdf.open()
    page = pdf.new_page(width=width, height=height)
    if cropped:
        page.set_cropbox(pymupdf.Rect(8, 10, width - 8, height - 10))
    left = (30 + column * width / 2) * scale
    page.insert_text((left + 30 * scale, 50 * scale), "2. OVERVIEW", fontsize=10 * scale)
    page.insert_text((left, 105 * scale), initial, fontname="tibo", fontsize=cap_size * scale)
    cap = next(b for b in page.get_text("dict")["blocks"] if any(
        s["text"] == initial for line in b.get("lines", []) for s in line["spans"]))
    cap_box = next(pymupdf.Rect(s["bbox"]) for line in cap["lines"] for s in line["spans"] if s["text"] == initial)
    right = cap_box.x1 + gap * scale
    text = [rest + " behavior is studied using a general method.",
            "The system responds to changes during operation.",
            "The experiment records measurements and outcomes."]
    for x, y, line in ((right, 87 * scale, text[0]), (right, 99 * scale, text[1]),
                       (left, 123 * scale, text[2])):
        page.insert_text((x, y), line, fontname="tiro", fontsize=10 * scale)
    if graphic:
        page.draw_rect((left - 3 * scale, 65 * scale, right + 220 * scale, 135 * scale), color=(0, 0, 0))
    native = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
    heading = next(pymupdf.Rect(line["bbox"]) for b in native for line in b["lines"]
                   if "".join(s["text"] for s in line["spans"]) == "2. OVERVIEW")
    body_lines = [pymupdf.Rect(line["bbox"]) for b in native for line in b["lines"]
                  if "".join(s["text"] for s in line["spans"]) in text]
    region = pymupdf.Rect(body_lines[0])
    for rect in body_lines[1:]:
        region |= rect
    page.set_rotation(rotation)

    def block(identifier, source, translation, rect):
        box = rect * page.rotation_matrix
        return {"id": identifier, "text": source, "translation": translation if translated else "",
                "target": "简体中文" if translated else "", "edited": False,
                "bbox": [box.x0 / page.rect.width, box.y0 / page.rect.height,
                         box.x1 / page.rect.width, box.y1 / page.rect.height]}

    blocks = [block("heading-stable", "2. OVERVIEW", "2. 概述", heading),
              block("body-stable", "\n".join(text), "动态行为通过通用方法研究。系统会对运行中的变化作出响应。实验记录测量结果。", region)]
    if grouping == "heading":
        blocks[0] = block("heading-stable", "2. OVERVIEW\n" + initial, "2. 概述\n" + initial, heading | cap_box)
    elif grouping == "body":
        blocks[1] = block("body-stable", initial + "\n" + "\n".join(text),
                          initial + "\n" + blocks[1]["translation"], region | cap_box)
    else:
        blocks.insert(1, block("initial-stable", initial, initial, cap_box))
    return pdf, {"id": "a" * 32, "name": "arbitrary.pdf", "pages": [{"number": 1, "blocks": blocks}],
                 "total_blocks": len(blocks), "reference_scan": 1}


class DropCapTests(unittest.TestCase):
    def document(self, **options):
        pdf, document = fixture(**options)
        self.addCleanup(pdf.close)
        return pdf, document

    def test_all_extraction_groupings_restore_word_without_changing_body_id(self):
        for grouping in ("heading", "body", "standalone"):
            for initial, rest in (("D", "YNAMIC"), ("T", "HE"), ("I", "N"), ("É", "TUDIER")):
                with self.subTest(grouping=grouping, initial=initial):
                    pdf, document = self.document(grouping=grouping, initial=initial, rest=rest)
                    original = copy.deepcopy(document)
                    self.assertTrue(normalize_drop_caps(document, pdf))
                    body = next(b for b in document["pages"][0]["blocks"] if b["id"] == "body-stable")
                    self.assertTrue(body["text"].startswith(initial + rest + " "), body)
                    self.assertEqual(body["translation"], original["pages"][0]["blocks"][-1]["translation"].removeprefix(initial + "\n"))
                    self.assertEqual(document["pages"][0]["blocks"][0]["translation"], "2. 概述")
                    self.assertTrue(body["drop_cap_review"])
                    self.assertTrue(document["drop_cap_legacy"])
                    normalized = copy.deepcopy(document)
                    self.assertFalse(normalize_drop_caps(document, pdf))
                    self.assertEqual(document, normalized)
                    if grouping == "standalone":
                        self.assertEqual(service.find_block(document, "initial-stable")[2]["id"], "body-stable")

    def test_word_sized_gap_keeps_initial_as_an_independent_word(self):
        pdf, document = self.document(initial="A", rest="GENERAL", gap=4)
        normalize_drop_caps(document, pdf)
        body = document["pages"][0]["blocks"][-1]
        self.assertTrue(body["text"].startswith("A GENERAL "), body)
        self.assertEqual(body["drop_cap"]["separator"], " ")

    def test_geometry_scales_with_font_columns_crop_page_size_and_rotation(self):
        for width, height, scale, column in ((600, 750, 1, 0), (900, 700, 1.4, 0),
                                            (600, 750, .65, 1), (700, 850, 1, 1)):
            for rotation in (0, 90, 180, 270):
                with self.subTest(width=width, scale=scale, column=column, rotation=rotation):
                    pdf, document = self.document(width=width, height=height, scale=scale,
                                                  column=column, rotation=rotation, cropped=True)
                    normalize_drop_caps(document, pdf)
                    self.assertTrue(document["pages"][0]["blocks"][-1]["text"].startswith("DYNAMIC "))

    def test_labels_same_size_initials_and_graphic_regions_do_not_join(self):
        for options in ({"initial": "A."}, {"cap_size": 10}, {"graphic": True}):
            with self.subTest(options=options):
                pdf, document = self.document(**options)
                expected = copy.deepcopy(document["pages"])
                normalize_drop_caps(document, pdf)
                self.assertEqual(document["pages"], expected)
                self.assertNotIn("drop_cap_legacy", document)
        pdf, _ = self.document()
        native = pdf[0].get_text("dict")["blocks"]
        for block in native:
            if block["type"] == 0:
                for line in block["lines"]:
                    for span in line["spans"]:
                        if span["text"] == "D":
                            span["font"] = "STIXTwoMath"
        self.assertEqual(_drop_caps(pdf[0], native), [])

    def test_manual_translation_is_retained_until_review_and_then_can_normalize(self):
        pdf, standalone = self.document(grouping="standalone")
        standalone["pages"][0]["blocks"][1].update(edited=True, translation="已手工记录的首字注释")
        normalize_drop_caps(standalone, pdf)
        self.assertEqual(standalone["pages"][0]["blocks"][1]["translation"], "已手工记录的首字注释")
        self.assertTrue(standalone["pages"][0]["blocks"][1]["drop_cap_preserve"])
        pdf, document = self.document()
        document["pages"][0]["blocks"][0]["edited"] = True
        expected = copy.deepcopy(document["pages"])
        normalize_drop_caps(document, pdf)
        self.assertEqual(document["pages"][0]["blocks"][0]["translation"], expected[0]["blocks"][0]["translation"])
        self.assertTrue(all(b.get("drop_cap_preserve") for b in document["pages"][0]["blocks"]))
        document["pages"][0]["blocks"][0]["translation"] = "2. 已核对概述"
        normalize_drop_caps(document, pdf)
        self.assertEqual(document["pages"][0]["blocks"][0]["translation"], "2. 已核对概述")
        self.assertTrue(document["pages"][0]["blocks"][1]["text"].startswith("DYNAMIC "))
        self.assertFalse(any(b.get("drop_cap_preserve") for b in document["pages"][0]["blocks"]))

    def test_typesetting_erases_initial_and_preserves_complete_translation_and_heading(self):
        for grouping in ("heading", "body", "standalone"):
            for rotation in (0, 90):
                with self.subTest(grouping=grouping, rotation=rotation), tempfile.TemporaryDirectory() as temp:
                    pdf, document = self.document(grouping=grouping, rotation=rotation)
                    original = copy.deepcopy(document)
                    source = Path(temp) / "source.pdf"
                    pdf.save(source)
                    raw, report = pdf_layout.typeset(source, document, "简体中文")
                    self.assertEqual(document, original)
                    self.assertEqual(report["preserved"], 0, report)
                    self.assertTrue(any(i["kind"] == "review" for i in report["issues"]))
                    with pymupdf.open(stream=raw, filetype="pdf") as result:
                        text = result[0].get_text()
                        self.assertNotIn("D", text)
                        self.assertIn("动态行为", text)
                        self.assertIn("概述", text)

    def test_fresh_extraction_and_existing_data_supply_complete_word_to_agent(self):
        for grouping in ("heading", "standalone"):
            with self.subTest(grouping=grouping), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pdf, document = self.document(grouping=grouping, translated=False)
                with patch.object(service, "DATA", root):
                    doc_id, extracted = service.extract_document(pdf.tobytes(), "unrelated-name.pdf")
                    self.assertTrue(any(b["text"].startswith("DYNAMIC ") for b in service.all_blocks(extracted)), extracted)
                    folder = root / document["id"]
                    folder.mkdir()
                    pdf.save(folder / "source.pdf")
                    service.save_doc(folder, document)
                    with TestClient(service.app) as client:
                        client.headers["X-Local-Token"] = client.get("/api/session").json()["token"]
                        loaded = client.get(f"/api/documents/{document['id']}").json()
                        body = next(b for b in service.all_blocks(loaded) if b["id"] == "body-stable")
                        self.assertTrue(body["text"].startswith("DYNAMIC "))
                        config = {"agent": {"base_url": "https://example.com/v1", "model": "mock"}, "target": "简体中文"}
                        with patch.object(service, "invoke_agent", new=AsyncMock(return_value="动态行为。")) as invoke:
                            response = client.post(f"/api/documents/{document['id']}/blocks/body-stable/translate", json=config)
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertTrue(invoke.call_args.args[2].startswith("DYNAMIC "))
                            self.assertNotIn("drop_cap_review", response.json())
                        saved = json.loads((folder / "document.json").read_text(encoding="utf-8"))
                        self.assertTrue(saved["drop_cap_legacy"])

    def test_existing_translation_review_clears_after_edit_or_batch_retranslation(self):
        for operation in ("edit", "batch"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                pdf, document = self.document()
                folder = root / document["id"]
                folder.mkdir()
                pdf.save(folder / "source.pdf")
                service.save_doc(folder, document)
                with patch.object(service, "DATA", root), TestClient(service.app) as client:
                    client.headers["X-Local-Token"] = client.get("/api/session").json()["token"]
                    loaded = client.get(f"/api/documents/{document['id']}").json()
                    self.assertTrue(loaded["pages"][0]["blocks"][-1]["drop_cap_review"])
                    if operation == "edit":
                        response = client.patch(f"/api/documents/{document['id']}/blocks/body-stable",
                                                json={"translation": "动态行为。", "target": "简体中文"})
                        self.assertEqual(response.status_code, 200, response.text)
                    else:
                        result = {"blocks": [{"id": "body-stable", "translation": "动态行为。"}]}
                        with patch.object(service, "invoke_agent", new=AsyncMock(return_value=json.dumps(result))) as invoke:
                            response = client.post(f"/api/documents/{document['id']}/blocks/translate-batch",
                                                   json={"agent": {"model": "mock"}, "block_ids": ["body-stable"]})
                            self.assertEqual(response.status_code, 200, response.text)
                            payload = json.loads(invoke.call_args.args[1].split("参考资料与本次待翻译段落：\n")[1])
                            self.assertTrue(payload["current_blocks"][0]["source"].startswith("DYNAMIC "))
                    saved = service.load_doc(document["id"])
                    self.assertNotIn("drop_cap_review", saved["pages"][0]["blocks"][-1])


if __name__ == "__main__":
    unittest.main()
