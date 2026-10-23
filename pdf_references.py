"""Reconstruct complete numbered bibliography entries before translating them."""
import copy
from collections import Counter
import html
import re
import unicodedata

import pymupdf
from pdf_typography import space_cjk_latin

SCHEMA = 1


def clean_text(value):
    value = re.sub(r"(?<=\w)-\s*\n\s*(?=[a-z])", "", value)
    for accent, combining in (("¨", "\u0308"), ("´", "\u0301"), ("`", "\u0300")):
        value = re.sub(re.escape(accent) + r"([A-Za-z])", lambda m: unicodedata.normalize("NFC", m[1] + combining), value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"(https?://\S+/)\s+(?=[A-Za-z0-9])", r"\1", value)
    return re.sub(r"(?<=\d)([–-])\s+(?=\d)", r"\1", value)


def split_reference(text):
    match = re.search(r'[“"]([^”"]+)[”"]', text)
    if not match:
        return None
    prefix, suffix = text[:match.start(1)], text[match.end(1):]
    number = re.match(r"\s*\[(\d+)\]", text)
    authors = re.sub(r'^\s*\[\d+\]\s*', '', prefix).rstrip(' ,“"')
    return {"prefix": prefix, "title": match[1].rstrip(" ,;"), "suffix": suffix,
            "authors": authors, "publication": suffix.lstrip('”" ,'), "number": int(number[1]) if number else None}


def translation_source(block):
    reference = block.get("reference")
    return reference["title"] if reference else block["text"]


def format_translation(block, title):
    reference = block.get("reference")
    if not reference:
        return title.strip()
    title = title.strip().strip('“”"')
    if (not title or "\n" in title or re.search(r"\[\d+\]", title)
            or reference["authors"] in title or reference["publication"] in title
            or len(title) > max(240, len(reference["title"]) * 5)):
        raise ValueError("参考文献仅接受本条论文标题的译文，不接受相邻条目或作者出版信息")
    separator = "。" if re.search(r"[\u3400-\u9fff]", title) else ". "
    return f"[{reference['number']}] {reference['authors']}. {title.rstrip('。. ')}{separator}{reference['publication']}"


