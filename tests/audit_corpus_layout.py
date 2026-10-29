"""Audit existing local papers without changing translations or calling an API.

Run: python tests/audit_corpus_layout.py
Results and rendered source/translation samples go to tmp/source-layout-audit/.
Link checks report, per page, reader-report schema violations, dropped source
URI/goto annotations and reader entries whose rectangle is not on their page.
"""
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymupdf
import pdf_layout
from pdf_typography import font_source


def han(text):
    return Counter(c for c in unicodedata.normalize("NFKC", text) if "\u3400" <= c <= "\u9fff")


def normalized_uri(value):
    """Comparable form of a link URI.

    Older PDFs carry the same DOI link with different spellings
    ("https://doi.org/..." vs "http://dx.doi.org/..."); comparing raw strings
    reports surviving links as dropped. The scheme and host spellings are
    normalized, the path keeps its case, and a trailing slash is ignored.
    """
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "dx.doi.org":
        host = "doi.org"
    path = parts.path
    if path.endswith("/"):
        path = path[:-1]
    target = host + path
    if parts.query:
        target += "?" + parts.query
    if parts.fragment:
        target += "#" + parts.fragment
    return target or value


def link_box(page, link):
    """Displayed normalized rectangle of an output page annotation."""
    try:
        box = pymupdf.Rect(link.get("from"))
    except (TypeError, ValueError):
        return None
    bounds = page.rect
    if box.is_empty or not box.is_valid or not bounds.width or not bounds.height:
        return None
    return [box.x0 / bounds.width, box.y0 / bounds.height,
            box.x1 / bounds.width, box.y1 / bounds.height]


def matches_reader_entry(page, link, entry):
    box = link_box(page, link)
    values = entry.get("from") or []
    return (box is not None and len(values) == 4
            and all(abs(actual - value) <= .02 for actual, value in zip(box, values)))


def check_reader_links(reader_links, page_count, errors):
    """Validate the reader link schema shared with static/app.js."""
    for entry in reader_links or []:
        if not isinstance(entry, dict):
            errors.append({"page": 0, "kind": "reader_entry", "entry": entry})
            continue
        source_page = entry.get("source_page")
        if (not isinstance(source_page, int) or isinstance(source_page, bool)
                or not 1 <= source_page <= page_count):
            errors.append({"page": source_page if isinstance(source_page, int) else 0,
                           "kind": "reader_source_page", "entry": entry})
            continue
        box = entry.get("from")
        if (not isinstance(box, list) or len(box) != 4
                or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                           and 0 <= value <= 1 for value in box)
                or box[0] > box[2] or box[1] > box[3]):
            errors.append({"page": source_page, "kind": "reader_from", "entry": entry})
            continue
        kind = entry.get("kind")
        if kind == "uri":
            if not isinstance(entry.get("uri"), str) or not entry.get("uri"):
                errors.append({"page": source_page, "kind": "reader_uri", "entry": entry})
        elif kind == "goto":
            target = entry.get("page")
            fraction = entry.get("fraction")
            if (not isinstance(target, int) or isinstance(target, bool)
                    or not 1 <= target <= page_count):
                errors.append({"page": source_page, "kind": "reader_goto_target", "entry": entry})
            elif (not isinstance(fraction, (int, float)) or isinstance(fraction, bool)
                  or not 0 <= fraction <= 1):
                errors.append({"page": source_page, "kind": "reader_fraction", "entry": entry})
        else:
            errors.append({"page": source_page, "kind": "reader_kind", "entry": entry})


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
                  "link_errors": [], "adjusted_regions": len(report.get("flow", [])), "samples": []}
        reader_links = report.get("links") or []
        check_reader_links(reader_links, len(document["pages"]), result["link_errors"])
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
                page_links = page.get_links()
                output_uris = {normalized_uri(link.get("uri") or "") for link in page_links
                               if link.get("kind") == pymupdf.LINK_URI}
                visible_text = "".join(page.get_text().split())
                translation_text = "".join("".join(
                    b.get("translation", "") for b in saved["blocks"]
                    if b.get("target") == target).split())
                source_links = original[index].get_links()
                # A source web annotation is only allowed to disappear when its
                # URL no longer appears anywhere on the page; if the URL survived
                # the translation the reader must still be able to click it.
                # Repeated annotations of the same URL collapse to one entry.
                reported_uris = set()
                for annotation in source_links:
                    if annotation.get("kind") != pymupdf.LINK_URI:
                        continue
                    uri = (annotation.get("uri") or "").strip()
                    if not uri or (uri not in visible_text and uri not in translation_text):
                        continue
                    key = normalized_uri(uri)
                    if not key or key in output_uris or key in reported_uris:
                        continue
                    reported_uris.add(key)
                    result["link_errors"].append({"page": index + 1, "kind": "missing_uri", "uri": uri})
                # Rewording a citation may move a jump target, but it must not
                # silently remove the jump from the page. Unresolvable named
                # destinations are excluded because no annotation can be rebuilt.
                source_gotos = [annotation for annotation in source_links
                                if annotation.get("kind") in {pymupdf.LINK_GOTO, pymupdf.LINK_NAMED}
                                and isinstance(annotation.get("page"), int)
                                and not isinstance(annotation.get("page"), bool)
                                and 0 <= annotation["page"] < len(document["pages"])]
                output_gotos = [link for link in page_links
                                if link.get("kind") in {pymupdf.LINK_GOTO, pymupdf.LINK_NAMED}]
                if len(output_gotos) < len(source_gotos):
                    result["link_errors"].append({"page": index + 1, "kind": "missing_goto",
                                                  "source": len(source_gotos), "output": len(output_gotos)})
                # Reader entries name the page the link lives on; the rectangle
                # must exist there, so a goto whose target page leaked into
                # source_page is caught instead of being placed on the wrong page.
                for entry in (item for item in reader_links
                              if item.get("source_page") == index + 1):
                    if entry.get("kind") == "uri":
                        candidates = [link for link in page_links
                                      if link.get("kind") == pymupdf.LINK_URI
                                      and normalized_uri(link.get("uri") or "")
                                      == normalized_uri(entry.get("uri") or "")]
                    else:
                        candidates = [link for link in page_links
                                      if link.get("kind") in {pymupdf.LINK_GOTO, pymupdf.LINK_NAMED}
                                      and isinstance(link.get("page"), int)
                                      and not isinstance(link.get("page"), bool)
                                      and link["page"] + 1 == entry.get("page")]
                    matched = any(matches_reader_entry(page, link, entry) for link in candidates)
                    if not matched and entry.get("kind") == "goto":
                        # An unresolved named destination exposes no target page
                        # to PyMuPDF; its placement on this page is still
                        # checkable, which is enough to catch a source_page leak.
                        matched = any(link.get("kind") == pymupdf.LINK_NAMED
                                      and matches_reader_entry(page, link, entry)
                                      for link in page_links)
                    if not matched:
                        result["link_errors"].append({"page": index + 1, "kind": "reader_mismatch",
                                                      "entry": entry})
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
               ("missing_chinese", "collisions", "spacing_errors", "font_changes", "bold_body",
                "symbol_errors", "link_errors"))


if __name__ == "__main__":
    raise SystemExit(audit())
