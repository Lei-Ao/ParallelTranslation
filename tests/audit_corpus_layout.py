"""Audit existing local papers without changing translations or calling an API.

Run: python tests/audit_corpus_layout.py
Results and rendered source/translation samples go to tmp/source-layout-audit/.
"""
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymupdf
import pdf_layout
from pdf_typography import font_source


def han(text):
    return Counter(c for c in unicodedata.normalize("NFKC", text) if "\u3400" <= c <= "\u9fff")


def audit():
    output = ROOT / "tmp" / "source-layout-audit"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for metadata in sorted((ROOT / ".data").glob("*/document.json")):
        document = json.loads(metadata.read_text(encoding="utf-8"))
        translated = [b for p in document["pages"] for b in p["blocks"] if b.get("translation")]
        if not translated:
            continue
        target = Counter(b.get("target") for b in translated).most_common(1)[0][0]
        report = pdf_layout.build_layout(metadata.parent, document, target)
        result = {"document": document["name"], "id": document["id"], "pages": report["pages"],
                  "replaced": report["replaced"], "preserved": report["preserved"],
                  "untranslated": report["untranslated"], "scaled": report["scaled"],
                  "revision": report["revision"], "missing_chinese": [], "collisions": [],
                  "spacing_errors": [], "font_changes": [], "bold_body": [], "symbol_errors": [],
                  "adjusted_regions": len(report.get("flow", [])), "samples": []}
        sample_pages = {0, 1, 2, len(document["pages"]) // 2, len(document["pages"]) - 1}
        pdf_path = metadata.parent / "layout" / (report["revision"] + ".pdf")
        with pymupdf.open(pdf_path) as pdf, pymupdf.open(metadata.parent / "source.pdf") as original:
            for index, saved in enumerate(document["pages"]):
                page = pdf[index]
                entries = [(b, pymupdf.Rect(report["placements"][b["id"]])) for b in saved["blocks"]
                           if b["id"] in report["placements"]]
                source_groups = {}
                for block, box in entries:
                    source_groups.setdefault(tuple(box), []).append(pymupdf.Rect(block["bbox"]))
                original_ids = set(report.get("original_placements", []))
                expected = han("".join(b["text"] if b["id"] in original_ids else b.get("translation", "")
                                       for b, _ in entries if b.get("target") == target))
                erased = {tuple(box) for item in report.get("erased_spans", [])
                          if item["page"] == saved["number"] for box in item["boxes"]}
                native = original[index].get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
                fixed_spans = [span for item in native if item["type"] == 0 for line in item["lines"]
                               for span in line["spans"] if span["text"].strip() and tuple(span["bbox"]) not in erased]
                output_spans = [span for item in page.get_text("dict")["blocks"] if item["type"] == 0
                                for line in item["lines"] for span in line["spans"] if han(span["text"])]
                missing = expected - han(page.get_text())
                if missing:
                    result["missing_chinese"].append({"page": index + 1, "count": sum(missing.values())})
                for i, (block, box) in enumerate(entries):
                    if font_source() is not None and block["id"] not in original_ids:
                        raw_box = pdf_layout.source_rect(page, {"bbox": list(box)})
                        styled = [span for span in output_spans if raw_box.contains(pymupdf.Rect(span["bbox"]).tl
                                  + (pymupdf.Rect(span["bbox"]).br - pymupdf.Rect(span["bbox"]).tl) * .5)]
                        count = sum(sum(han(span["text"]).values()) for span in styled)
                        fallback_count = sum(sum(han(span["text"]).values()) for span in styled if "ParallelBook" not in span["font"])
                        if count >= 8 and fallback_count > count * .5:
                            result["font_changes"].append({"page": index + 1, "block": block["id"]})
                        bold_count = sum(sum(han(span["text"]).values()) for span in styled if span["flags"] & 16)
                        if count >= 20 and bold_count > count * .8 and pdf_layout.is_prose(block["text"]):
                            source_style, _ = pdf_layout.inspect_block([b for b in native if b["type"] == 0],
                                                                       pdf_layout.source_rect(original[index], block))
                            if source_style and not source_style["bold"]:
                                result["bold_body"].append({"page": index + 1, "block": block["id"]})
                    for other, other_box in entries[i + 1:]:
                        if box == other_box:
                            continue  # One merged paragraph or cover group owns both IDs.
                        area = pdf_layout.overlap(box, other_box) * page.rect.width * page.rect.height
                        source_overlap = max(pdf_layout.overlap(a, b) for a in source_groups[tuple(box)]
                                             for b in source_groups[tuple(other_box)])
                        if area > 1 and source_overlap < 1e-6:
                            result["collisions"].append({"page": index + 1, "blocks": [block["id"], other["id"]], "area": round(area, 2)})
                    for fixed in fixed_spans:
                        displayed = pymupdf.Rect(fixed["bbox"]) * original[index].rotation_matrix
                        bounds = original[index].rect
                        fixed_box = pymupdf.Rect(displayed.x0 / bounds.width, displayed.y0 / bounds.height,
                                                displayed.x1 / bounds.width, displayed.y1 / bounds.height)
                        area = pdf_layout.overlap(box, fixed_box) * page.rect.width * page.rect.height
                        if area > 1 and not any(pdf_layout.overlap(source, fixed_box) >= 1e-6
                                                for source in source_groups[tuple(box)]):
                            result["collisions"].append({"page": index + 1, "block": block["id"], "kind": "fixed_text", "area": round(area, 2)})
                for adjustment in report.get("flow", []):
                    if adjustment["page"] != saved["number"]:
                        continue
                    key = adjustment["blocks"][0]
                    actual = pdf_layout.source_rect(page, {"bbox": report["placements"][key]})
                    gap = actual.y0 - adjustment["previous_bottom"]
                    ink_gap = adjustment.get("source_ink_gap")
                    too_tight = (gap + adjustment["ink_padding"] < ink_gap - .1
                                 if ink_gap is not None else gap < adjustment["source_gap"] - .1)
                    if abs(gap - adjustment["gap"]) > .1 or too_tight:
                        result["spacing_errors"].append({"page": index + 1, "block": key, "gap": gap, "expected": adjustment["gap"]})
                for item in report.get("inline_math", []):
                    if item["page"] != saved["number"]:
                        continue
                    source_counts = Counter(key for key in item["source"] if "ˆ" in key or "\u0307" in key)
                    translated_text = pdf_layout.normalized_inline_math("".join(
                        b.get("translation", "") for b in saved["blocks"] if b["id"] in item["blocks"]))
                    for key, count in source_counts.items():
                        expected_count = min(count, translated_text.count(key))
                        actual_count = sum(label.count(key) for label in item["matched"])
                        if actual_count < expected_count:
                            result["symbol_errors"].append({"page": index + 1, "blocks": item["blocks"], "symbol": key})
                if index in sample_pages:
                    prefix = f"{document['id'][:8]}-p{index + 1}"
                    for label, rendered in (("source", original[index]), ("translation", page)):
                        rendered.get_pixmap(matrix=pymupdf.Matrix(1.3, 1.3)).save(output / f"{prefix}-{label}.png")
                    result["samples"].append(prefix)
        results.append(result)
        print(json.dumps({key: value for key, value in result.items() if key != "samples"}, ensure_ascii=True), flush=True)
    summary = {"engine_version": pdf_layout.ENGINE_VERSION, "documents": len(results),
               "pages": sum(r["pages"] for r in results), "results": results}
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return any(r[field] for r in results for field in
               ("missing_chinese", "collisions", "spacing_errors", "font_changes", "bold_body", "symbol_errors"))


if __name__ == "__main__":
    raise SystemExit(audit())
