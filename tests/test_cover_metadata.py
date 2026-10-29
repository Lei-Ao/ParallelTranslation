import tempfile
import unittest
import unicodedata
from collections import Counter
from pathlib import Path

import pymupdf

import pdf_layout


class CoverMetadataTests(unittest.TestCase):
    def fixture(self, rotation=0, split=False, graphic=False, punctuated=True, comma_markers=False):
        pdf = pymupdf.open()
        page = pdf.new_page(width=612, height=792)
        page.insert_text((100, 80), "A General Research Paper About Robots", fontsize=18, fontname="hebo")
        names = ["John Doe1, Jane Smith2, Alan Researcher1,", "Alice Example2 and Robert Scientist1"]
        if comma_markers:
            names = ["John Doe,1, Jane Smith,2, Alan Researcher,1,", "Alice Example,2 and Robert Scientist,1"]
        if not punctuated:
            names = [text.replace(",", "").replace(" and ", " ") for text in names]
        for index, text in enumerate(names):
            page.insert_text((160 if index == 0 else 180, 130 + index * 12), text, fontsize=10, fontname="hebo")
        if split:
            page.insert_text((200, 153), "1 Example University, 2 Research Institute", fontsize=9)
            page.insert_text((205, 173), "{john, jane}@example.edu", fontsize=9, fontname="cour")
        else:
            page.insert_text((200, 153), "1 Example University, 2 Research Institute\n{john, jane}@example.edu",
                             fontsize=9, fontname="cour", lineheight=1.3)
        page.insert_text((215, 188), "{alice, robert}@lab.org", fontsize=9, fontname="cour")
        if graphic:
            page.draw_rect((80, 195, 530, 230), fill=(.8, .8, .8))
        page.insert_textbox((110, 240, 500, 340), "Abstract: The method is useful for the control of a robot. " * 4, fontsize=10)
        page.set_rotation(rotation)
        blocks = []
        for index, native in enumerate(page.get_text("dict")["blocks"]):
            if native["type"] != 0:
                continue
            text = "\n".join("".join(s["text"] for s in line["spans"]) for line in native["lines"])
            rect = pymupdf.Rect(native["bbox"]) * page.rotation_matrix
            translation = text
            if text.startswith("A General"):
                translation = "机器人通用研究论文"
            elif text.startswith("Abstract"):
                translation = "摘要：这里的正文用于检查作者信息不能挤入摘要区域。" * 4
            elif "Example University" in text:
                translation = text.replace("Example University", "示例大学").replace("Research Institute", "研究所")
            blocks.append({"id": f"arbitrary-{index}", "text": text, "translation": translation, "target": "简体中文",
                           "bbox": [rect.x0 / page.rect.width, rect.y0 / page.rect.height,
                                    rect.x1 / page.rect.width, rect.y1 / page.rect.height]})
        return pdf, {"pages": [{"number": 1, "blocks": blocks}]}

    def render(self, rotation=0, split=False, edit=None, graphic=False, punctuated=True, comma_markers=False):
        with tempfile.TemporaryDirectory() as temp:
            pdf, document = self.fixture(rotation, split, graphic, punctuated, comma_markers)
            if edit:
                edit(document)
            path = Path(temp) / "source.pdf"
            pdf.save(path)
            pdf.close()
            raw, report = pdf_layout.typeset(path, document, "简体中文")
        output = pymupdf.open(stream=raw, filetype="pdf")
        self.addCleanup(output.close)
        return output, report, document

    def test_metadata_reflows_together_without_losing_names_units_or_email(self):
        for split in (False, True):
            with self.subTest(split=split):
                output, report, document = self.render(split=split)
                normalize = lambda text: Counter(c for c in unicodedata.normalize("NFKC", text) if not c.isspace())
                expected = normalize("".join(b["translation"] for b in document["pages"][0]["blocks"]))
                self.assertFalse(expected - normalize(output[0].get_text()))
                self.assertFalse(report["issues"], report)
                source_authors = next(b for b in document["pages"][0]["blocks"] if "Robert Scientist" in b["text"])
                source_units = next(b for b in document["pages"][0]["blocks"] if "Example University" in b["text"])
                self.assertGreater(pdf_layout.overlap(pymupdf.Rect(source_authors["bbox"]),
                                                     pymupdf.Rect(source_units["bbox"])), 0)
                body = next(b for b in document["pages"][0]["blocks"] if b["text"].startswith("Abstract"))
                author = next(b for b in document["pages"][0]["blocks"] if b["text"].startswith("John"))
                self.assertLess(report["placements"][author["id"]][3], report["placements"][body["id"]][1])
                # Check actual text lines, including markers, rather than
                # exempting overlaps inherited from the source PDF's boxes.
                lines = [line for block in output[0].get_text("dict")["blocks"] if block["type"] == 0
                         for line in block["lines"] if any("@" in span["text"] for span in line["spans"])]
                self.assertEqual(len(lines), 2)
                names = [span for block in output[0].get_text("dict")["blocks"] if block["type"] == 0
                         for line in block["lines"] for span in line["spans"] if "Robert" in span["text"]]
                self.assertTrue(names)
                self.assertGreater(min(line["bbox"][1] for line in lines), max(span["bbox"][3] for span in names))
                markers = [span for block in output[0].get_text("dict")["blocks"] if block["type"] == 0
                           for line in block["lines"] for span in line["spans"]
                           if span["text"].strip() in {"1", "2"} and span["size"] < 8]
                self.assertGreaterEqual(len(markers), 3)

    def test_affiliation_markers_after_commas_are_superscripted(self):
        output, report, document = self.render(comma_markers=True)
        self.assertFalse(report["issues"], report)
        spans = [span for block in output[0].get_text("dict")["blocks"] if block["type"] == 0
                 for line in block["lines"] for span in line["spans"] if span["text"].strip()]
        markers = [span for span in spans if span["text"].strip() in {"1", "2"} and span["size"] < 8]
        self.assertGreaterEqual(len(markers), 4, markers)
        # Affiliation digits must not stay on the baseline next to the surname.
        names = [span for span in spans
                 if any(name in span["text"] for name in
                        ("Doe", "Smith", "Researcher", "Example", "Scientist"))]
        self.assertFalse([span for span in names if any(char.isdigit() for char in span["text"])], names)

    def test_cover_group_retains_rotation_and_all_metadata(self):
        for rotation in (90, 180, 270):
            with self.subTest(rotation=rotation):
                output, report, document = self.render(rotation)
                self.assertEqual(output[0].rotation, rotation)
                self.assertIn("{alice, robert}@lab.org", output[0].get_text())
                self.assertFalse(report["issues"], report)

    def test_partial_translation_preserves_the_entire_source_group(self):
        def edit(document):
            block = next(b for b in document["pages"][0]["blocks"] if "@lab.org" in b["text"])
            block["translation"] = ""
        output, report, _ = self.render(edit=edit)
        self.assertIn("Example University", output[0].get_text())
        self.assertTrue(any(issue["kind"] == "untranslated" for issue in report["issues"]))

    def test_excessive_metadata_falls_back_as_one_group(self):
        def edit(document):
            block = next(b for b in document["pages"][0]["blocks"] if b["text"].startswith("John"))
            block["translation"] = "Long Author Name1, " * 100
        output, report, _ = self.render(edit=edit)
        self.assertIn("John Doe1", output[0].get_text())
        self.assertTrue(any("作者" in issue["message"] for issue in report["issues"]))

    def test_graphic_below_metadata_constrains_available_height(self):
        output, report, document = self.render(graphic=True)
        self.assertFalse(report["issues"], report)
        author = next(b for b in document["pages"][0]["blocks"] if b["text"].startswith("John"))
        self.assertLess(report["placements"][author["id"]][3] * 792, 195)
        self.assertTrue(any(abs(d["rect"].y0 - 195) < .1 for d in output[0].get_drawings()))

    def test_author_lists_without_commas_or_and_are_recognized(self):
        output, report, document = self.render(punctuated=False)
        self.assertFalse(report["issues"], report)
        author = next(b for b in document["pages"][0]["blocks"] if b["text"].startswith("John"))
        contact = next(b for b in document["pages"][0]["blocks"] if "@lab.org" in b["text"])
        self.assertEqual(report["placements"][author["id"]], report["placements"][contact["id"]])


if __name__ == "__main__":
    unittest.main()