def _visual_lines(page):
    native = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    lines = []
    for block in native["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            text = "".join(s["text"] for s in line["spans"]).strip()
            if text:
                lines.append({"text": text, "rect": pymupdf.Rect(line["bbox"])})
    return lines


def _rows(lines):
    rows = []
    for line in sorted(lines, key=lambda item: (item["rect"].y0, item["rect"].x0)):
        match = next((row for row in reversed(rows[-3:]) if abs(row["rect"].y0 - line["rect"].y0) < 1.6), None)
        if match:
            match["fragments"].append(line)
            match["rect"] |= line["rect"]
        else:
            rows.append({"rect": pymupdf.Rect(line["rect"]), "fragments": [line]})
    for row in rows:
        row["text"] = " ".join(fragment["text"] for fragment in sorted(row["fragments"], key=lambda item: item["rect"].x0))
    return rows


def discover(pdf):
    """Fail closed for multi-page bibliographies; never merge half an entry."""
    for page_index, page in enumerate(pdf):
        lines = _visual_lines(page)
        heading = next((line for line in lines if re.fullmatch(r"REFERENCES|BIBLIOGRAPHY|参考文献", line["text"], re.I)), None)
        if heading is None:
            continue
        bounds = page.rect * page.derotation_matrix
        mid = bounds.width / 2
        double = (any(line["rect"].width > bounds.width * .2 and line["rect"].x1 <= mid + 6 for line in lines)
                  and any(line["rect"].width > bounds.width * .2 and line["rect"].x0 >= mid - 6 for line in lines))
        if double:
            start_column = int((heading["rect"].x0 + heading["rect"].x1) / 2 >= mid)
            columns = [[line for line in lines if int((line["rect"].x0 + line["rect"].x1) / 2 >= mid) == column]
                       for column in range(start_column, 2)]
        else:
            columns = [lines]
        entries, regions, current = [], [], None
        for column_index, column in enumerate(columns):
            selected = [line for line in column if (column_index > 0 or line["rect"].y0 >= heading["rect"].y1 - .5)]
            selected = [line for line in selected if not re.fullmatch(r"\d+", line["text"]) or line["rect"].y0 < bounds.height - 30]
            rows = _rows(selected)
            if not rows:
                continue
            region = pymupdf.Rect(rows[0]["rect"])
            for row in rows:
                region |= row["rect"]
                marker = re.match(r"^\s*\[(\d{1,4})\]\s*(.*)", row["text"])
                if marker:
                    current = {"number": int(marker[1]), "rows": [], "parts": []}
                    entries.append(current)
                if current is None:
                    return None
                current["rows"].append(row["text"])
                part = {"page": page_index + 1, "column": column_index, "rect": list(row["rect"])}
                current["parts"].append(part)
            regions.append({"column": column_index, "rect": list(region)})
        if len(entries) < 2 or [entry["number"] for entry in entries] != list(range(1, len(entries) + 1)):
            return None
        # A missing closing title/terminal year usually means a continuation on
        # the next page. Preserve that section instead of silently truncating it.
        if page_index != len(pdf) - 1:
            return None
        for entry in entries:
            entry["text"] = clean_text("\n".join(entry.pop("rows")))
            entry["reference"] = split_reference(entry["text"])
            if entry["reference"] is None:
                return None
        return {"page": page_index + 1, "heading_rect": list(heading["rect"]), "regions": regions, "entries": entries}
    return None


def normalize_document(document, pdf, *, force=False):
    if document.get("reference_schema") == SCHEMA:
        return False
    discovered = discover(pdf)
    if discovered is None:
        return False
    saved_page = document["pages"][discovered["page"] - 1]
    page = pdf[discovered["page"] - 1]
    heading_rect = pymupdf.Rect(discovered["heading_rect"])
    regions = [pymupdf.Rect(region["rect"]) + (-2, -.5, 2, .5) for region in discovered["regions"]]
    retained, legacy, heading_id = [], [], None
    for block in saved_page["blocks"]:
        box = pymupdf.Rect(block["bbox"][0] * page.rect.width, block["bbox"][1] * page.rect.height,
                           block["bbox"][2] * page.rect.width, block["bbox"][3] * page.rect.height) * page.derotation_matrix
        center = (box.tl + box.br) * .5
        if heading_rect.contains(center):
            block = {**block, "kind": "reference_heading"}
            heading_id = block["id"]
            retained.append(block)
        elif any(region.contains(center) for region in regions):
            legacy.append(copy.deepcopy(block))
        else:
            retained.append(block)
    if not legacy or not heading_id:
        return False
    # Existing fragment translations cannot safely be matched to complete
    # titles. Only an explicit reviewed migration may replace them with blanks.
    if not force and any(block.get("translation") for block in legacy):
        return False
    for entry in discovered["entries"]:
        first_column = entry["parts"][0]["column"]
        boxes = [pymupdf.Rect(part["rect"]) for part in entry["parts"] if part["column"] == first_column]
        rect = pymupdf.Rect(boxes[0])
        for box in boxes[1:]:
            rect |= box
        displayed = rect * page.rotation_matrix
        bounds = page.rect
        retained.append({"id": f"p{discovered['page']}-ref{entry['number']}", "kind": "reference", "reference_number": entry["number"],
                         "text": entry["text"], "reference": entry["reference"], "source_parts": entry["parts"],
                         "bbox": [displayed.x0 / bounds.width, displayed.y0 / bounds.height, displayed.x1 / bounds.width, displayed.y1 / bounds.height],
                         "translation": "", "target": "", "edited": False})
    saved_page["blocks"] = retained
    saved_page["reference_layout"] = {key: value for key, value in discovered.items() if key != "entries"}
    saved_page["reference_layout"]["heading_id"] = heading_id
    document["reference_legacy"] = {"page": discovered["page"], "blocks": legacy}
    document["reference_schema"] = SCHEMA
    document["total_blocks"] = sum(len(page["blocks"]) for page in document["pages"])
    return True

def prepare_bibliography(page, saved_page, native, overlays, prepared, source_rects, typography, target):
    """Preflight complete entries with hanging labels; never shrink each fragment."""
    layout = saved_page.get("reference_layout")
    if not layout:
        return None
    entries = sorted((block for block in saved_page["blocks"] if block.get("kind") == "reference"), key=lambda block: block["reference_number"])
    heading = next(block for block in saved_page["blocks"] if block["id"] == layout["heading_id"])
    heading_source = pymupdf.Rect(layout["heading_rect"])
    regions = [pymupdf.Rect(region["rect"]) for region in layout["regions"]]
    first = regions[0]
    left = min(first.x0, heading_source.x0)
    right = max(first.x1, heading_source.x1)
    # Reclaim the whitespace after the translated conclusion, without moving
    # references over figures or prose that still occupies the column.
    preceding = [result["rect"][3] for block, _, result in prepared
                 if source_rects[block["id"]].y1 <= heading_source.y0
                 and source_rects[block["id"]].x0 >= left - 10
                 and source_rects[block["id"]].x1 <= right + 10]
    heading_top = min(heading_source.y0, max(preceding, default=heading_source.y0 - 12) + 12)
    bottom = min(page.rect.height - 36, max(region.y1 for region in regions) + 9)
    columns = [pymupdf.Rect(left, heading_top + 19, right, bottom)]
    columns += [pymupdf.Rect(region.x0, region.y0, region.x1, bottom) for region in regions[1:]]
    start = len(overlays)
    normalize = lambda value: Counter(c for c in unicodedata.normalize("NFKC", value) if not c.isspace() and c != "\u00ad")
    for size in (8.5, 8.25, 8.0):
        while len(overlays) > start:
            overlays.delete_page(-1)
        results, column, top, failed = [], 0, columns[0].y0, False
        for block in entries:
            text = block["translation"] if block.get("target") == target and block.get("translation") else block["text"]
            # Hang continuation lines under the authors, not under the number.
            content = re.sub(r"^\s*\[\d+\]\s*", "", text)
            reference = block.get("reference", {})
            author_prefix = reference.get("authors", "") + ". "
            publication = reference.get("publication", "")
            if content.startswith(author_prefix) and publication and content.endswith(publication):
                title = space_cjk_latin(content[len(author_prefix):-len(publication)])
                inner = ('<span class="meta">' + html.escape(author_prefix) + '</span>'
                         + '<span class="title">' + html.escape(title) + '</span>'
                         + '<span class="meta">' + html.escape(publication) + '</span>')
            else:
                inner = html.escape(content)
            markup = f'<div class="entry">[{block["reference_number"]}] {inner}</div>'
            meta_size = max(7.5, size - .5)
            css = typography["css"] + (f"*{{margin:0;padding:0}}body{{font-family:serif;font-size:{meta_size}pt;"
                                        "line-height:1.24;text-align:left;font-weight:400}"
                                        ".entry{margin-left:14pt;text-indent:-14pt}"
                                        f".title{{font-family:{typography['family']};font-size:{size}pt}}")
            placed = None
            while column < len(columns):
                box = pymupdf.Rect(columns[column].x0, top, columns[column].x1, columns[column].y1)
                if box.height < size * 1.3:
                    column += 1
                    if column < len(columns):
                        top = columns[column].y0
                    continue
                scratch = overlays.new_page(width=page.rect.width, height=page.rect.height)
                spare, scale = scratch.insert_htmlbox(box, markup, css=css, archive=typography["archive"], scale_low=1)
                ink = [item for item in scratch.get_text("blocks") if item[6] == 0 and item[4].strip()]
                if spare >= 0 and ink and not (normalize(text) - normalize(scratch.get_text(clip=box))):
                    crop = pymupdf.Rect(box)
                    crop.y1 = min(box.y1, max(item[3] for item in ink) + 1)
                    placed = {"overlay": len(overlays) - 1, "rect": list(crop), "clip": list(crop), "scale": 1}
                    top = crop.y1 + 3
                    break
                overlays.delete_page(-1)
                column += 1
                if column < len(columns):
                    top = columns[column].y0
            if placed is None:
                failed = True
                break
            results.append((block, {"spans": [], "size": size, "lines": []}, placed))
        if failed:
            continue
        # The first entry owns the original bibliography's text for redaction;
        # the remaining entries only paint their new preflighted overlays.
        owned = [span for item in native for line in item["lines"] for span in line["spans"]
                 if any((region + (-2, -.5, 2, .5)).contains((pymupdf.Rect(span["bbox"]).tl + pymupdf.Rect(span["bbox"]).br) * .5)
                        for region in regions + [heading_source])]
        results[0][1]["spans"] = owned
        scratch = overlays.new_page(width=page.rect.width, height=page.rect.height)
        heading_box = pymupdf.Rect(left, heading_top, right, heading_top + 17)
        text = heading.get("translation") if heading.get("target") == target else "References"
        text = text or "参考文献"
        css = typography["css"] + f"body{{margin:0;font-family:{typography['family']};font-size:10pt;font-weight:600;text-align:center}}"
        spare, _ = scratch.insert_htmlbox(heading_box, html.escape(text), css=css, archive=typography["archive"], scale_low=1)
        if spare < 0:
            continue
        results.append((heading, {"spans": [], "size": 10, "lines": []},
                        {"overlay": len(overlays) - 1, "rect": list(heading_box), "clip": list(heading_box), "scale": 1}))
        return results, {"entries": len(entries), "font_size": size, "metadata_font_size": meta_size, "line_height": 1.24}
    while len(overlays) > start:
        overlays.delete_page(-1)
    return None
