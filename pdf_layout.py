"""Conservative, deterministic typesetting onto the original PDF.

Preflight every replacement on a separate PDF before removing any source text.
Uncertain/overflowing regions remain intact and are reported to the reader.
"""
import hashlib
import html
import json
import re
import threading
import unicodedata
import copy
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pymupdf
from pdf_typography import font_resources, font_signature, space_cjk_latin
from pdf_flow import (visible_drawings, heading as section_heading, original_formula_units,
                      column_lanes, reflow_columns, text_ink_bounds)
from pdf_references import (REFERENCE_MARK, fallback_reference_regions, prepare_bibliography,
                            reference_fallback_pages)
from pdf_text import normalize_drop_caps

ET.register_namespace("", "http://www.w3.org/2000/svg")
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")

PDF_LOCK = threading.RLock()
ENGINE_VERSION = 55
MATH_FONT = re.compile(r"math|symbol|cmmi|cmsy|cmex|msam|msbm|stmary|rsfs", re.I)
INSTITUTION = re.compile(r"universit|institut|laborator|department|school|academy|college|hospital|研究所|大学|学院|实验室", re.I)
# Author lines mark affiliations with raised digits. Publishers put them right
# after the surname ("Schoellig1,2"), after a comma ("Panerati,1,2"), after a
# space or CJK punctuation, and sometimes beside a footnote symbol. The whole
# marker is one superscript run; a preceding name is not part of the markup.
AUTHOR_MARKS = re.compile(
    r"(?<![\d])(?P<marks>\d{1,2}(?:\s*[,，]\s*\d{1,2})*)(?=[\s,，、;；*∗†‡]|$)",
    re.UNICODE)
_INLINE_MATH_FONT = re.compile(r"^(?:CM[A-Z]+\d*|MSAM\d*|MSBM\d*|STMARY\w*|RSFS\w*|SYMBOL\w*|.*MATH.*)$", re.I)
_INLINE_MATH_PRIMARY = re.compile(r"^(?:CMMI|CMSY|CMEX|MSAM|MSBM|STMARY|RSFS|SYMBOL|.*MATH.*)", re.I)


def source_rect(page, block):
    x0, y0, x1, y1 = block["bbox"]
    bounds = page.rect
    return pymupdf.Rect(x0 * bounds.width, y0 * bounds.height,
                        x1 * bounds.width, y1 * bounds.height) * page.derotation_matrix


def heading_separator(drawing, rect):
    """Recognize a decorative bar drawn between a section number and its title.

    Publishers often draw the bar in "1 | Introduction" as a vector stroke
    instead of a text glyph, so replacing the heading text would leave it
    behind and let it cut through the translated characters.
    """
    box = pymupdf.Rect(drawing["rect"])
    return (box.width <= 1.6 and box.height >= 1.5
            and (rect + (-.75, -.75, .75, .75)).contains(box))


def overlap(a, b):
    return max(0, min(a.x1, b.x1) - max(a.x0, b.x0)) * max(0, min(a.y1, b.y1) - max(a.y0, b.y0))


def is_prose(text):
    # Inline symbols and subscripts do not make an entire paragraph an equation.
    return ((len(re.findall(r"\b[A-Za-z]{2,}\b", text)) >= 8
             and len(re.findall(r"\b(?:the|is|of|and|to|in|with|for|can|are|this|that|from|where|which)\b", text, re.I)) >= 2)
            or len(re.findall(r"[\u3400-\u9fff]", text)) >= 20)


def narrative_fragment(text):
    return is_prose(text) or bool(re.match(r"\s*where\b", text, re.I)) or bool(re.search(r"\b(?:is the|is a|when the|denotes the)\b", text, re.I))


_LIST_LINE = re.compile(r"(?m)^\s*(?:[•·●▪▫◦‣∙⋅※■□◆◇]|\(?\d{1,2}[.)]|[A-Za-z][.)])\s+")


def join_soft_lines(value):
    """Treat line breaks inside one source paragraph as soft wraps.

    Models sometimes return a paragraph as several lines. A single extraction
    block is one paragraph, so those breaks must not become forced breaks in
    the middle of the translation. Strong CJK punctuation still glues the two
    lines directly; otherwise one space keeps Latin words separated.
    """
    text = re.sub(r"[ \t]*\r?\n+[ \t]*", " ", value or "").strip()
    return re.sub(r"(?<=[。！？；：，、]) (?=[\u3400-\u9fff（“‘《【])", "", text)


def soft_translation(block):
    """Collapse translation-only line breaks for prose blocks, never for lists.

    A translated paragraph must stay one paragraph. Blocks that are lists,
    nomenclature tables or other line-structured fragments keep their breaks.
    """
    text = block.get("translation") or ""
    if "\n" not in text and "\r" not in text:
        return text
    source = block.get("text") or ""
    if (not is_prose(source) or _LIST_LINE.search(source)
            or re.search(r"\n[ \t]*\n", source)):
        return text
    return join_soft_lines(text)


def normalized_text(value):
    return "".join(char for char in unicodedata.normalize("NFKC", value or "")
                   if not char.isspace() and char != "\u00ad")


def normalized_inline_math(value):
    """Compare source math glyphs with plain-text subscript/superscript markup."""
    replacements = {"∗": "*", "−": "-", "–": "-", "‑": "-", "‐": "-",
                    "，": ",", "、": ",", "；": ";"}
    result = []
    for char in value or "":
        for item in unicodedata.normalize("NFKC", char):
            if item.isspace() or item in "_^{}" or item == "\u00ad":
                continue
            result.append(replacements.get(item, item))
    return "".join(result)


def _inline_math_font(font, text=""):
    if not _INLINE_MATH_FONT.match(font or ""):
        return False
    # Bold Latin runs can be ordinary emphasized prose, not a variable.
    return not (font.upper().startswith("CMBX") and len(normalized_inline_math(text)) > 1)


def _inline_math_primary(font, text=""):
    return bool(_INLINE_MATH_PRIMARY.match(font or "") or
                (font.upper().startswith("CMBX") and len(normalized_inline_math(text)) == 1))


def _inline_math_run(chars, primary):
    visible = [char for char in chars if char.get("c", "").strip()]
    text = "".join(char.get("c", "") for char in chars)
    normalized = normalized_inline_math(text)
    if not primary or not visible or not normalized:
        return None
    rect = pymupdf.Rect(visible[0]["bbox"])
    for char in visible[1:]:
        rect |= pymupdf.Rect(char["bbox"])
    return {"text": text, "normalized": normalized, "rect": rect,
            "chars": chars, "primary": primary}


def inline_math_runs(native, rect, combine=False):
    """Find original math-font runs on prose lines, retaining script geometry."""
    def span_text(span):
        return span.get("text") or "".join(char.get("c", "") for char in span.get("chars", []))

    candidates = [block for block in native
                  if block.get("type", 0) == 0 and overlap(rect, pymupdf.Rect(block["bbox"])) > .5]
    if not candidates:
        return []
    best = max(candidates, key=lambda block: overlap(rect, pymupdf.Rect(block["bbox"])))
    selected = ([block for block in candidates
                 if overlap(rect, pymupdf.Rect(block["bbox"]))
                 >= pymupdf.Rect(block["bbox"]).get_area() * .5] if combine else [best])
    runs = []
    for block in selected:
        for line in block.get("lines", []):
            line_box = pymupdf.Rect(line["bbox"])
            if overlap(rect, line_box) < line_box.get_area() * .5:
                continue
            prose = any(not _inline_math_font(span.get("font", ""), span_text(span))
                        and re.search(r"[A-Za-z]{2,}|[\u3400-\u9fff]{2,}", span_text(span))
                        for span in line.get("spans", []))
            if not prose:
                continue
            group, primary = [], False

            def flush():
                nonlocal group, primary
                run = _inline_math_run(group, primary)
                if run:
                    runs.append(run)
                group, primary = [], False

            for span in line.get("spans", []):
                font = span.get("font", "")
                text = span_text(span)
                if not _inline_math_font(font, text):
                    flush()
                    continue
                span_primary = _inline_math_primary(font, text)
                for char in span.get("chars", []):
                    value = char.get("c", "")
                    if not value or (value.isspace() and not group):
                        continue
                    box = pymupdf.Rect(char["bbox"])
                    size = float(span.get("size", 0) or 0)
                    item = {"c": value, "bbox": tuple(box), "origin": char.get("origin"),
                            "font": font, "size": size}
                    if group:
                        previous = group[-1]
                        previous_box = pymupdf.Rect(previous["bbox"])
                        gap = box.x0 - previous_box.x1
                        shift = abs(box.y0 - previous_box.y0)
                        max_gap = max(6, min(10, (previous["size"] + size) * .4))
                        max_shift = max(8, max(previous["size"], size) * .9)
                        if gap > max_gap or shift > max_shift:
                            flush()
                    group.append(item)
                    primary = primary or span_primary
            flush()
    return runs


def _normalized_page_chars(page):
    chars, normalized, mapping = [], [], []
    line_number = 0
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    index = len(chars)
                    item = {"c": char.get("c", ""), "bbox": pymupdf.Rect(char["bbox"]),
                            "origin": char.get("origin"), "line": line_number}
                    chars.append(item)
                    for value in normalized_inline_math(item["c"]):
                        normalized.append(value)
                        mapping.append(index)
            line_number += 1
    return "".join(normalized), mapping, chars


def _inline_math_match(text, needle, start, mapping, chars, strict=True):
    if not needle:
        return None
    while True:
        index = text.find(needle, start)
        if index < 0:
            return None
        before = text[index - 1] if index else ""
        after_index = index + len(needle)
        after = text[after_index] if after_index < len(text) else ""
        boundary = True
        if strict:
            boundary = not (needle[0].isascii() and needle[0].isalnum()
                            and before.isascii() and before.isalnum())
            boundary = boundary and not (needle[-1].isascii() and needle[-1].isalnum()
                                         and after.isascii() and after.isalnum())
        if not boundary:
            start = index + 1
            continue
        first, last = mapping[index], mapping[index + len(needle) - 1]
        raw = chars[first:last + 1]
        lines = {char["line"] for char in raw}
        box = pymupdf.Rect(raw[0]["bbox"])
        for char in raw[1:]:
            box |= char["bbox"]
        return {"start": index, "end": after_index, "first": first, "last": last,
                "box": box, "line": next(iter(lines)) if len(lines) == 1 else None,
                "multiline": len(lines) != 1}


def inline_math_graphics(drawings, runs):
    """Own short vector strokes inside math runs that are copied with the glyphs."""
    owned = []
    for drawing in drawings:
        box = pymupdf.Rect(drawing["rect"])
        if (drawing.get("type") != "s" or not drawing.get("items")
                or any(item[0] != "l" for item in drawing["items"])):
            continue
        for run in runs:
            size = max((char["size"] for char in run["chars"]), default=0)
            if (size > 0 and box.width <= size * 2 and box.height <= size * .5
                    and (run["rect"] + (-.5, -.5, .5, .5)).contains(box)):
                owned.append(tuple(box))
                break
    return owned


def translated_math_runs(runs, translation):
    """Keep exact expressions intact; split unmatched lists into whole variables.

    Translators often turn the commas between basis vectors into a Chinese
    enumeration or a conjunction. Operators and function arguments are never
    split, and accents/subscripts stay attached to their original base glyph.
    """
    normalized = normalized_inline_math(translation)
    result = []
    for run in runs:
        if run["normalized"] in normalized:
            result.append(run)
            continue
        chars = [c for c in run["chars"] if not c["c"].isspace()]
        if chars and chars[0]["c"] == "(" and chars[-1]["c"] == ")":
            chars = chars[1:-1]
        text = "".join(c["c"] for c in chars)
        if not re.search(r"[,;]", text) or re.search(r"[=+*/()\[\]{}<>≤≥∑∫⊗]", text):
            result.append(run)
            continue
        parts, current = [], []
        for char in chars:
            if char["c"] in ",;":
                part = _inline_math_run(current, True)
                if part:
                    parts.append(part)
                current = []
            else:
                current.append(char)
        part = _inline_math_run(current, True)
        if part:
            parts.append(part)
        if parts and all(part["normalized"] in normalized for part in parts):
            result.extend(parts)
        else:
            result.append(run)
    return result


def inline_math_assets(page, runs, translation=None):
    """Extract only the original glyph paths, without neighboring page ink."""
    svg = ET.fromstring(page.get_svg_image(text_as_path=True))
    ns = "{http://www.w3.org/2000/svg}"
    href = "{http://www.w3.org/1999/xlink}href"
    definitions = {node.get("id"): node for node in svg.iter() if node.get("id")}
    uses = []
    for node in svg.iter(ns + "use"):
        values = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", node.get("transform", ""))
        if len(values) == 6:
            uses.append((node, float(values[4]), float(values[5])))
    assets = []
    if translation is not None:
        runs = translated_math_runs(runs, translation)
    for run in runs:
        selected = []
        for char in run["chars"]:
            if not char["c"].strip() or not char.get("origin"):
                continue
            x, y = char["origin"]
            match = next((node for node, px, py in uses
                          if abs(px - x) < .04 and abs(py - y) < .04
                          and node.get("data-text") in (char["c"], "\ufffd")), None)
            if match is not None:
                selected.append(match)
        if len(selected) != sum(bool(c["c"].strip()) for c in run["chars"]):
            continue
        box = run["rect"] + (-.2, -.2, .2, .2)
        root = ET.Element(ns + "svg", {"width": str(box.width), "height": str(box.height),
                                      "viewBox": f"{box.x0} {box.y0} {box.width} {box.height}"})
        defs = ET.SubElement(root, ns + "defs")
        for key in {node.get(href, "")[1:] for node in selected}:
            if key in definitions:
                defs.append(copy.deepcopy(definitions[key]))
        root.extend(copy.deepcopy(node) for node in selected)
        owned_strokes = inline_math_graphics(page.get_drawings(), [run])
        for drawing in page.get_drawings():
            if tuple(drawing["rect"]) in owned_strokes:
                color = drawing.get("color") or (0, 0, 0)
                for _, first, last in drawing["items"]:
                    ET.SubElement(root, ns + "path", {
                        "d": f"M {first.x} {first.y} L {last.x} {last.y}",
                        "stroke": "rgb(" + ",".join(str(round(c * 255)) for c in color) + ")",
                        "stroke-width": str(drawing.get("width", .4))})
        # Font bounding boxes can extend into neighboring lines (especially
        # accents). Reserve the actual path ink height, keeping the baseline.
        with pymupdf.open(stream=ET.tostring(root), filetype="svg") as vector:
            with pymupdf.open(stream=vector.convert_to_pdf(), filetype="pdf") as measured:
                ink = [drawing["rect"] for drawing in measured[0].get_drawings()]
        if ink:
            top = min(rect.y0 for rect in ink)
            bottom = max(rect.y1 for rect in ink)
            box = pymupdf.Rect(box.x0, box.y0 + top - .15, box.x1, box.y0 + bottom + .15)
        # HTML rasterizes SVG; oversample paths without changing CSS size.
        root.set("width", str(box.width * 6))
        root.set("height", str(box.height * 6))
        root.set("viewBox", f"{box.x0} {box.y0} {box.width} {box.height}")
        baseline = max(run["chars"], key=lambda c: c["size"])["origin"][1]
        assets.append({"normalized": run["normalized"], "svg": ET.tostring(root),
                       "width": box.width, "height": box.height, "descent": box.y1 - baseline})
    return assets


def formula_glyph_bounds(page, spans, raw_native):
    """Measure actual formula strokes when font frames touch adjacent prose."""
    boxes = {tuple(span["bbox"]) for span in spans}
    chars = [{**char, "size": span["size"], "font": span["font"]}
             for item in raw_native if item.get("type", 0) == 0
             for line in item.get("lines", []) for span in line.get("spans", [])
             if tuple(span["bbox"]) in boxes for char in span.get("chars", [])]
    run = _inline_math_run(chars, True)
    if run is None:
        return None
    assets = inline_math_assets(page, [run])
    if not assets:
        return None
    x, y, width, height = map(float, ET.fromstring(assets[0]["svg"]).get("viewBox").split())
    return pymupdf.Rect(x, y, x + width, y + height)


_AUTO_URL = re.compile(r"https?://[^\s\u3400-\u9fff<>\"'，。；：！？）】」』《【〕]+")
_URL_TRAILING = ".,;:!?)]}'\"。、；，！？）：」』）》"


def _trim_url(value):
    return value.rstrip(_URL_TRAILING)


def page_link_targets(page, names=None):
    """Actionable link annotations of one page, in unrotated page coordinates.

    PyMuPDF reports link rectangles in displayed coordinates, and clearing the
    runtime rotation does not change them. Derotate with the page's current
    rotation, so callers must not reset the rotation first: the rectangles then
    share the coordinate space of text extraction and block rectangles. Only
    kinds the reader can act on are kept: web/mailto references and jumps
    inside the document.
    """
    targets = []
    for link in page.get_links():
        kind = link.get("kind")
        box = pymupdf.Rect(link.get("from") or (0, 0, 0, 0)) * page.derotation_matrix
        if box.is_empty or not box.is_valid or box.get_area() <= 0:
            continue
        if kind == pymupdf.LINK_URI:
            uri = (link.get("uri") or "").strip()
            if re.match(r"(?:https?|mailto):", uri, re.I):
                targets.append({"rect": [box.x0, box.y0, box.x1, box.y1], "kind": "uri", "uri": uri})
            continue
        target, point = link.get("page"), link.get("to")
        if kind == pymupdf.LINK_NAMED and names:
            resolved = names.get(link.get("name") or link.get("nameddest") or "")
            if resolved:
                target = resolved.get("page")
                point = resolved.get("to") or point
        if not isinstance(target, int) or target < 0:
            continue
        where = pymupdf.Point(point) if point else pymupdf.Point(0, 0)
        targets.append({"rect": [box.x0, box.y0, box.x1, box.y1], "kind": "goto",
                        "page": target, "to": [where.x, where.y]})
    return targets


def goto_fraction(page, point):
    """Vertical reading fraction of a link destination on its target page."""
    point = pymupdf.Point(point or (0, 0))
    height = page.rect.height
    if not height:
        return 0.0
    value = point.x if page.rotation % 180 else point.y
    return max(0.0, min(1.0, value / height))


_LINK_TRIM = ",.;:!?，。；：！？、·…—–-\"'“”‘’"


def link_needle(value):
    """Normalize an annotation's visible text into a search needle.

    Translations regularly move or change the punctuation at the edge of the
    linked phrase ("[19]," becomes "[19]"), so edge punctuation is not part of
    the needle. Brackets stay: they identify citation and figure references.
    """
    return normalized_inline_math((value or "").strip().strip(_LINK_TRIM).strip())


def link_needle_usable(needle):
    # Very short needles match unrelated text; URLs and citation markers are
    # longer than this after normalization.
    return 3 <= len(needle or "") <= 600


def _link_text_items(native, box=None):
    """Visible characters (or spans) of `native`, optionally inside one box.

    Character boxes make the annotation's own text precise: a link that covers
    only "[19]" inside a long extracted line must not borrow the whole line as
    its anchor text. `native` is a rawdict block list when characters are
    available, otherwise a plain dict block list.
    """
    items = []
    for item in native:
        if item.get("type", 0) != 0:
            continue
        for line in item.get("lines", []):
            for span in line.get("spans", []):
                color = span.get("color", 0)
                chars = span.get("chars")
                if chars:
                    for char in chars:
                        value = char.get("c", "")
                        if not value.strip():
                            continue
                        char_box = pymupdf.Rect(char["bbox"])
                        if box is not None and not (
                                overlap(char_box, box) >= char_box.get_area() * .5
                                or box.contains((char_box.tl + char_box.br) * .5)):
                            continue
                        items.append((round(char_box.y0, 1), char_box.x0, value, color))
                else:
                    text = span.get("text", "")
                    if not text.strip():
                        continue
                    span_box = pymupdf.Rect(span["bbox"])
                    if box is not None and not (
                            overlap(span_box, box) >= span_box.get_area() * .5
                            or box.contains((span_box.tl + span_box.br) * .5)):
                        continue
                    items.append((round(span_box.y0, 1), span_box.x0, text, color))
    items.sort(key=lambda item: (item[0], item[1]))
    return items


def block_link_hints(page_links, rect, native, raw_native=None):
    """Link annotations inside one block, with the source text they cover.

    That text becomes the search needle which re-anchors the link onto the
    translated paragraph; without a reliable text match no hint is emitted,
    because a guessed rectangle would point at unrelated content. A link that
    covers nearly the whole block is also marked as a whole-block link: if its
    text cannot be found in the translation, the link is attached to the
    translated block instead of being dropped (fully linked titles, author
    lines and license sentences are common).

    `color` is the source link text's color when the annotation covers text
    (publishers style links black, blue or gray); it lets the translation keep
    the original appearance instead of the HTML default link blue.
    """
    source = raw_native if raw_native is not None else native
    block_parts = _link_text_items(source, rect)
    block_text = normalized_inline_math(" ".join(text for _, _, text, _ in block_parts))
    hints = []
    for link in page_links:
        box = pymupdf.Rect(link["rect"])
        if overlap(box, rect) < box.get_area() * .5:
            continue
        parts = _link_text_items(source, box)
        needle = link_needle(" ".join(text for _, _, text, _ in parts))
        if not link_needle_usable(needle):
            continue
        colors = Counter(color for _, _, text, color in parts for _ in text.strip())
        hint = {"needle": needle, "kind": link["kind"], "source": link["rect"],
                "color": "#%06x" % colors.most_common(1)[0][0] if colors else None,
                "whole_block": bool(block_text) and len(needle) / len(block_text) >= .9}
        if link["kind"] == "uri":
            hint["uri"] = link["uri"]
        else:
            hint["page"] = link["page"]
            hint["to"] = link.get("to") or [0, 0]
        hints.append(hint)
    return hints


def _needle_occurrences(text, needle, strict=True):
    """Boundary-aware occurrence positions.

    The same boundary rule runs when the anchor is located on the rendered
    overlay page, so occurrence ordinals stay aligned between both searches.
    Link needles opt out of the boundary rule: normalized text removes the
    spaces around a URL or cross-reference, and such needles come from real
    annotations or unambiguous scheme-prefixed URLs, not short variables.
    """
    positions, start = [], 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return positions
        start = index + 1
        if strict:
            end = index + len(needle)
            before = text[index - 1] if index else ""
            after = text[end] if end < len(text) else ""
            if needle[0].isascii() and needle[0].isalnum() and before.isascii() and before.isalnum():
                continue
            if needle[-1].isascii() and needle[-1].isalnum() and after.isascii() and after.isalnum():
                continue
        positions.append(index)


def _link_ranges(value, text, mapping, matches, links):
    """Resolve link hints and literal URLs to ranges in the displayed text.

    Annotated links match by normalized source text (whitespace and NFKC
    differences ignored); any further http(s) URL that survived translation
    is anchored too. Math images win over links, and overlapping candidates
    keep the earliest, most specific one.
    """
    candidates = []
    math_ranges = [(start, end) for start, end, _ in matches]
    for hint in links or ():
        needle = link_needle(hint.get("needle") or "")
        if not link_needle_usable(needle):
            continue
        for ordinal, index in enumerate(_needle_occurrences(text, needle, strict=False)):
            end = index + len(needle)
            start, stop = mapping[index], mapping[end - 1] + 1
            candidates.append({"start": start, "end": stop, "kind": hint["kind"],
                               "uri": hint.get("uri"), "page": hint.get("page"),
                               "to": hint.get("to"), "source": hint.get("source"),
                               "color": hint.get("color"),
                               "ordinal": ordinal, "text": value[start:stop]})
    for match in _AUTO_URL.finditer(value):
        url = _trim_url(match[0])
        if len(url) < 9:
            continue
        start = match.start()
        needle = normalized_inline_math(url)
        occurrences = _needle_occurrences(text, needle, strict=False)
        ordinal = next((order for order, index in enumerate(occurrences)
                        if mapping[index] == start), None)
        if ordinal is None:
            continue
        candidates.append({"start": start, "end": start + len(url), "kind": "uri",
                           "uri": url, "page": None, "to": None, "source": None,
                           "ordinal": ordinal, "text": url})
    accepted = []
    for item in sorted(candidates, key=lambda item: (item["start"], -item["end"])):
        if any(item["start"] < end and start < item["end"] for start, end in math_ranges):
            continue
        if any(item["start"] < other["end"] and other["start"] < item["end"] for other in accepted):
            continue
        accepted.append(item)
    return accepted


def anchor_overlay_rects(page, anchors):
    """Rendered rectangles of link anchor texts on a preflight overlay page.

    The search runs over the painted characters, so ligatures, line breaks and
    hyphenation inside a URL do not prevent matching. An anchor wrapped across
    lines yields one rectangle per line. Repeated URLs are told apart by the
    occurrence ordinal recorded when the markup was built.
    """
    text, mapping, chars = _normalized_page_chars(page)
    if not text:
        return []
    placed = []
    for anchor in anchors:
        needle = normalized_inline_math(anchor.get("text", ""))
        if len(needle) < 3:
            continue
        found, position = None, 0
        for _ in range(int(anchor.get("ordinal", 0)) + 1):
            # Anchor text was already matched in the translation; normalized
            # text removes the space before a URL ("URL http://..."), so the
            # generic word-boundary rule must not reject it here.
            found = _inline_math_match(text, needle, position, mapping, chars, strict=False)
            if not found:
                break
            position = found["end"]
        if not found:
            continue
        rows = {}
        for char in chars[found["first"]:found["last"] + 1]:
            rows.setdefault(char["line"], []).append(char)
        rects = []
        for group in rows.values():
            box = pymupdf.Rect(group[0]["bbox"])
            for char in group[1:]:
                box |= pymupdf.Rect(char["bbox"])
            box = box + (-.2, -.2, .2, .2)
            if box.width > 1 and box.height > 1:
                rects.append([box.x0, box.y0, box.x1, box.y1])
        if rects:
            placed.append({"kind": anchor.get("kind", "uri"), "uri": anchor.get("uri"),
                           "page": anchor.get("page"), "to": anchor.get("to"),
                           "source": anchor.get("source"), "rects": rects})
    return placed


BOLD_MARKER = re.compile(r"(Q\d+[\).]|(?<!\d)\d{1,2}[\).]|(?<![A-Za-z])[A-Za-z][\).]|[•·●▪◆])\s*$")


def inline_bold_hints(style, text):
    """Locate bold run-in labels whose emphasis should survive translation.

    Only structural labels are returned: a leading label, a label after a
    number/bullet, or a leading caption word. Arbitrary emphasis inside a
    sentence cannot be aligned across languages and is left untouched.
    """
    lines = style.get("lines") or []
    total = sum(len(span["text"].strip()) for line in lines for span in line["spans"] if span["text"].strip())
    bold = sum(len(span["text"].strip()) for line in lines for span in line["spans"]
               if span["text"].strip() and span["flags"] & 16)
    if not total or not bold or bold > total * .55:
        return []
    hints = []
    history = ""
    for line_index, line in enumerate(lines):
        spans = [span for span in line["spans"] if span["text"].strip()]
        before = ""
        index = 0
        while index < len(spans):
            if not spans[index]["flags"] & 16:
                before += spans[index]["text"]
                index += 1
                continue
            run = []
            while index < len(spans) and spans[index]["flags"] & 16:
                run.append(spans[index])
                index += 1
            run_text = "".join(span["text"] for span in run).strip()
            after = "".join(span["text"] for span in spans[index:]).lstrip()
            prefix = before.strip()
            marker = BOLD_MARKER.search(prefix)
            ordinal = (max(0, _marker_count(history + before, marker[1]) - 1) if marker else 0)
            before += "".join(span["text"] for span in run)
            if len(run_text) < 2:
                continue
            terminator, include = None, False
            if run_text[-1] in ":：":
                terminator, include = "colon", True
            elif run_text[-1] in "—–-":
                terminator, include = "dash", True
            elif run_text[-1] in ".。":
                terminator, include = "period", True
            elif after[:1] in ":：":
                terminator, include = "colon", False
            elif after[:1] in "—–":
                terminator, include = "dash", False
            marker = BOLD_MARKER.search(prefix)
            single_word = len(run_text) <= 20 and not re.search(r"\s", run_text)
            if marker:
                kind = "marker"
            elif line_index == 0 and not prefix and not terminator and single_word:
                kind = "lead_token"
            elif line_index == 0 and not prefix:
                kind = "lead"
            else:
                continue
            if kind != "lead_token" and not terminator:
                continue
            hints.append({"kind": kind, "marker": marker[1] if marker else "",
                          "ordinal": ordinal, "terminator": terminator, "include": include,
                          "limit": max(16, min(120, len(run_text) * 2 + 12))})
        history += "".join(span["text"] for span in spans)
    return hints


def _marker_matches(text, marker):
    """Find list markers only; decimals ("1.5") and equation labels ("(3).") do not count."""
    variants = {marker, marker.replace(")", "）"), marker.replace("(", "（"),
                marker.replace("(", "（").replace(")", "）")}
    alternatives = "|".join(re.escape(value) for value in sorted(variants, key=len, reverse=True))
    pattern = re.compile(r"(?<![\w])(" + alternatives + r")")
    matches = []
    for match in pattern.finditer(text):
        before = text[:match.start()]
        after = text[match.end():match.end() + 1]
        if before and not (before[-1].isspace() or before[-1] in ":;。；："):
            continue
        # A period followed by a digit is a decimal ("1.5"), not a list marker.
        if after.isdigit():
            continue
        matches.append(match)
    return matches


def _marker_count(text, marker):
    return len(_marker_matches(text, marker))


def _bold_end(text, start, terminator, include, limit):
    if start >= len(text):
        return None
    if terminator == "colon":
        found = [index for index in (text.find("：", start), text.find(":", start)) if index >= 0]
        if not found:
            return None
        index = min(found)
        end = index + 1 if include else index
    elif terminator == "dash":
        found = [(text.find(token, start), token) for token in ("——", "—", "–")]
        found = [(index, token) for index, token in found if index >= 0]
        if not found:
            return None
        index, token = min(found)
        end = index + len(token) if include else index
    elif terminator == "period":
        match = re.search(r"[。！？]|\.(?=\s|$)", text[start:start + limit + 1])
        if not match:
            return None
        end = start + match.end() if include else start + match.start()
    else:
        return None
    return end if start < end <= start + limit else None


def bold_ranges(text, hints):
    """Map source bold label hints to character ranges in the translation."""
    ranges = []
    for hint in hints:
        if hint["kind"] == "lead":
            start = len(text) - len(text.lstrip())
            end = _bold_end(text, start, hint["terminator"], hint["include"], hint["limit"])
            if end:
                ranges.append((start, end))
        elif hint["kind"] == "marker":
            matches = _marker_matches(text, hint["marker"])
            ordinal = hint.get("ordinal", 0)
            if ordinal >= len(matches):
                continue
            match = matches[ordinal]
            start = match.end()
            while start < len(text) and text[start].isspace():
                start += 1
            end = _bold_end(text, start, hint["terminator"], hint["include"], hint["limit"])
            if end:
                ranges.append((start, end))
        else:
            start = len(text) - len(text.lstrip())
            token = re.match(r"\S{1,12}", text[start:])
            if token:
                ranges.append((start, start + token.end()))
    return _merge_ranges(ranges)


def _merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _text_html(value, fallback):
    if not fallback:
        return html.escape(value).replace("\n", "<br>")
    return "".join("<br>" if char == "\n" else
                   '<span style="font-family:serif">' + html.escape(char) + '</span>'
                   if char in fallback else html.escape(char)
                   for char in value)


def _bold_html(value, start, end, ranges, fallback=()):
    pieces, position = [], start
    for first, last in ranges:
        first, last = max(first, start), min(last, end)
        if first >= last or last <= position:
            continue
        if first > position:
            pieces.append(_text_html(value[position:first], fallback))
        pieces.append("<b>" + _text_html(value[first:last], fallback) + "</b>")
        position = last
    if position < end:
        pieces.append(_text_html(value[position:end], fallback))
    return "".join(pieces)


_LIST_MARKERS = "•●▪▫▸▹►‣∙⋅※◦■□◆◇·・"
_LEAD_LIST_MARKER = re.compile(r"^(\s*)([" + re.escape(_LIST_MARKERS) + r"])")


def lead_list_marker(value):
    return _LEAD_LIST_MARKER.match(value or "")


def list_marker_style(block, style, size):
    """Match a translated list marker to the source glyph's size and weight.

    CJK text fonts often map U+2022 to a tiny raised dot, while the source's
    Latin font draws a full bullet. Keep the stored translation untouched and
    render only the leading marker in a Latin/symbol face at the source size.
    """
    translated = lead_list_marker(block.get("translation", ""))
    if translated is None:
        return block.get("translation", ""), None
    display = block.get("translation", "")
    source = lead_list_marker(block.get("text", ""))
    if source is not None and source.group(2) != translated.group(2):
        # A list marker is layout punctuation. Use the source symbol so the
        # translation keeps the document's original bullet shape.
        display = display[:translated.start(2)] + source.group(2) + display[translated.end(2):]
    span = next((item for item in style.get("spans", [])
                 if lead_list_marker(item.get("text", ""))), None)
    ratio, weight = 1.0, 400
    if span is not None:
        marker_size = float(span.get("size") or 0)
        body_size = float(style.get("size") or 0)
        if marker_size > 0 and body_size > 0:
            ratio = max(.6, min(1.8, marker_size / body_size))
        weight = 600 if span.get("flags", 0) & 16 else 400
    return display, {"family": "'Times New Roman',serif",
                     "size": size * ratio, "weight": weight}


def inline_math_markup(value, assets, archive, scale=1, bold=(), fallback=(), marker_style=None, links=()):
    marker = lead_list_marker(value) if marker_style is not None else None
    if marker is not None:
        first, last = marker.start(2), marker.end(2)
        prefix, suffix = value[:first], value[last:]
        gap = ""
        if suffix[:1] in {" ", "\t"}:
            # A leading space would collapse once the marker becomes its own
            # inline box; keep the source's visible gap after the bullet.
            gap = "&nbsp;"
            suffix = suffix[1:]
        # Bold ranges address the whole string; shift them past the marker
        # and the space it replaced, which are drawn separately.
        removed = last + (1 if gap else 0)
        shifted = []
        for start, end in bold:
            start, end = max(0, start - removed), end - removed
            if end > 0 and start < end:
                shifted.append((max(0, start), end))
        contents, plain, labels, resolved = inline_math_markup(
            suffix, assets, archive, scale, shifted, fallback, links=links)
        # Resolved ranges address the suffix; map them back behind the marker.
        resolved = [dict(item, start=item["start"] + removed, end=item["end"] + removed)
                    for item in resolved]
        marker_html = (f'<span style="font-family:{marker_style["family"]};'
                       f'font-size:{marker_style["size"]:.2f}pt;'
                       f'font-weight:{marker_style["weight"]};line-height:0">'
                       f'{html.escape(value[first:last])}</span>')
        return (_text_html(prefix, fallback) + marker_html + gap + contents,
                prefix + value[first:last] + (" " if gap else "") + plain, labels, resolved)
    normalized, mapping = [], []
    for index, char in enumerate(value):
        for item in normalized_inline_math(char):
            normalized.append(item)
            mapping.append(index)
    text = "".join(normalized)
    matches = []
    # Match complete expressions before their individual variables. Translation
    # can change the order of clauses, so source order must not consume a prefix
    # of a longer expression or prevent a later variable from being preserved.
    for asset in sorted(assets, key=lambda item: -len(item["normalized"])):
        needle = asset["normalized"]
        index = text.find(needle)
        while index >= 0:
            before = text[index - 1:index]
            after = text[index + len(needle):index + len(needle) + 1]
            occupied = any(index < end and index + len(needle) > start for start, end, _ in matches)
            if not (occupied or (needle[0].isascii() and needle[0].isalnum() and before.isascii() and before.isalnum())
                    or (needle[-1].isascii() and needle[-1].isalnum() and after.isascii() and after.isalnum())):
                break
            index = text.find(needle, index + 1)
        if index < 0:
            continue
        matches.append((index, index + len(needle), asset))
    # Match coordinates are normalized-string indices; map them back to the
    # displayed text before cutting bold ranges, labels and link anchors.
    placed = [(mapping[start], mapping[end - 1] + 1, asset) for start, end, asset in matches]
    ranges = _link_ranges(value, text, mapping, placed, links)
    events = [(start, end, "math", asset) for start, end, asset in placed]
    events += [(item["start"], item["end"], "link", item) for item in ranges]
    events.sort(key=lambda event: event[0])
    pieces, plain, labels, resolved, raw_cursor = [], [], [], [], 0
    for start, end, kind, payload in events:
        if start < raw_cursor:
            continue
        pieces.append(_bold_html(value, raw_cursor, start, bold, fallback))
        plain.append(value[raw_cursor:start])
        if kind == "math":
            name = "math-" + hashlib.sha256(payload["svg"]).hexdigest()[:20] + ".svg"
            archive.add((payload["svg"], name))
            pieces.append(f'<img src="{name}" style="width:{payload["width"] * scale}pt;'
                          f'height:{payload["height"] * scale}pt;vertical-align:{-payload["descent"] * scale}pt">')
            labels.append(value[start:end])
        else:
            # Goto anchors carry no href; the reader overlay navigates, and the
            # markup only marks the text so its rendered position can be found.
            inner = _bold_html(value, start, end, bold, fallback)
            if payload.get("color"):
                # Publishers style links black, blue or gray. Keep that color
                # instead of MuPDF's default HTML link blue.
                inner = f'<span style="color:{payload["color"]}">{inner}</span>'
            if payload["kind"] == "uri" and payload.get("uri"):
                pieces.append('<a href="' + html.escape(payload["uri"], quote=True) + '">'
                              + inner + "</a>")
            else:
                pieces.append("<a>" + inner + "</a>")
            plain.append(value[start:end])
            resolved.append(dict(payload))
        raw_cursor = end
    pieces.append(_bold_html(value, raw_cursor, len(value), bold, fallback))
    plain.append(value[raw_cursor:])
    return "".join(pieces), "".join(plain), labels, resolved


def _map_overlay_point(point, clip, destination):
    return pymupdf.Point(destination.x0 + (point.x - clip.x0) * destination.width / clip.width,
                         destination.y0 + (point.y - clip.y0) * destination.height / clip.height)


def _map_overlay_rect(rect, clip, destination):
    first = _map_overlay_point(rect.tl, clip, destination)
    last = _map_overlay_point(rect.br, clip, destination)
    return pymupdf.Rect(first.x, first.y, last.x, last.y)


def _draw_inline_math_fragment(page, source_page, run, scale, match, clip, placement):
    source_box = (pymupdf.Rect(run["rect"]) + (-.12, -.12, .12, .12)) & source_page.rect
    target_box = _map_overlay_rect(match["box"], clip, placement)
    if source_box.is_empty or target_box.is_empty or source_box.width <= 0 or target_box.width <= 0:
        return False
    scale_x = target_box.width / source_box.width
    scale_y = min(float(scale or 1) * placement.height / clip.height,
                   target_box.height / source_box.height)
    if not .35 <= scale_x <= 2.5 or not .35 <= scale_y <= 1.5:
        return False
    height = source_box.height * scale_y
    top = target_box.y0 + (target_box.height - height) / 2
    destination = pymupdf.Rect(target_box.x0, top, target_box.x1, top + height)
    if destination.is_empty or not page.rect.contains(destination):
        return False
    pixmap = source_page.get_pixmap(matrix=pymupdf.Matrix(6, 6), clip=source_box,
                                    alpha=False, annots=False)
    if pixmap.width <= 0 or pixmap.height <= 0:
        return False
    cover = (target_box + (-.15, -.15, .15, .15)) & page.rect
    page.draw_rect(cover, color=None, fill=(1, 1, 1), overlay=True)
    page.insert_image(destination, stream=pixmap.tobytes("png"), overlay=True,
                      keep_proportion=False)
    return True


def _split_inline_math_run(run, separators):
    parts, current, pending = [], [], []

    def flush():
        nonlocal current
        part = _inline_math_run(current, True)
        if part:
            parts.append(part)
        current = []

    for char in run["chars"]:
        value = char.get("c", "")
        if value.isspace():
            flush()
        elif value in separators:
            if current:
                current.append(char)
                flush()
            else:
                pending.append(char)
        else:
            if pending:
                current.extend(pending)
                pending = []
            current.append(char)
    if pending:
        current.extend(pending)
    flush()
    return parts


def _preserve_inline_math(page, source_page, overlays, result):
    target_page = overlays[result["overlay"]]
    text, mapping, chars = _normalized_page_chars(target_page)
    if not text:
        return 0
    clip = pymupdf.Rect(result["clip"])
    placement = pymupdf.Rect(result["rect"])
    if clip.is_empty or placement.is_empty:
        return 0
    separators = " \t\r\n,，;；=:[\\](){}×→∈+-−–‑‐<>≤≥*/·|∧∨∩∪≈≠∼∝"

    cursor = preserved = 0
    for run in result.get("inline_math_runs", []):
        for part in _split_inline_math_run(run, separators) or [run]:
            match = _inline_math_match(text, part["normalized"], cursor, mapping, chars)
            if not match:
                continue
            cursor = match["end"]
            if not match["multiline"] and _draw_inline_math_fragment(
                    page, source_page, part, result.get("scale", 1), match, clip, placement):
                preserved += 1
    return preserved


def should_preserve_inline_symbols(block):
    text = (block.get("text") or "").strip()
    translation = block.get("translation") or ""
    kind = (block.get("kind") or "").lower()
    if any(label in kind for label in
           ("figure", "caption", "table", "chart", "reference", "formula", "equation", "display", "algorithm")):
        return False
    if re.match(r"^\s*(?:fig(?:ure)?|table|algorithm)\b", text, re.I):
        return False
    if re.match(r"^\s*(?:图|表)\s*\d", translation):
        return False
    return sum("\u3400" <= char <= "\u9fff" for char in translation) >= 6


_COMPAT_FALLBACK_RANGES = ((0x00B2, 0x00B3), (0x00B9, 0x00B9), (0x1D400, 0x1D7FF), (0x2070, 0x209C), (0x1D2C, 0x1D6A),
                           (0x2160, 0x2188), (0x2460, 0x24FF), (0xFB00, 0xFB4F))


def printable_text(value):
    """Use compatibility equivalents only for glyphs the CJK fallback often lacks.

    Mathematical alphanumerics, superscripts/subscripts and ligatures become
    ordinary printable characters (for example ²⁷ -> 27 and 𝐿 -> L), while
    normal CJK punctuation stays untouched.
    """
    chars = []
    for char in value or "":
        if any(start <= ord(char) <= end for start, end in _COMPAT_FALLBACK_RANGES):
            chars.append(unicodedata.normalize("NFKC", char))
        else:
            chars.append(char)
    return "".join(chars)


def compact_punctuation(value):
    """Half-width punctuation fallback for narrow figure/table labels."""
    return (value or "").translate({ord("（"): "(", ord("）"): ")", ord("［"): "[",
                                     ord("］"): "]", ord("｛"): "{", ord("｝"): "}",
                                     ord("："): ":", ord("；"): ";", ord("，"): ","})


def unchanged_non_text(block):
    """Numbers, ticks, units and symbol fragments already read correctly."""
    source = block.get("text") or ""
    if not normalized_text(source) or normalized_text(source) != normalized_text(block.get("translation")):
        return False
    return not is_prose(source) and len(re.findall(r"\b[A-Za-z]{2,}\b", source)) <= 3


def span_strip(span):
    # A strip through the glyph interior removes the source text without touching
    # the bounding-box padding of an adjacent equation or its subscripts.
    box = pymupdf.Rect(span["bbox"])
    if (span.get("descender") == 0 and span.get("origin")
            and 0 < span.get("ascender", 0) < .7):
        center = span["origin"][1] - span["size"] * span["ascender"] * .5
        return pymupdf.Rect(box.x0 + .02, center - .3, box.x1 - .02, center + .3)
    inset = max(0, box.height / 2 - min(.5, box.height * .15))
    return pymupdf.Rect(box.x0 + min(.05, box.width * .1), box.y0 + inset,
                        box.x1 - min(.05, box.width * .1), box.y1 - inset)


_FLOW_CAPTION = re.compile(r"^\s*(?:fig(?:ure)?|table|algorithm)\b", re.I)
_FLOW_LIST = re.compile(r"^\s*(?:[•·●▪◆]|\(?\d{1,2}[.)]|[A-Za-z][.)])\s+")


def _flow_lines(native, rect):
    lines = []
    for block in native:
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            box = pymupdf.Rect(line["bbox"])
            if rect.contains((box.tl + box.br) * .5):
                lines.append(line)
    return sorted(lines, key=lambda line: (round(line["bbox"][1], 2), line["bbox"][0]))


def small_caps_nominal_size(lines, size):
    """True small caps report the reduced glyph size; translations need the nominal size.

    Small-caps glyphs are uppercase letters at 70–85% of the nominal size in
    the same font, set directly beside full-cap letters. Measuring such a
    heading from its dominant reduced spans would typeset it below body size.
    """
    if size <= 0:
        return None
    for line in lines:
        ordered = [s for s in line.get("spans", []) if s.get("text", "").strip()]
        for left, right in zip(ordered, ordered[1:]):
            if round(left["size"], 1) == round(right["size"], 1):
                continue
            small, large = (left, right) if round(left["size"], 1) == size else (right, left)
            if (round(small["size"], 1) != size or not re.search(r"[A-Z]", small["text"])
                    or not re.search(r"[A-Z]", large["text"]) or small["font"] != large["font"]):
                continue
            candidate = round(large["size"], 1)
            if not size * 1.15 <= candidate <= size * 1.5:
                continue
            gap = pymupdf.Rect(right["bbox"]).x0 - pymupdf.Rect(left["bbox"]).x1
            if -1 <= gap <= size * 1.2:
                return candidate
    return None


def _flow_size(lines):
    sizes = Counter(round(span["size"], 1) for line in lines
                    for span in line.get("spans", []) if span.get("text", "").strip())
    if not sizes:
        return 0
    size = sizes.most_common(1)[0][0]
    return small_caps_nominal_size(lines, size) or size


def page_body_size(native):
    """Character-weighted body size of one page, used to recognize display type.

    Titles, author lines and body text can share a page; only text clearly above
    the dominant body size is treated as display type. Math/font glyphs and
    empty spans do not vote, so formula-heavy pages do not skew the reference.
    """
    weights = Counter()
    for block in native:
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = (span.get("text") or "").strip()
                size = float(span.get("size") or 0)
                if text and size >= 4 and not MATH_FONT.search(span.get("font", "")):
                    weights[round(size, 1)] += len(text)
    return weights.most_common(1)[0][0] if weights else 0


def _narrative(text):
    """Accept a short continuation line that is prose but too brief for is_prose."""
    words = re.findall(r"\b[A-Za-z]{2,}\b", text or "")
    stops = re.findall(r"\b(?:the|is|of|and|to|in|with|for|can|are|this|that|from|where|which)\b",
                       text or "", re.I)
    return is_prose(text) or (len(words) >= 4 and len(stops) >= 1)


def _flow_join(left, right):
    """Join translated fragments without leaving a space between CJK glyphs."""
    left, right = (left or "").strip(), (right or "").strip()
    if not left:
        return right
    if not right:
        return left
    if (re.search(r"[\u3400-\u9fff。，；：！？、”’）》】]$", left)
            and re.match(r"^[\u3400-\u9fff（“‘《【]", right)):
        return left + right
    return left + " " + right


def merge_continued_paragraphs(page, blocks, native, target, cover_page=False):
    """Flow a paragraph that extraction split into adjacent text blocks.

    Extractors often cut a paragraph after a line ending in inline math or a
    display formula. Typesetting both halves independently leaves the first
    line ragged and restarts the continuation at the margin even though the
    source is one justified paragraph. Join only when the geometry proves a
    continuation: same column and left edge, a full-width last line above,
    no first-line indent below, a line-sized gap, matching type size, and
    nothing (text, image or rule) in between. References, captions, headings
    and special blocks never join — except the wrapped lines of one numbered
    heading, which `heading_joinable` handles.
    """
    count = len(blocks)
    if count < 2:
        return blocks
    rects = [source_rect(page, block) for block in blocks]
    lines = [_flow_lines(native, rect) for rect in rects]
    sizes = [_flow_size(item) for item in lines]
    body_size = page_body_size(native) if cover_page else 0
    rotation = page.rotation
    if rotation:
        page.set_rotation(0)
    try:
        graphics = [pymupdf.Rect(image["bbox"]) for image in page.get_image_info()]
        graphics += [pymupdf.Rect(drawing["rect"]) for drawing in visible_drawings(page)]
    finally:
        if rotation:
            page.set_rotation(rotation)
    parents = list(range(count))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def special(block):
        kind = (block.get("kind") or "").lower()
        return any(label in kind for label in
                   ("reference", "formula", "equation", "table", "figure", "caption",
                    "title", "heading", "cover", "algorithm"))

    def joinable(upper, lower):
        first, second = blocks[upper], blocks[lower]
        if special(first) or special(second):
            return False
        if not (first.get("translation") and first.get("target") == target
                and second.get("translation") and second.get("target") == target):
            return False
        if not _narrative(first["text"]) or not is_prose(second["text"]):
            return False
        for block in (first, second):
            value = (block.get("text") or "").strip()
            if section_heading(value) or TABLE_CAPTION.match(value) or _FLOW_CAPTION.match(value):
                return False
        if _FLOW_LIST.match(second["text"].strip()):
            return False
        # Never flow two bibliography entries into one paragraph fragment.
        if re.match(r"^\s*\[\d{1,4}\]", second["text"].strip()):
            return False
        a, b = rects[upper], rects[lower]
        size = max(sizes[upper], sizes[lower])
        gap = b.y0 - a.y1
        if not -1 <= gap <= max(3.0, size * .4):
            return False
        horizontal = min(a.x1, b.x1) - max(a.x0, b.x0)
        if horizontal < min(a.width, b.width) * .85:
            return False
        if abs(a.x0 - b.x0) > 3 or abs(a.x1 - b.x1) > 3:
            return False
        if abs(sizes[upper] - sizes[lower]) > max(.6, size * .08):
            return False
        upper_lines, lower_lines = lines[upper], lines[lower]
        if not upper_lines or not lower_lines:
            return False
        last = pymupdf.Rect(upper_lines[-1]["bbox"])
        first_line = pymupdf.Rect(lower_lines[0]["bbox"])
        # A full last line above and an unindented first line below rule out
        # paragraph spacing that merely looks like a line gap.
        if a.x1 - last.x1 > max(2.0, size * .35):
            return False
        if first_line.x0 - b.x0 > 3 or abs(first_line.x0 - a.x0) > 3:
            return False
        band = pymupdf.Rect(a.x0, a.y1 + .2, max(a.x1, b.x1), b.y0 - .2)
        return band_clear(band, upper, lower)

    def band_clear(band, upper, lower):
        if band.height <= 0:
            return True
        if any(overlap(band, box) > .1 for box in graphics):
            return False
        if any(overlap(band, rects[index]) > .1
               for index in range(count) if index not in (upper, lower)
               and root(index) not in {root(upper), root(lower)}):
            return False
        # The joined blocks' own lines share the band through font ascent and
        # descent padding; only a line belonging to neither block blocks the join.
        own = [pymupdf.Rect(rects[upper]), pymupdf.Rect(rects[lower])]
        return not any(overlap(band, pymupdf.Rect(line["bbox"])) > .1
                       for block in native if block.get("type", 0) == 0
                       for line in block.get("lines", [])
                       if not any(overlap(pymupdf.Rect(line["bbox"]), box)
                                  >= pymupdf.Rect(line["bbox"]).get_area() * .5 for box in own))

    def heading_joinable(upper, lower):
        """Join the wrapped lines of one numbered heading split across blocks.

        Extraction keeps each centered heading line as its own block, and one
        line alone has no leading to spare — its translation is forced below
        body size. Only an unnumbered non-prose line joins a numbered heading
        above it; another numbered heading, prose, captions, lists, labels and
        formulas stay separate structures.
        """
        first, second = blocks[upper], blocks[lower]
        if special(first) or special(second):
            return False
        if not (first.get("translation") and first.get("target") == target
                and second.get("translation") and second.get("target") == target):
            return False
        if not section_heading(blocks[root(upper)]["text"]):
            return False
        value = (second.get("text") or "").strip()
        if (not value or section_heading(value) or is_prose(second["text"])
                or _FLOW_CAPTION.match(value) or TABLE_CAPTION.match(value)
                or _FLOW_LIST.match(value) or re.match(r"^\s*\[\d{1,4}\]", value)
                or re.fullmatch(r"[\s\d.,()%:：、。·-]+", value)
                or not re.search(r"[A-Za-z]{2,}|[\u3400-\u9fff]{2,}", value)):
            return False
        if cover_page and min(rects[upper].y0, rects[lower].y0) < page.rect.height * .22:
            return False  # Leave cover title groups to the cover pre-layout.
        if any(MATH_FONT.search(span["font"]) for line in lines[lower]
               for span in line.get("spans", []) if span.get("text", "").strip()):
            return False
        a, b = rects[upper], rects[lower]
        size = max(sizes[upper], sizes[lower])
        if not -1 <= b.y0 - a.y1 <= max(3.0, size * .6):
            return False
        if min(a.x1, b.x1) - max(a.x0, b.x0) < min(a.width, b.width) * .85:
            return False
        if abs(sizes[upper] - sizes[lower]) > max(.6, size * .08):
            return False
        band = pymupdf.Rect(a.x0, a.y1 + .2, max(a.x1, b.x1), b.y0 - .2)
        return band_clear(band, upper, lower)

    def author_display(value, size):
        """Author, affiliation and footnote lines never join a title group."""
        if "@" in value or re.search(r"[∗†‡]", value) or INSTITUTION.search(value):
            return True
        # Author lists use display-sized capitals at author sizes. Titles are
        # larger than this band, so their own capitalized words are unaffected.
        return (size <= 14 and not is_prose(value)
                and len(re.findall(r"\b[A-Z][a-z]+\b", value)) >= 2
                and bool(re.search(r",|\band\b", value)))

    def display_title_joinable(upper, lower):
        """Join the visual lines of one cover title that extraction split apart.

        A title is one typographic object: setting each extracted line on its
        own lets the first line shrink while the second keeps the source size,
        and lets only the wider line qualify for title handling. Two same-size
        display lines directly above each other on the cover page, aligned on
        one axis and with nothing in the gap, form one title group. Subtitles
        of another size, authors, affiliations, captions, equations, lists and
        prose stay separate. Rotated covers keep the conservative per-line path.
        """
        if not cover_page or page.rotation or not body_size:
            return False
        first, second = blocks[upper], blocks[lower]
        if special(first) or special(second):
            return False
        if not (first.get("translation") and first.get("target") == target
                and second.get("translation") and second.get("target") == target):
            return False
        a, b = rects[upper], rects[lower]
        size = max(sizes[upper], sizes[lower])
        if size < max(6.0, body_size * 1.25):
            return False
        if a.y0 > page.rect.height * .45:
            return False
        if abs(sizes[upper] - sizes[lower]) > max(.8, size * .08):
            return False
        for block in (first, second):
            value = (block.get("text") or "").strip()
            if not value or not re.search(r"[A-Za-z\u3400-\u9fff]{2,}", value):
                return False
            if author_display(value, size):
                return False
            if (section_heading(value) or TABLE_CAPTION.match(value)
                    or _FLOW_CAPTION.match(value) or _FLOW_LIST.match(value)
                    or re.match(r"^\s*\[\d{1,4}\]", value)):
                return False
        gap = b.y0 - a.y1
        if not -1 <= gap <= max(4.0, size * .8):
            return False
        if min(a.x1, b.x1) - max(a.x0, b.x0) <= 0:
            return False
        tolerance = max(2.0, size * .5)
        axis_aligned = abs((a.x0 + a.x1) / 2 - (b.x0 + b.x1) / 2) <= tolerance
        edge_aligned = abs(a.x0 - b.x0) <= 3 or abs(a.x1 - b.x1) <= 3
        if not (axis_aligned or edge_aligned):
            return False
        band = pymupdf.Rect(a.x0, a.y1 + .2, max(a.x1, b.x1), b.y0 - .2)
        return band_clear(band, upper, lower)

    for lower in range(count):
        # Only the nearest block above in the same horizontal band can be the
        # previous half; skipping past an intervening fragment would merge
        # across unrelated content.
        candidates = [upper for upper in range(count) if upper != lower
                      and rects[upper].y1 <= rects[lower].y0 + 1
                      and min(rects[upper].x1, rects[lower].x1)
                      - max(rects[upper].x0, rects[lower].x0) > 0]
        candidates.sort(key=lambda index: rects[index].y1, reverse=True)
        if not candidates:
            continue
        upper = candidates[0]
        if root(upper) != root(lower) and (joinable(upper, lower) or heading_joinable(upper, lower)
                                           or display_title_joinable(upper, lower)):
            parents[root(lower)] = root(upper)

    groups, order = {}, []
    for index in range(count):
        key = root(index)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(index)
    merged = []
    for key in order:
        members = sorted(groups[key], key=lambda index: (rects[index].y0, rects[index].x0))
        if len(members) == 1:
            merged.append(blocks[members[0]])
            continue
        region = pymupdf.Rect(rects[members[0]])
        for index in members[1:]:
            region |= rects[index]
        displayed = region * page.rotation_matrix
        bounds = page.rect
        translation = ""
        for index in members:
            translation = _flow_join(translation, blocks[index].get("translation"))
        identifiers = [key for index in members
                       for key in blocks[index].get("_members", [blocks[index]["id"]])]
        merged.append({
            **blocks[members[0]],
            "text": " ".join((blocks[index].get("text") or "").strip() for index in members).strip(),
            "translation": translation,
            "bbox": [displayed.x0 / bounds.width, displayed.y0 / bounds.height,
                     displayed.x1 / bounds.width, displayed.y1 / bounds.height],
            "_members": identifiers,
        })
    return merged


def layout_blocks(page, saved_page, native, target, cover_page=False):
    """Join overlapping extraction fragments of prose with inline mathematics.

    Source translations and IDs stay intact; this grouping is only for typesetting.
    Display equations and unrelated overlapping regions retain the safety checks.
    """
    blocks = []
    for original in saved_page["blocks"]:
        block = original
        if block.get("drop_cap"):
            block = {**block, "_members": [block["id"]]}
        # Equation numbers are sometimes extracted into the following paragraph.
        # Leave those labels on the source page and typeset only the explanation.
        if re.match(r"^\s*\(\d+\)\s*\n", block["text"]) and is_prose(block["text"]):
            rect = source_rect(page, block)
            best = max(native, key=lambda b: overlap(rect, pymupdf.Rect(b["bbox"])), default=None)
            lines = [line for line in best["lines"] if not re.fullmatch(r"\s*\(\d+\)\s*", "".join(s["text"] for s in line["spans"]))] if best else []
            if lines:
                region = pymupdf.Rect(lines[0]["bbox"])
                for line in lines[1:]:
                    region |= pymupdf.Rect(line["bbox"])
                displayed = region * page.rotation_matrix
                bounds = page.rect
                block = {**block, "text": re.sub(r"^\s*\(\d+\)\s*\n", "", block["text"]),
                         "translation": re.sub(r"^\s*[（(]\d+[）)]\s*", "", block["translation"]),
                         "bbox": [displayed.x0 / bounds.width, displayed.y0 / bounds.height,
                                  displayed.x1 / bounds.width, displayed.y1 / bounds.height]}
        blocks.append(block)
    rects = [source_rect(page, b) for b in blocks]
    remaining = set(range(len(blocks)))
    result = []
    while remaining:
        first = min(remaining)
        component = {first}
        frontier = [first]
        remaining.remove(first)
        while frontier:
            index = frontier.pop()
            neighbors = {i for i in remaining if narrative_fragment(blocks[index]["text"])
                         and narrative_fragment(blocks[i]["text"]) and overlap(rects[index], rects[i]) > 1}
            remaining.difference_update(neighbors)
            component.update(neighbors)
            frontier.extend(neighbors)
        members = [blocks[i] for i in sorted(component)]
        region = pymupdf.Rect(rects[first])
        for i in component:
            region |= rects[i]
        spans = [s for b in native if overlap(region, pymupdf.Rect(b["bbox"])) > .5
                 for line in b["lines"] for s in line["spans"]]
        if (len(members) > 1 and is_prose(" ".join(b["text"] for b in members))
                and all(b.get("translation") and b.get("target") == target for b in members)
                and any(MATH_FONT.search(s["font"]) for s in spans)
                and region.width <= max(rects[i].width for i in component) * 1.15):
            bounds = page.rect
            displayed = region * page.rotation_matrix
            result.append({**members[0], "text": " ".join(b["text"] for b in members),
                           "translation": " ".join(b["translation"] for b in members),
                           "bbox": [displayed.x0 / bounds.width, displayed.y0 / bounds.height,
                                    displayed.x1 / bounds.width, displayed.y1 / bounds.height],
                           "_members": [b["id"] for b in members]})
        else:
            result.extend(members)
    return merge_continued_paragraphs(page, result, native, target, cover_page)


def inspect_block(native, rect, combine=False):
    candidates = [b for b in native if overlap(rect, pymupdf.Rect(b["bbox"])) > .5]
    if not candidates:
        return None, "未找到可靠的原文样式"
    best = max(candidates, key=lambda b: overlap(rect, pymupdf.Rect(b["bbox"])))
    if combine:
        best = {"lines": [line for block in candidates
                          for line in block["lines"]]}
    best = {"lines": [line for line in best["lines"]
                      if overlap(rect, pymupdf.Rect(line["bbox"])) >= pymupdf.Rect(line["bbox"]).get_area() * .5]}
    spans = [s for line in best["lines"] for s in line["spans"] if s["text"].strip()]
    if not spans:
        return None, "原文没有可用字体信息"
    text = " ".join(s["text"] for s in spans)
    ordinary_letters = sum(len(re.findall(r"[A-Za-z]", s["text"])) for s in spans if not MATH_FONT.search(s["font"]))
    math_characters = sum(len(s["text"].strip()) for s in spans if MATH_FONT.search(s["font"]))
    heading = ordinary_letters >= 12 and ordinary_letters > math_characters * 3
    if not is_prose(text) and not heading and (any(MATH_FONT.search(s["font"]) for s in spans) or re.search(r"[∑∫√∏∂∇]|\b\w\s*[=≤≥]\s*\w", text)):
        return None, "公式或公式混排区域保留原文"
    if any(abs(line.get("dir", (1, 0))[0] - 1) > .01 or abs(line.get("dir", (1, 0))[1]) > .01 for line in best["lines"]):
        return None, "竖排或倾斜文字保留原文"
    # Multiple cells / columns inside one extraction block must not become one paragraph.
    lines = best["lines"]
    cell_rows = set()
    gap_limit = max(10, max(s["size"] for s in spans) * 1.5)
    for line in lines:
        a = pymupdf.Rect(line["bbox"])
        # Compare adjacent fragments, not arbitrary non-adjacent words on the
        # same justified line (which previously looked like separate cells).
        right = [pymupdf.Rect(other["bbox"]) for other in lines
                 if abs(a.y0 - other["bbox"][1]) < 2 and other["bbox"][0] >= a.x1]
        if right and min(b.x0 for b in right) - a.x1 > gap_limit:
            cell_rows.add(round(a.y0 / 3))
    # One split baseline is common in section numbers and figure captions.
    # Tables have multiple rows of separated cells. Inline math prose is grouped above.
    if len(cell_rows) >= 2 and not combine:
        return None, "包含多个单元格的文本块保留原文"
    weights = Counter()
    colors = Counter()
    for span in spans:
        weights[round(span["size"], 1)] += len(span["text"])
        colors[span["color"]] += len(span["text"])
    size = weights.most_common(1)[0][0]
    if size <= 0:
        return None, "无效字号"
    size = small_caps_nominal_size(lines, size) or size
    # A long bold label or vector name must not determine a whole paragraph's
    # weight when ordinary body text was extracted into many short spans.
    emphasis = [s for s in spans if not MATH_FONT.search(s["font"])
                and not (s["font"].upper().startswith("CMBX") and len(s["text"].strip()) == 1)]
    total = sum(len(s["text"].strip()) for s in emphasis)
    bold = sum(len(s["text"].strip()) for s in emphasis if s["flags"] & 16)
    italic = sum(len(s["text"].strip()) for s in emphasis if s["flags"] & 2)
    return {"size": size, "color": colors.most_common(1)[0][0],
            "bold": bool(total and bold >= total * .75),
            "italic": bool(total and italic >= total * .75), "lines": lines,
            "spans": spans}, None


def cover_text_role(block, page, rect, style):
    """Recognize centered title and author lines on a paper cover page."""
    if block.get("_source_page", page.number) != 0 or rect.y0 >= page.rect.height * .22 or len(style["lines"]) > 3:
        return None
    if abs((rect.x0 + rect.x1) / 2 - page.rect.width / 2) > 18:
        return None
    if (style["bold"] and style["size"] >= 14 and rect.width >= page.rect.width * .4
            and len(block.get("text", "")) <= 140 and len(style["lines"]) <= 2):
        return "title"
    if (not is_prose(block.get("text", ""))
            and len(re.findall(r"\b[A-Z][a-z]+\b", block.get("text", ""))) >= 2
            and re.search(r",|\band\b", block.get("text", ""))
            and len(style["lines"]) <= 3):
        return "authors"
    return None


def prepare_cover_metadata(page, working, rects, native, overlays, typography, target):
    """Preflight the author/affiliation/contact cluster as one layout transaction."""
    body_start = re.compile(r"^\s*(?:abstract\b|keywords?\b|\d+\s+introduction\b|摘要|关键词)", re.I)
    candidates = []
    for block in working:
        rect = rects[block["id"]]
        if rect.y0 > page.rect.height * .5 or rect.width > page.rect.width * .92:
            continue
        if abs((rect.x0 + rect.x1) / 2 - page.rect.width / 2) > page.rect.width * .12:
            continue
        style, reason = inspect_block(native, rect, combine="_members" in block)
        if style and not reason:
            candidates.append((block, style, rect))
    candidates.sort(key=lambda item: (item[2].y0, item[2].x0))
    title_bottom = max((rect.y1 for _, style, rect in candidates if style["size"] >= 14), default=0)
    seed = next((i for i, (block, style, rect) in enumerate(candidates)
                 if rect.y0 >= title_bottom and 7 <= style["size"] <= 13.5 and not is_prose(block["text"])
                 and not INSTITUTION.search(block["text"]) and "@" not in block["text"]
                 and len(re.findall(r"\b[A-Z][a-z]+\b", block["text"])) >= 2
                 and (re.search(r",|\band\b", block["text"])
                      or (len(re.findall(r"\b[A-Z][a-z]+\b", block["text"])) >= 3
                          and re.search(r"[A-Za-z][\d¹²³]", block["text"])))), None)
    if seed is None:
        return None, [], ""
    group = [candidates[seed]]
    for item in candidates[seed + 1:]:
        block, style, rect = item
        previous = group[-1][2]
        if (rect.y0 - previous.y1 > max(18, group[0][1]["size"] * 2.2)
                or body_start.search(block["text"]) or style["size"] > group[0][1]["size"] * 1.25
                or (is_prose(block["text"]) and not INSTITUTION.search(block["text"]) and "@" not in block["text"])):
            break
        group.append(item)
    members = [block for block, _, _ in group]
    if any(not b.get("translation") or b.get("target") != target for b in members):
        return None, members, "作者、单位和邮箱尚未全部翻译，暂保留完整原文信息区"
    region = pymupdf.Rect(group[0][2])
    for _, _, rect in group[1:]:
        region |= rect
    own_spans = {id(span) for _, style, _ in group for span in style["spans"]}
    spans = [span for item in native for line in item["lines"] for span in line["spans"]
             if span["text"].strip()]
    foreign = [span for span in spans if id(span) not in own_spans]
    following = [pymupdf.Rect(span["bbox"]).y0 for span in foreign
                 if pymupdf.Rect(span["bbox"]).y0 > region.y1 - 1
                 and min(pymupdf.Rect(span["bbox"]).x1, region.x1) - max(pymupdf.Rect(span["bbox"]).x0, region.x0) > 1]
    bottom = min(following, default=min(page.rect.height * .55, region.y1 + 48)) - 6
    title_width = max((rect.width for block, style, rect in candidates[:seed]
                       if style["size"] >= 14), default=region.width)
    width = min(page.rect.width * .84, max(region.width, title_width))
    center = page.rect.width / 2
    graphics = [pymupdf.Rect(image["bbox"]) for image in page.get_image_info()]
    graphics += [d["rect"] + (-.25, -.25, .25, .25) for d in visible_drawings(page)]
    below_graphics = [item.y0 - 2 for item in graphics if item.y0 >= region.y1 - .5
                      and min(item.x1, center + width / 2) - max(item.x0, center - width / 2) > 1]
    bottom = min([bottom] + below_graphics)
    box = pymupdf.Rect(center - width / 2, region.y0, center + width / 2, bottom)
    if (box.height < 10 or any(overlap(box, obstacle) > .1 for obstacle in graphics)
            or any(overlap(box, pymupdf.Rect(span["bbox"])) > .1 for span in foreign)):
        return None, members, "作者信息区含图形或相邻文字，暂保留完整原文以避免重叠"
    markup = []
    anchors = []
    email = re.compile(r"(\{[^{}]+\}@[\w.\-]+|[\w.+\-]+@[\w.\-]+)")
    for block, style, _ in group:
        text = printable_text(block["translation"])
        is_author = "@" not in block["text"] and not INSTITUTION.search(block["text"])
        pieces = [text.replace("\n", " ")] if is_author else email.split(text)
        for piece in pieces:
            if not piece.strip():
                continue
            contact = bool(email.fullmatch(piece.strip()))
            role = "author" if is_author else "email" if contact else "affiliation"
            content = html.escape(piece.strip()).replace("\n", "<br>")
            if contact:
                content = ('<a href="mailto:' + html.escape(piece.strip(), quote=True)
                           + '">' + content + "</a>")
                anchors.append({"text": piece.strip(), "kind": "uri", "uri": "mailto:" + piece.strip(),
                                "ordinal": sum(1 for item in anchors if item["text"] == piece.strip())})
            if is_author:
                content = AUTHOR_MARKS.sub(r"<sup>\g<marks></sup>", content)
            elif not contact:
                content = re.sub(r"(?<![\w])(\d{1,2})(?=\s+[\w\u3400-\u9fff])", r"<sup>\1</sup>", content)
            size = style["size"]
            markup.append((role, size, style["bold"], content))
    expected = Counter(c for b in members for c in unicodedata.normalize("NFKC", b["translation"]) if not c.isspace())
    for factor in (1, .96, .92, .88, .84):
        scratch = overlays.new_page(width=page.rect.width, height=page.rect.height)
        contents = []
        for role, size, bold, content in markup:
            family = "monospace" if role == "email" else typography["family"]
            contents.append(f'<div style="font-family:{family};font-size:{size * factor}pt;'
                            f'font-weight:{600 if bold else 400};line-height:{1.28 if role == "email" else 1.5};margin:0 0 1.5pt">{content}</div>')
        css = typography["css"] + ("body{margin:0;padding:0;text-align:center}"
                                   "a:link,a:visited,a:hover{color:inherit;text-decoration:none}"
                                   "sup{font-size:65%;line-height:0}")
        spare, scale = scratch.insert_htmlbox(box, "".join(contents), css=css,
                                              archive=typography["archive"], scale_low=1)
        visible = Counter(c for c in unicodedata.normalize("NFKC", scratch.get_text()) if not c.isspace())
        ink = [pymupdf.Rect(item[:4]) for item in scratch.get_text("blocks") if item[6] == 0 and item[4].strip()]
        if spare < 0 or not ink or expected - visible:
            overlays.delete_page(-1)
            continue
        lines = [pymupdf.Rect(line["bbox"]) for item in scratch.get_text("dict")["blocks"]
                 if item.get("type") == 0 for line in item["lines"]]
        if any(min(a.y1, b.y1) - max(a.y0, b.y0) > .6
               and min(a.x1, b.x1) - max(a.x0, b.x0) > 1
               for i, a in enumerate(lines) for b in lines[i + 1:]):
            overlays.delete_page(-1)
            continue
        crop = pymupdf.Rect(box.x0, min(item.y0 for item in ink) - .3,
                            box.x1, max(item.y1 for item in ink) + .5)
        if (crop.y0 < box.y0 - group[0][1]["size"] * .4 or crop.y1 > box.y1
                or any(overlap(crop, pymupdf.Rect(span["bbox"])) > .1 for span in foreign)):
            overlays.delete_page(-1)
            continue
        combined = {**members[0], "text": "\n".join(b["text"] for b in members),
                    "translation": "\n".join(b["translation"] for b in members), "_cover_metadata": True,
                    "_members": [key for b in members for key in b.get("_members", [b["id"]])]}
        style = {**group[0][1], "spans": [span for span in spans if id(span) in own_spans]}
        result = {"overlay": len(overlays) - 1, "rect": list(crop), "clip": list(crop), "scale": factor}
        contact_links = anchor_overlay_rects(scratch, anchors) if anchors else []
        if contact_links:
            result["links"] = contact_links
        rects[combined["id"]] = region
        return (combined, style, result), members, ""
    return None, members, "作者、单位和邮箱无法在摘要前安全容纳，暂保留完整原文信息区"


def is_short_cjk_line(block, style, text):
    source = block.get("text", "")
    return (len(style.get("lines", [])) == 1
            and "\n" not in block.get("translation", "")
            and len(text.strip()) <= 10
            and any("\u3400" <= char <= "\u9fff" for char in text)
            and not any(MATH_FONT.search(span.get("font", "")) for span in style.get("spans", []))
            and not re.search(r"[=<>∑∫√∏∂∇]|(?:\b[A-Za-z]\d+\b)|(?:\b[A-Za-z]+\s*[_^]\s*\w)", source))


def short_cjk_line_boxes(source_rect, box, page, block, style, text_obstacles,
                         text, size, centered):
    """Offer a wider, safe box when a short one-line source wraps in Chinese."""
    if centered or not is_short_cjk_line(block, style, text):
        return []
    required_width = 2.0
    for char in text:
        if char.isspace():
            required_width += size * .25
        elif unicodedata.east_asian_width(char) in {"F", "W"}:
            required_width += size
        elif unicodedata.category(char).startswith("P"):
            required_width += size * .5
        else:
            required_width += size * .55

    right_limit = page.rect.width - 12
    own_spans = {id(span) for span in style.get("spans", [])}
    for span in text_obstacles or []:
        if id(span) in own_spans:
            continue
        other = pymupdf.Rect(span["bbox"])
        if min(source_rect.y1, other.y1) - max(source_rect.y0, other.y0) <= .25:
            continue
        if other.x0 >= source_rect.x1 - .5:
            right_limit = min(right_limit, other.x0 - .8)
        elif other.x1 > source_rect.x1 + .2 and other.x0 < source_rect.x1 - .2:
            right_limit = min(right_limit, source_rect.x1)

    max_width = right_limit - source_rect.x0
    if max_width <= source_rect.width + 1:
        return []
    target_width = min(max_width, max(source_rect.width + 2, required_width))
    widths = []
    for candidate in (target_width, target_width + 4, target_width + 8):
        candidate = min(candidate, max_width)
        if candidate > source_rect.width + 1 and (not widths or candidate > widths[-1] + .5):
            widths.append(candidate)
    return [pymupdf.Rect(box.x0, box.y0, box.x0 + width, box.y1) for width in widths]


def prominent_heading(style, text, text_obstacles):
    """Use the page's ordinary text size to recognize display type, including regular weight."""
    if (not re.search(r"[A-Za-z]{2,}|[\u3400-\u9fff]{2,}", text)
            or re.search(r"[=≤≥∑∫√∏∂∇]", text)):
        return False
    sizes = Counter()
    for span in text_obstacles or []:
        if MATH_FONT.search(span.get("font", "")):
            continue
        letters = len(re.findall(r"[A-Za-z\u3400-\u9fff]", span.get("text", "")))
        if letters >= 2 and span.get("size", 0) > 0:
            sizes[round(span["size"], 1)] += letters
    return bool(sizes and style["size"] >= sizes.most_common(1)[0][0] * 1.25)


def source_paragraph_style(style, rect, text, page_width=None, text_obstacles=(), graphics=()):
    """Infer alignment from visual baselines, never from a body block's center.

    Inline symbols can split a baseline into several extraction lines. Ignore
    superscripts when measuring leading and join the remaining fragments first.
    """
    size = style["size"]
    fragments = []
    for line in style.get("lines", []):
        # Small caps measure ~.8 of the nominal size and belong to the same
        # line; only superscripts and other true sub-runs are excluded.
        spans = [s for s in line.get("spans", []) if s.get("text", "").strip()
                 and size * .75 <= s.get("size", size) <= size * 1.25]
        if spans:
            box = pymupdf.Rect(spans[0]["bbox"])
            for span in spans[1:]:
                box |= pymupdf.Rect(span["bbox"])
            baseline = sorted(s.get("origin", (0, s["bbox"][3]))[1] for s in spans)[len(spans) // 2]
            fragments.append((baseline, box))
    rows = []
    for baseline, box in sorted(fragments, key=lambda item: (item[0], item[1].x0)):
        if rows and abs(baseline - rows[-1][0]) <= size * .25:
            rows[-1][1] |= box
        else:
            rows.append([baseline, box])
    distances = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:])
                       if size * .85 <= b[0] - a[0] <= size * 2)
    leading = min(1.7, distances[len(distances) // 2] / size) if distances else 1.2
    alignment, indent = "left", 0
    tolerance = max(1, size * .2)
    boxes = [row[1] for row in rows]
    if len(boxes) >= 2:
        centers = [(b.x0 + b.x1) / 2 for b in boxes]
        if (max(centers) - min(centers) <= tolerance
                and max(b.width for b in boxes) - min(b.width for b in boxes) > size * 1.5):
            alignment = "center"
        else:
            left = min(b.x0 for b in boxes)
            first_indent = boxes[0].x0 - left
            if is_prose(text) and tolerance < first_indent <= size * 3:
                indent = first_indent
            if (is_prose(text) and all(abs(b.x1 - rect.x1) <= tolerance for b in boxes[:-1])
                    and all(abs(b.x0 - left) <= tolerance for b in boxes[1:])):
                alignment = "justify"
    elif boxes and (prominent_heading(style, text, text_obstacles)
                    or section_heading(text) and not is_prose(text)
                    or _FLOW_CAPTION.match(text) or re.match(r"^\s*[图表]\s*\d", text)):
        # Single display lines may be separate extraction blocks. Infer their
        # axis from page/column/artwork geometry, independently of font weight.
        center = (rect.x0 + rect.x1) / 2
        axes = [page_width / 2] if page_width else []
        peers = []
        for span in text_obstacles or []:
            box = pymupdf.Rect(span["bbox"])
            if (box.width > rect.width + size * 2 and box.x0 < rect.x0 - size
                    and box.x1 > rect.x1 + size):
                axes.append((box.x0 + box.x1) / 2)
            baseline = span.get("origin", (0, box.y1))[1]
            if (abs(span.get("size", 0) - size) <= size * .08
                    and size * .75 <= abs(baseline - rows[0][0]) <= size * 2.2
                    and min(box.x1, rect.x1) > max(box.x0, rect.x0)
                    and re.search(r"[A-Za-z\u3400-\u9fff]{2,}", span.get("text", ""))):
                peer = next((item for item in peers if abs(item[0] - baseline) <= size * .25), None)
                if peer is None:
                    peers.append([baseline, box])
                else:
                    peer[1] |= box
        for graphic in graphics:
            box = pymupdf.Rect(graphic)
            distance = max(box.y0 - rect.y1, rect.y0 - box.y1, 0)
            if (distance <= size * 4 and box.width > rect.width + size * 2
                    and box.x0 < rect.x0 - size and box.x1 > rect.x1 + size):
                axes.append((box.x0 + box.x1) / 2)
        # A full first line of a left/right-aligned title can coincidentally
        # span the page symmetrically. Its shorter adjacent line disambiguates it.
        edge_aligned = any(abs(box.width - rect.width) > size * 1.5
                           and abs((box.x0 + box.x1) / 2 - center) > tolerance
                           and (abs(box.x0 - rect.x0) <= tolerance or abs(box.x1 - rect.x1) <= tolerance)
                           for _, box in peers)
        if not edge_aligned and any(abs(center - axis) <= tolerance for axis in axes):
            alignment = "center"
    return alignment, indent, leading


def fitted_overlay(overlays, page, block, rect, style, obstacles, typography=None,
                   horizontal_obstacles=None, bold_hints=None):
    if block.get("translation"):
        block = {**block, "translation": soft_translation(block)}
    typography = typography or font_resources(block["translation"])
    archive = pymupdf.Archive()
    if typography["archive"] is not None:
        archive.add(typography["archive"])
    math_labels = []
    alignment, indent, source_leading = source_paragraph_style(
        style, rect, block["text"], page.rect.width, horizontal_obstacles, obstacles)
    if block.get("drop_cap"):
        indent = 0
    cover_role = cover_text_role(block, page, rect, style)
    if cover_role == "title" and alignment != "center":
        cover_role = None
    # Keep the source's nominal title size. Inflating cover titles by 12% made
    # the translated title larger than the source and forced tight lines to
    # shrink; a title group now fits at the size the document actually used.
    size = style["size"]
    display_text, marker_style = list_marker_style(block, style, size)
    validation_text = display_text
    single_line = (bool(style["lines"])
                   and max(line["bbox"][1] for line in style["lines"])
                   - min(line["bbox"][1] for line in style["lines"]) < size * .5)
    # Keep an 8 pt floor for body text. Labels that were already smaller may
    # still shrink to 75% instead of being pinned at 100% and overflowing.
    minimum = 8 if size >= 8 else max(4.5, size * .75)
    scale_low = min(1, max(.70, minimum / size))
    # Built-in CJK fallback lacks some Mathematical Alphanumeric Symbols and
    # superscript digits. Their compatibility equivalents preserve the
    # variable/number rather than discarding an otherwise printable block.
    printable = printable_text(space_cjk_latin(display_text))
    if single_line:
        # Extraction separates section numbers and inline symbols with newlines,
        # even when the heading occupies a single visual baseline.
        printable = printable.replace("\n", " ")
    variants = []
    for candidate in (printable, compact_punctuation(printable)):
        if candidate not in variants:
            variants.append(candidate)
    expanded = pymupdf.Rect(rect)
    expanded.y1 = min(page.rect.height, rect.y1 + min(48, rect.height * .35 + 12))
    for item in obstacles:
        if min(rect.x1, item.x1) - max(rect.x0, item.x0) > 1 and item.y0 >= rect.y1 - .5:
            expanded.y1 = min(expanded.y1, max(rect.y1, item.y0 - .5))
    # A formula block can start above the prose's bottom because of accents or
    # superscripts. Its individual glyphs still constrain downward expansion.
    own_spans = {id(span) for span in style.get("spans", [])}
    for span in horizontal_obstacles or []:
        if id(span) in own_spans:
            continue
        item = pymupdf.Rect(span["bbox"])
        if (min(rect.x1, item.x1) - max(rect.x0, item.x0) > 1
                and item.y1 >= rect.y1 - .5 and item.y0 >= rect.y1 - size):
            expanded.y1 = min(expanded.y1, max(rect.y1, item.y0 - .5))
    # Normal leading first; tighter leading is only a fallback for one-line
    # references/labels whose glyph box is barely as tall as the font. This
    # keeps already-fitting lines from creeping into the line above.
    cjk = bool(re.search(r"[\u3400-\u9fff]", printable))
    preferred_leading = max(source_leading, 1.45 if cjk else 1.2)
    line_heights = ([1.25, 1.12, 1.0] if single_line else
                    list(dict.fromkeys([preferred_leading, max(source_leading, 1.3), 1.2])))
    if typography.get("compact_fallback"):
        line_heights = [1.15, 1.04, 1.0] if single_line or size < 9 else [1.15]
    if cover_role == "authors":
        line_heights = [1.18, 1.08] if not typography.get("compact_fallback") else [1.12, 1.04]

    def render_box(box, markup, css, count_lines, minimum_scale, anchors=()):
        scratch = overlays.new_page(width=page.rect.width, height=page.rect.height)
        spare, scale = scratch.insert_htmlbox(box, markup, css=css,
                                              archive=archive, scale_low=minimum_scale)
        if spare < 0:
            overlays.delete_page(-1)
            return None, 0
        normalize = lambda value: Counter(c for c in unicodedata.normalize("NFKC", value)
                                           if not c.isspace() and c != "\u00ad")
        if normalize(validation_text) - normalize(scratch.get_text()):
            overlays.delete_page(-1)
            return None, 0
        line_count = 0
        if count_lines:
            positions = sorted(line["bbox"][1] for item in scratch.get_text("dict")["blocks"]
                               if item.get("type") == 0 for line in item["lines"])
            baselines = []
            for position in positions:
                if not baselines or abs(position - baselines[-1]) > 1.5:
                    baselines.append(position)
            line_count = len(baselines)
        ink = [item for item in scratch.get_text("blocks") if item[6] == 0 and item[4].strip()]
        images = scratch.get_image_info()
        ink.extend(image["bbox"] for image in images)
        if not ink:
            overlays.delete_page(-1)
            return None, 0
        crop = pymupdf.Rect(box)
        crop.y0 = max(box.y0, min(item[1] for item in ink) - .3)
        crop.y1 = min(box.y1, max(item[3] for item in ink) + 1)
        # Keep the source spelling selectable/searchable beneath each symbol
        # image. Invisible text cannot interfere with visible glyph geometry.
        for image, label in zip(images, math_labels):
            image_box = pymupdf.Rect(image["bbox"])
            scratch.insert_text((image_box.x0, image_box.y1 - 1), label,
                                fontsize=min(6, image_box.height * .5),
                                fontname="china-s", render_mode=3)
        visible = text_ink_bounds(scratch.get_bboxlog(), crop)
        # Formula images contribute their full extent; invisible search text
        # is intentionally excluded from the glyph measurement.
        if visible is not None:
            for item in images:
                visible |= pymupdf.Rect(item["bbox"])
        result = {"overlay": len(overlays) - 1, "rect": list(crop),
                  "clip": list(crop), "scale": scale, "math_labels": math_labels[:],
                  "ink_insets": [visible.y0 - crop.y0, crop.y1 - visible.y1]
                  if visible is not None else None}
        if anchors:
            placed_links = anchor_overlay_rects(scratch, anchors)
            if placed_links:
                result["links"] = placed_links
        return result, line_count

    # Display type keeps the source's bold weight even when its text contains
    # ordinary function words: "Learning ... for ..." is prose to is_prose but
    # still a bold cover title. Body paragraphs keep the conservative rule so a
    # bold run-in label cannot make the entire paragraph heavy.
    display_weight = cover_role == "title"
    if not display_weight and style["bold"]:
        display_weight = prominent_heading(style, block["text"], horizontal_obstacles or ())
    weight = 600 if section_heading(block["text"]) or (
        style["bold"] and (display_weight or not is_prose(block["text"]))) else 400

    for variant in variants:
        ranges = bold_ranges(variant, bold_hints) if bold_hints else ()
        contents, validation_text, math_labels, resolved_links = inline_math_markup(
            variant, block.get("_inline_assets", []), archive, bold=ranges,
            fallback=typography.get("fallback_characters", ""), marker_style=marker_style,
            links=block.get("_link_hints", []))
        anchors = [{"text": item["text"], "kind": item["kind"], "uri": item.get("uri"),
                    "page": item.get("page"), "to": item.get("to"), "source": item.get("source"),
                    "ordinal": item["ordinal"]} for item in resolved_links]
        markup = "<div>" + contents + "</div>"
        # Try every safe box/leading at the original font size before shrinking.
        # A tight source glyph frame should not force the whole paragraph small.
        for minimum_scale, line_height in [(scale, leading) for scale in (1, scale_low)
                                          for leading in line_heights]:
            css = typography["css"] + (f"*{{margin:0;padding:0}} body{{font-family:{typography['family']};font-size:{size}pt;"
                   f"line-height:{line_height};color:#{style['color']:06x};"
                   f"font-weight:{weight};"
                   f"font-style:{'italic' if style['italic'] else 'normal'};}} b{{font-weight:600}}"
                   " a:link,a:visited,a:hover{color:inherit;text-decoration:none}")
            centered = cover_role in {"title", "authors"} or alignment == "center"
            css += f"body{{text-align:{'center' if centered else alignment};text-indent:{0 if centered else indent}pt}}"
            boxes = [expanded, rect] if cover_role in {"title", "authors"} else [rect, expanded]
            for box in boxes:
                result, line_count = render_box(
                    box, markup, css, is_short_cjk_line(block, style, variant) and not centered,
                    minimum_scale, anchors)
                if result:
                    if line_count > 1:
                        for wider_box in short_cjk_line_boxes(
                                rect, box, page, block, style, horizontal_obstacles,
                                variant, size, centered):
                            wider_result, wider_lines = render_box(wider_box, markup, css, True, minimum_scale, anchors)
                            if wider_result and wider_lines == 1:
                                return wider_result
                            if wider_result:
                                overlays.delete_page(wider_result["overlay"])
                    return result
    if typography["archive"] is not None and not typography.get("compact_fallback"):
        # Tight regions can use closer leading while retaining the same font.
        # Unsupported symbols fall back individually; never switch all Chinese
        # prose to a heavier sans face just because one glyph failed.
        result = fitted_overlay(overlays, page, block, rect, style, obstacles,
                                {**typography, "compact_fallback": True}, horizontal_obstacles,
                                bold_hints=bold_hints)
        if result:
            result["leading_fallback"] = True
        return result
    return None


TABLE_CAPTION = re.compile(r"^\s*(?:TABLE|Table)\s+[IVXLC\d]+(?:$|\s|[.:])|^\s*表\s*[IVXLC\d]+", re.I)


def _table_text(page):
    lines, captions = [], []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]:
        if block.get("type") != 0:
            continue
        text = "\n".join("".join(span["text"] for span in line["spans"]).strip() for line in block["lines"]).strip()
        if not text:
            continue
        if TABLE_CAPTION.match(text):
            captions.append(pymupdf.Rect(block["bbox"]))
        for line in block["lines"]:
            value = "".join(span["text"] for span in line["spans"]).strip()
            if not value:
                continue
            lines.append({"text": value, "rect": pymupdf.Rect(line["bbox"]),
                          "size": max(span["size"] for span in line["spans"])})
            if TABLE_CAPTION.match(value):
                rect = pymupdf.Rect(line["bbox"])
                if not any(_same_region(rect, caption) or caption.contains(rect) for caption in captions):
                    captions.append(rect)
    return lines, captions


def _table_gutter(page, lines):
    """Return the x of a two-column gutter; single-column pages return None."""
    body = [line for line in lines if line["rect"].width >= page.rect.width * .2]
    if len(body) < 6:
        return None
    best = None
    for x in range(int(page.rect.width * .35), int(page.rect.width * .65) + 1, 2):
        crossing = sum(1 for line in body if line["rect"].x0 < x < line["rect"].x1)
        left = sum(1 for line in body if line["rect"].x1 <= x)
        right = sum(1 for line in body if line["rect"].x0 >= x)
        if left < len(body) * .15 or right < len(body) * .15:
            continue
        if best is None or crossing < best[0]:
            best = crossing, x
    return best[1] if best and best[0] <= max(2, len(body) * .06) else None


def _table_rows(lines):
    rows = []
    for line in sorted(lines, key=lambda item: (item["rect"].y0, item["rect"].x0)):
        row = next((row for row in rows[-2:]
                    if abs(row["rect"].y0 - line["rect"].y0) <= max(1.2, line["size"] * .28)), None)
        if row is None:
            rows.append({"rect": pymupdf.Rect(line["rect"]), "cells": [line], "size": line["size"]})
        else:
            row["cells"].append(line)
            row["rect"] |= line["rect"]
            row["size"] = max(row["size"], line["size"])
    return sorted(rows, key=lambda row: row["rect"].y0)


def _table_row_is_grid(row, page_width):
    cells = sorted(row["cells"], key=lambda cell: cell["rect"].x0)
    if len(cells) < 2:
        return False
    gaps = [cells[index + 1]["rect"].x0 - cells[index]["rect"].x1 for index in range(len(cells) - 1)]
    if max(gaps) < max(5, row["size"] * .6):
        return False
    widths = sorted(cell["rect"].width for cell in cells)
    if widths[len(widths) // 2] > page_width * .30:
        return False
    # Two body columns side by side are wide cells, not table columns.
    return not (len(cells) <= 2 and all(cell["rect"].width > page_width * .25 for cell in cells))


def _same_region(first, second):
    intersection = overlap(first, second)
    return intersection > 0 and intersection / min(first.get_area(), second.get_area()) > .8


def table_regions(page):
    """Locate tables from a Table caption and consistent multi-cell rows.

    Borderless tables are extracted as independent cell lines, so a caption
    anchors the search and grid rows separate the table from two-column prose.
    Returned boxes exclude captions; captions and notes outside the grid stay
    translatable.
    """
    lines, captions = _table_text(page)
    if not lines or not captions:
        return []
    gutter = _table_gutter(page, lines)
    regions = []
    for caption in captions:
        if gutter:
            if caption.x1 <= gutter + 8:
                x0, x1 = page.rect.x0, gutter
            elif caption.x0 >= gutter - 8:
                x0, x1 = gutter, page.rect.x1
            else:
                x0, x1 = page.rect.x0, page.rect.x1
        else:
            x0, x1 = page.rect.x0, page.rect.x1
        side = [line for line in lines if x0 <= (line["rect"].x0 + line["rect"].x1) / 2 <= x1]
        rows = _table_rows(side)
        grid = [_table_row_is_grid(row, page.rect.width) for row in rows]
        groups = []
        for index, is_grid in enumerate(grid):
            if not is_grid:
                continue
            if groups and rows[index]["rect"].y0 - rows[groups[-1][-1]]["rect"].y1 <= max(24, rows[index]["size"] * 2.6):
                groups[-1].append(index)
            else:
                groups.append([index])
        best = None
        for group in groups:
            if len(group) < 3:
                continue
            region = pymupdf.Rect(rows[group[0]]["rect"])
            for index in range(group[0], group[-1] + 1):
                region |= rows[index]["rect"]
            if region.width < page.rect.width * .15 or not (12 <= region.height < page.rect.height * .8):
                continue
            if any(_same_region(region, existing) for existing in regions):
                continue
            inside = [(row, is_grid) for row, is_grid in zip(rows, grid)
                      if row["rect"].y0 >= region.y0 - 2 and row["rect"].y1 <= region.y1 + 2]
            if not inside:
                continue
            grid_lines = sum(1 for row, is_grid in inside
                             if is_grid or (len(row["cells"]) == 1 and row["rect"].width < region.width * .92
                                            and len(row["cells"][0]["text"]) <= 80))
            if grid_lines / len(inside) < .55:
                continue
            horizontal = min(region.x1, caption.x1) - max(region.x0, caption.x0)
            gap = max(0, region.y0 - caption.y1, caption.y0 - region.y1)
            if horizontal <= 0 or gap > 60:
                continue
            if best is None or gap < best[0]:
                best = gap, region
        if best:
            regions.append(best[1])
    return regions


def typeset(source: Path, document: dict, target: str, page_numbers=None, typography=None,
            page_links=None, reference_fallbacks=None):
    """Typeset translated blocks onto the original pages.

    `page_links` maps each original page index to its link annotations in
    unrotated coordinates, as collected by `build_layout` from the full
    document; without it, links are read from the opened pages directly.
    `reference_fallbacks` maps page indexes to bibliography column areas that
    cannot be rebuilt into complete entries; `build_layout` derives it from the
    whole document so continuation pages follow the heading page's decision.
    """
    typography = typography or font_resources("\n".join(b.get("translation", "") for p in document["pages"] for b in p["blocks"]))
    pdf = pymupdf.open(source)
    # Internal jump destinations are document page indexes; keep the full count
    # before selecting the one page this pass typesets (the assembled document
    # restores them, but the report must accept every valid target).
    total_pages = pdf.page_count
    if page_numbers is not None:
        pdf.select(page_numbers)
    report = {"target": target, "pages": len(pdf), "replaced": 0, "untranslated": 0,
              "preserved": 0, "scaled": 0, "issues": [], "placements": {}}
    try:
        document = copy.deepcopy(document)
        normalize_drop_caps(document, pdf)
        for number, saved_page in enumerate(document["pages"]):
            page = pdf[number]
            source_page_number = page_numbers[number] if page_numbers is not None else number
            rotation = page.rotation
            native = [b for b in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"] if b["type"] == 0]
            raw_native = page.get_text("rawdict")["blocks"]
            working = layout_blocks(page, saved_page, native, target,
                                    cover_page=source_page_number == 0)
            rects = {b["id"]: source_rect(page, b) for b in working}
            # Link hints must share the unrotated coordinate space of rects and
            # native text, so collect them before clearing the page rotation.
            if page_links is None:
                links_on_page = page_link_targets(page)
            else:
                links_on_page = list(page_links.get(source_page_number, ()))
            page.set_rotation(0)
            source_ink = page.get_bboxlog()
            graphics = [pymupdf.Rect(image["bbox"]) for image in page.get_image_info()]
            drawings = visible_drawings(page)
            graphics += [drawing["rect"] for drawing in drawings]
            table_boxes = table_regions(page)
            obstacles = [pymupdf.Rect(b["bbox"]) for b in native] + graphics
            text_obstacles = [span for item in native for line in item["lines"]
                              for span in line["spans"] if span["text"].strip()]
            has_redactions = any(a.type[0] == pymupdf.PDF_ANNOT_REDACT for a in (page.annots() or []))
            prepared = []
            formula_candidates = []
            table_blocks = 0
            dropped_links = 0
            fallback_ids = set()
            if not saved_page.get("reference_layout") and not has_redactions:
                # A bibliography whose complete entries cannot be rebuilt (for
                # example an unquoted book title or a multi-page list) is kept
                # whole: translating its line fragments independently mixes
                # languages inside one entry and overlaps the lines left
                # original. The heading itself still translates normally.
                regions = (reference_fallbacks.get(source_page_number, [])
                           if reference_fallbacks is not None else
                           fallback_reference_regions(page))
                fallback = [block for block in working
                            if any(region.contains((rects[block["id"]].tl + rects[block["id"]].br) * .5)
                                   for region in regions)]
                if any(REFERENCE_MARK.search(block["text"]) for block in fallback):
                    fallback_ids = {block["id"] for block in fallback}
                    members = sum(len(block.get("_members", [block["id"]])) for block in fallback)
                    report["preserved"] += members
                    report["issues"].append({
                        "page": saved_page["number"], "block_id": None, "kind": "reference",
                        "message": f"此页参考文献无法按完整条目重建，已整段保留原文（{members} 个文本块），"
                                   "避免译文与原文错配或重叠"})
            with pymupdf.open() as overlays:
                cover, cover_members, cover_reason = (None, [], "")
                if source_page_number == 0 and not has_redactions:
                    cover, cover_members, cover_reason = prepare_cover_metadata(
                        page, working, rects, native, overlays, typography, target)
                cover_ids = {b["id"] for b in cover_members}
                if cover:
                    prepared.append(cover)
                    report["replaced"] += len(cover[0]["_members"])
                    if cover[2]["scale"] < .99:
                        report["scaled"] += len(cover[0]["_members"])
                for block in working:
                    if block["id"] in fallback_ids:
                        continue
                    if block.get("kind") in {"reference", "reference_heading"}:
                        continue
                    entry = {"page": saved_page["number"], "block_id": block["id"]}
                    member_ids = block.get("_members", [block["id"]])
                    if block["id"] in cover_ids:
                        if not cover:
                            kind = "untranslated" if not block.get("translation") or block.get("target") != target else "preserved"
                            report[kind] += len(member_ids)
                            report["issues"].extend({**entry, "block_id": key, "kind": kind, "message": cover_reason} for key in member_ids)
                        continue
                    block_rect = rects[block["id"]]
                    if table_boxes and not TABLE_CAPTION.match(block["text"]) and any(
                            overlap(block_rect, box) >= block_rect.get_area() * .5 for box in table_boxes):
                        # Keep the published table intact; mixed cell translations
                        # were unreadable. Captions and notes outside the grid still
                        # translate through their own blocks.
                        report["preserved"] += len(member_ids)
                        table_blocks += len(member_ids)
                        continue
                    if not block["translation"] or block.get("target") != target:
                        report["untranslated"] += 1
                        report["issues"].append({**entry, "kind": "untranslated", "message": "尚无当前语言译文，保留原文"})
                        continue
                    if unchanged_non_text(block):
                        unchanged_rect = rects[block["id"]]
                        _, unchanged_reason = inspect_block(native, unchanged_rect)
                        if unchanged_reason and unchanged_reason.startswith("公式") and not has_redactions:
                            formula_candidates.append((block, unchanged_rect))
                        elif (not has_redactions and len(block["text"]) <= 80
                              and not re.search(r"[A-Za-z]{2,}|[\u3400-\u9fff]", block["text"])):
                            formula_candidates.append(({**block, "_math_fragment": True}, unchanged_rect))
                        # The source page already shows the right digits/symbols;
                        # erasing and reinserting them only creates false alarms.
                        report["replaced"] += len(member_ids)
                        continue
                    rect = rects[block["id"]]
                    style, reason = inspect_block(native, rect, combine="_members" in block)
                    if block.get("drop_cap_preserve"):
                        reason = block["drop_cap_preserve"]
                    if block.get("drop_cap_review"):
                        report["issues"].append({**entry, "kind": "review",
                                                 "message": "已恢复首字下沉的完整单词；已有正文译文保持不变，请核对段首词义，必要时重新翻译此段"})
                    if has_redactions:
                        reason = "本页有原有涂黑批注，保留原页以避免改变批注"
                    if rect.is_empty or rect.is_infinite or not page.rect.contains(rect):
                        reason = "文字区域超出页面，保留原文"
                    # Do not remove any source characters belonging to neighboring blocks.
                    own_spans = {id(span) for span in style["spans"]} if style else set()
                    foreign = [span for item in native if overlap(rect, pymupdf.Rect(item["bbox"])) > .1
                               for line in item["lines"] for span in line["spans"]
                               if id(span) not in own_spans and span["text"].strip()]
                    strips = [span_strip(span) for span in style["spans"]] if style else []
                    if style and any(overlap(strip, pymupdf.Rect(span["bbox"])) > .1 for strip in strips for span in foreign):
                        reason = "文字区域相互重叠，保留原文"
                    result = None
                    if not reason:
                        fit_rect = pymupdf.Rect(rect)
                        for neighbor in foreign:
                            box = pymupdf.Rect(neighbor["bbox"])
                            if (box.y0 < rect.y0 < box.y1 and box.y1 - rect.y0 < 3
                                    and min(box.x1, rect.x1) - max(box.x0, rect.x0) > 1):
                                fit_rect.y0 = max(fit_rect.y0, box.y1 + .6)
                        runs = []
                        if should_preserve_inline_symbols(block):
                            runs = inline_math_runs(raw_native, rect, combine="_members" in block)
                            if runs:
                                block = {**block, "_inline_assets": inline_math_assets(page, runs, block["translation"])}
                        hints = block_link_hints(links_on_page, rect, native, raw_native)
                        if hints:
                            block = {**block, "_link_hints": hints}
                        block = {**block, "_source_page": source_page_number}
                        result = fitted_overlay(overlays, page, block, fit_rect, style, obstacles,
                                                typography, text_obstacles,
                                                bold_hints=inline_bold_hints(style, block["text"]))
                        if result and runs:
                            result["inline_math_runs"] = runs
                            result["carried_graphics"] = inline_math_graphics(drawings, runs)
                        if result and hints:
                            resolved_links = result.get("links") or []
                            whole_links = []
                            # A link that covers the block proves the whole
                            # translated block is its anchor (fully linked
                            # titles, author lines, license sentences). Only a
                            # single-link block is unambiguous.
                            if len(hints) == 1 and hints[0].get("whole_block"):
                                hint = hints[0]
                                matched = any(
                                    item.get("kind") == hint["kind"]
                                    and (item.get("uri") or None) == (hint.get("uri") or None)
                                    and item.get("source") == hint.get("source")
                                    for item in resolved_links)
                                if not matched:
                                    whole_links.append({**hint, "whole": True})
                            if whole_links:
                                result["whole_links"] = whole_links
                            kept = {(item.get("kind"), item.get("uri") or None)
                                    for item in resolved_links + whole_links}
                            dropped_links += sum(
                                1 for hint in hints if hint["kind"] == "uri"
                                and ("uri", hint.get("uri") or None) not in kept)
                        if result is None:
                            reason = "译文超出可用空间或含无法可靠排印的字符，保留原文；请编辑后重新排版"
                    if reason and reason.startswith("公式") and not has_redactions and not is_prose(block["text"]):
                        formula_candidates.append((block, rect))
                    if reason:
                        report["preserved"] += len(member_ids)
                        report["issues"].extend({**entry, "block_id": block_id, "kind": "preserved", "message": reason} for block_id in member_ids)
                        continue
                    # Redact only the original span boxes. Background graphics stay untouched.
                    prepared.append((block, style, result))
                    measured = text_ink_bounds(source_ink, rect)
                    if measured is not None:
                        style["ink_bounds"] = list(measured)
                    if result["scale"] < .99:
                        report["scaled"] += len(member_ids)
                    report["replaced"] += len(member_ids)
                lanes = column_lanes(prepared, rects, page.rect.width, is_prose)
                owned_spans = [span for _, style, _ in prepared for span in style["spans"]]
                formulas = original_formula_units(page, overlays, formula_candidates, native, graphics,
                                                  lanes, occupied_spans=owned_spans,
                                                  glyph_bounds=lambda spans: formula_glyph_bounds(page, spans, raw_native))
                prepared.extend(formulas)
                for block, _, result in formulas:
                    rects[block["id"]] = pymupdf.Rect(result["clip"])
                for block, _, result in prepared:
                    if section_heading(block["text"]) and not result.get("original"):
                        separators = [tuple(drawing["rect"]) for drawing in drawings
                                      if heading_separator(drawing, rects[block["id"]])]
                        result.setdefault("carried_graphics", []).extend(separators)
                adjustments = reflow_columns(prepared, native, graphics, rects, page.rect.width, is_prose)
                report.setdefault("flow", []).extend({"page": saved_page["number"], **item} for item in adjustments)
                moved_formulas = [item for item in formulas if item[2]["rect"] != item[2]["clip"]]
                for _, _, result in moved_formulas:
                    page.add_redact_annot(pymupdf.Rect(result["clip"]), fill=False, cross_out=False)
                report.setdefault("original_placements", []).extend(
                    key for block, _, _ in moved_formulas for key in block.get("_members", [block["id"]]))
                inline_strokes = {box for _, _, result in prepared
                                  if result.get("inline_math_runs")
                                  for box in result.get("carried_graphics", [])}
                for box in inline_strokes:
                    page.add_redact_annot(pymupdf.Rect(box) + (-.25, -.25, .25, .25),
                                         fill=False, cross_out=False)
                heading_strokes = set()
                for block, style, result in prepared:
                    if result.get("original") or not section_heading(block["text"]):
                        continue
                    source = rects.get(block["id"])
                    if source is None:
                        continue
                    for drawing in drawings:
                        if heading_separator(drawing, source):
                            heading_strokes.add(tuple(pymupdf.Rect(drawing["rect"])))
                for box in heading_strokes:
                    page.add_redact_annot(pymupdf.Rect(box) + (-.5, -.5, .5, .5),
                                          fill=False, cross_out=False)
                if moved_formulas or inline_strokes or heading_strokes:
                    page.apply_redactions(images=0, graphics=1, text=0)
                if saved_page.get("reference_layout"):
                    bibliography = prepare_bibliography(page, saved_page, native, overlays, prepared, rects, typography, target,
                                                        anchor_rects=anchor_overlay_rects)
                    if bibliography:
                        bibliography_items, details = bibliography
                        prepared.extend(bibliography_items)
                        report.setdefault("bibliography", []).append({"page": saved_page["number"], **details})
                        for block, _, _ in bibliography_items:
                            if block.get("translation") and block.get("target") == target:
                                report["replaced"] += 1
                            else:
                                report["untranslated"] += 1
                                report["issues"].append({"page": saved_page["number"], "block_id": block["id"],
                                                         "kind": "untranslated", "message": "此参考文献题名尚未翻译，保留完整原文条目"})
                    else:
                        retained = [block for block in working if block.get("kind") in {"reference", "reference_heading"}]
                        report["preserved"] += len(retained)
                        report["issues"].append({"page": saved_page["number"], "block_id": None, "kind": "preserved",
                                                 "message": "完整参考文献无法在安全字号下容纳，保留原文；请缩短题名译文后重试"})
                for block, _, result in prepared:
                    if block.get("_inline_assets"):
                        report.setdefault("inline_math", []).append({
                            "page": saved_page["number"], "blocks": block.get("_members", [block["id"]]),
                            "source": [asset["normalized"] for asset in block["_inline_assets"]],
                            "matched": [normalized_inline_math(label) for label in result.get("math_labels", [])]})
                erased = []
                for block, style, result in prepared:
                    if result.get("original"):
                        if result["rect"] != result["clip"]:
                            erased.extend(list(span["bbox"]) for span in style["spans"])
                        continue
                    for span in style["spans"]:
                        page.add_redact_annot(span_strip(span), fill=False, cross_out=False)
                        erased.append(list(span["bbox"]))
                report.setdefault("erased_spans", []).append({"page": saved_page["number"], "boxes": erased})
                if prepared:
                    page.apply_redactions(images=0, graphics=0, text=0)
                for block, style, result in prepared:
                    if result.get("original") and result["rect"] == result["clip"]:
                        continue
                    box = pymupdf.Rect(result["rect"])
                    page.show_pdf_page(box, overlays, result["overlay"], clip=pymupdf.Rect(result["clip"]))
                    page.set_rotation(rotation)
                    displayed = box * page.rotation_matrix
                    for block_id in block.get("_members", [block["id"]]):
                        report["placements"][block_id] = [displayed.x0 / page.rect.width, displayed.y0 / page.rect.height,
                                                         displayed.x1 / page.rect.width, displayed.y1 / page.rect.height]
                    page.set_rotation(0)
                    # Link anchors travel with the translated text. insert_pdf
                    # drops annotations, so build_layout re-creates these on the
                    # assembled document from the placed_links report entries.
                    for item in [*result.get("links", ()), *result.get("whole_links", ())]:
                        clip = pymupdf.Rect(result["clip"])
                        destination = pymupdf.Rect(result["rect"])
                        whole = bool(item.get("whole"))
                        # Every rectangle of one wrapped link shares a group so
                        # the reader can highlight the whole link at once.
                        group = hashlib.sha1(json.dumps(
                            [saved_page["number"], item["kind"], item.get("uri"),
                             item.get("page"), item.get("to"), item.get("source"),
                             item.get("ordinal")], sort_keys=True, default=str).encode()).hexdigest()[:16]
                        for overlay_box in ([list(destination)] if whole else item.get("rects", ())):
                            area = (pymupdf.Rect(overlay_box) if whole
                                    else _map_overlay_rect(pymupdf.Rect(overlay_box), clip, destination)) & page.rect
                            if area.is_empty or area.get_area() <= 0:
                                continue
                            entry = {"page": saved_page["number"], "kind": item["kind"],
                                     "rect": [area.x0, area.y0, area.x1, area.y1],
                                     "source_rect": item.get("source"), "group": group}
                            if item["kind"] == "uri" and item.get("uri"):
                                entry["uri"] = item["uri"]
                                try:
                                    page.insert_link({"kind": pymupdf.LINK_URI, "from": area, "uri": item["uri"]})
                                except (RuntimeError, ValueError):
                                    pass
                            elif (item.get("kind") == "goto" and isinstance(item.get("page"), int)
                                    and 0 <= item["page"] < total_pages):
                                entry["target_page"] = item["page"]
                                entry["to"] = item.get("to") or [0, 0]
                                try:
                                    page.insert_link({"kind": pymupdf.LINK_GOTO, "from": area,
                                                      "page": item["page"],
                                                      "to": pymupdf.Point(item["to"][0], item["to"][1])})
                                except (RuntimeError, ValueError):
                                    pass
                            else:
                                continue
                            report.setdefault("placed_links", []).append(entry)
            page.set_rotation(rotation)
            if table_blocks:
                report["issues"].append({"page": saved_page["number"], "block_id": None, "kind": "table",
                                         "message": f"表格内容保留原文（{table_blocks} 个文本块）；仅翻译表题与表注"})
            if dropped_links:
                report["issues"].append({
                    "page": saved_page["number"], "block_id": None, "kind": "link",
                    "message": f"本页有 {dropped_links} 个原文外部链接因译文改写锚文本而未能定位，"
                               "译文 PDF 中不可点击；可保留原网址后重新翻译或排版。"})
            if saved_page.get("needs_ocr"):
                report["issues"].append({"page": saved_page["number"], "block_id": None, "kind": "image_page", "message": "无文字层的页面保持原样"})
        pdf.subset_fonts()
        return pdf.tobytes(garbage=4, deflate=True), report
    finally:
        pdf.close()


def _reader_link(page_obj, raw_rect, entry, fraction=None, source_page=None, group=None):
    """A link in displayed normalized coordinates for the reader overlays.

    `source_page` is the page the link lives on (1-based); the reader filters
    overlays by it. For internal jumps `page` is the *target* page, so the two
    values differ and must not be conflated. `group` identifies rectangles of
    one wrapped link so the reader highlights all of them together.
    """
    box = pymupdf.Rect(raw_rect) * page_obj.rotation_matrix
    bounds = page_obj.rect
    link = {"kind": entry["kind"],
            "from": [round(box.x0 / bounds.width, 5), round(box.y0 / bounds.height, 5),
                     round(box.x1 / bounds.width, 5), round(box.y1 / bounds.height, 5)],
            "source_page": source_page if source_page is not None else page_obj.number + 1}
    if group:
        link["group"] = group
    if entry["kind"] == "uri":
        link["uri"] = entry["uri"]
    else:
        link["page"] = entry["target_page"] + 1
        link["fraction"] = round(fraction if fraction is not None else 0.0, 5)
    return link


def build_layout(directory: Path, document: dict, target: str):
    source = directory / "source.pdf"
    signature = json.dumps({"version": ENGINE_VERSION, "font": font_signature(), "source_size": source.stat().st_size,
                            "source_mtime": source.stat().st_mtime_ns,
                            "target": target, "pages": document["pages"]}, ensure_ascii=False, sort_keys=True)
    revision = hashlib.sha256(signature.encode()).hexdigest()[:24]
    cache = directory / "layout"
    with PDF_LOCK:
        cache.mkdir(exist_ok=True)
        pdf_path = cache / (revision + ".pdf")
        report_path = cache / (revision + ".json")
        if pdf_path.is_file() and report_path.is_file():
            return json.loads(report_path.read_text(encoding="utf-8"))
        page_cache = cache / "pages"
        page_cache.mkdir(exist_ok=True)
        report = {"target": target, "pages": len(document["pages"]), "replaced": 0,
                  "untranslated": 0, "preserved": 0, "scaled": 0, "issues": [], "placements": {}}
        typography = font_resources("\n".join(b.get("translation", "") for p in document["pages"] for b in p["blocks"]))
        # One pass over the source resolves named destinations and collects every
        # page's links in unrotated coordinates; per-page typeset calls then see
        # the full document's targets even though only one page is selected.
        source_links = None
        reference_fallbacks = None
        try:
            with pymupdf.open(source) as scanned:
                try:
                    names = scanned.resolve_names()
                except Exception:
                    names = {}
                source_links = {}
                for index, page in enumerate(scanned):
                    # page_link_targets derotates PyMuPDF's displayed link
                    # rectangles itself; resetting the rotation first would
                    # lose the transform on rotated source pages.
                    source_links[index] = page_link_targets(page, names)
                reference_fallbacks = reference_fallback_pages(scanned)
        except Exception:
            source_links = None
            reference_fallbacks = None
        with pymupdf.open() as combined:
            for index, saved_page in enumerate(document["pages"]):
                page_signature = json.dumps({"version": ENGINE_VERSION, "font": font_signature(), "source_size": source.stat().st_size,
                                             "source_mtime": source.stat().st_mtime_ns, "index": index,
                                             "target": target, "page": saved_page}, ensure_ascii=False, sort_keys=True)
                key = hashlib.sha256(page_signature.encode()).hexdigest()[:24]
                page_pdf = page_cache / (key + ".pdf")
                page_report = page_cache / (key + ".json")
                if page_pdf.is_file() and page_report.is_file():
                    details = json.loads(page_report.read_text(encoding="utf-8"))
                else:
                    page_raw, details = typeset(source, {"pages": [saved_page]}, target, page_numbers=[index],
                                                typography=typography, page_links=source_links,
                                                reference_fallbacks=reference_fallbacks)
                    page_temp = page_cache / (key + ".tmp")
                    page_temp.write_bytes(page_raw)
                    page_temp.replace(page_pdf)
                    page_report.write_text(json.dumps(details, ensure_ascii=False), encoding="utf-8")
                with pymupdf.open(page_pdf) as cached:
                    combined.insert_pdf(cached, links=False, annots=True)
                for field in ("replaced", "untranslated", "preserved", "scaled"):
                    report[field] += details[field]
                report["issues"].extend(details["issues"])
                report["placements"].update(details["placements"])
                if details.get("bibliography"):
                    report.setdefault("bibliography", []).extend(details["bibliography"])
                if details.get("flow"):
                    report.setdefault("flow", []).extend(details["flow"])
                for field in ("original_placements", "erased_spans", "inline_math"):
                    if details.get(field):
                        report.setdefault(field, []).extend(details[field])
                if details.get("placed_links"):
                    report.setdefault("placed_links", []).extend(details["placed_links"])
            # Single-page caches cannot resolve links to other pages; rebuild them
            # from the original only after assembling the full document. Anchors
            # that the layout re-attached to translated text replace the original
            # rectangles, and links whose source text was erased are dropped
            # instead of pointing at unrelated content.
            with pymupdf.open(source) as original:
                combined.set_metadata(original.metadata)
                toc = original.get_toc()
                if toc:
                    combined.set_toc(toc)
                try:
                    named_targets = original.resolve_names()
                except Exception:
                    named_targets = {}
                reader_links = []
                placed_sources = {}
                for entry in report.get("placed_links", []):
                    source = entry.get("source_rect")
                    if source:
                        placed_sources.setdefault(entry["page"], set()).add(
                            (entry["kind"], tuple(round(float(value), 1) for value in source)))
                for index, page in enumerate(original):
                    number = index + 1
                    for entry in (item for item in report.get("placed_links", []) if item["page"] == number):
                        box = pymupdf.Rect(entry["rect"])
                        if box.is_empty or box.get_area() <= 0:
                            continue
                        if entry["kind"] == "uri" and entry.get("uri"):
                            spec = {"kind": pymupdf.LINK_URI, "from": box, "uri": entry["uri"]}
                        elif (entry["kind"] == "goto" and isinstance(entry.get("target_page"), int)
                                and 0 <= entry["target_page"] < len(original)):
                            spec = {"kind": pymupdf.LINK_GOTO, "from": box, "page": entry["target_page"],
                                    "to": pymupdf.Point(*(entry.get("to") or (0, 0)))}
                        else:
                            continue
                        try:
                            combined[index].insert_link(spec)
                        except (RuntimeError, ValueError):
                            continue
                        fraction = (goto_fraction(original[entry["target_page"]], entry.get("to"))
                                    if entry["kind"] == "goto" else 0.0)
                        reader_links.append(_reader_link(combined[index], box, entry, fraction,
                                                         source_page=number, group=entry.get("group")))
                    erased = [pymupdf.Rect(box) for item in report.get("erased_spans", [])
                              if item["page"] == number for box in item["boxes"]]
                    for link in page.get_links():
                        raw = pymupdf.Rect(link.get("from") or (0, 0, 0, 0)) * page.derotation_matrix
                        if raw.is_empty or not raw.is_valid or raw.get_area() <= 0:
                            continue
                        kind_name = ("uri" if link.get("kind") == pymupdf.LINK_URI else
                                     "goto" if link.get("kind") in {pymupdf.LINK_GOTO, pymupdf.LINK_NAMED}
                                     else None)
                        # A re-anchored link replaces its source annotation; do
                        # not also restore the original rectangle (it may now sit
                        # over unrelated translated text).
                        if kind_name and (kind_name, tuple(round(value, 1) for value in raw)) in placed_sources.get(number, ()):
                            continue
                        erased_overlap = any(overlap(raw, box) > raw.get_area() * .3 for box in erased)
                        # A web link whose anchor text was erased must not open
                        # from unrelated translated text. A jump target stays
                        # valid even when its cross-reference was reworded, so
                        # unre-anchored goto links keep their original position.
                        if erased_overlap and link.get("kind") == pymupdf.LINK_URI:
                            continue
                        restored = {key: value for key, value in link.items() if key not in {"xref", "id"}}
                        restored["from"] = raw
                        target_page, target_to = link.get("page"), link.get("to")
                        if link.get("kind") == pymupdf.LINK_NAMED:
                            # Viewer support for named destinations varies and
                            # PyMuPDF may not expose their target page; resolve
                            # the name once and store a plain page jump instead.
                            target = named_targets.get(link.get("name") or link.get("nameddest") or "", {}) or {}
                            if isinstance(target.get("page"), int):
                                target_page = target["page"]
                                target_to = target.get("to") or target_to
                                restored = {"kind": pymupdf.LINK_GOTO, "from": raw,
                                            "page": target_page,
                                            "to": pymupdf.Point(*(target_to or (0, 0)))}
                        try:
                            combined[index].insert_link(restored)
                        except (RuntimeError, ValueError):
                            continue
                        if link.get("kind") == pymupdf.LINK_URI:
                            uri = (link.get("uri") or "").strip()
                            if re.match(r"(?:https?|mailto):", uri, re.I):
                                reader_links.append(_reader_link(combined[index], raw, {"kind": "uri", "uri": uri},
                                                                 source_page=number,
                                                                 group=f"restored-{number}-uri-"
                                                                       f"{round(raw.x0, 1)}-{round(raw.y0, 1)}"))
                        elif (not erased_overlap and link.get("kind") in {pymupdf.LINK_GOTO, pymupdf.LINK_NAMED}
                                and isinstance(target_page, int) and 0 <= target_page < len(original)):
                            reader_links.append(_reader_link(
                                combined[index], raw, {"kind": "goto", "target_page": target_page},
                                goto_fraction(original[target_page], target_to), source_page=number,
                                group=f"restored-{number}-goto-{round(raw.x0, 1)}-"
                                      f"{round(raw.y0, 1)}-{target_page}"))
                if reader_links:
                    report["links"] = reader_links
            raw = combined.tobytes(garbage=4, deflate=True)
        report["revision"] = revision
        temporary = cache / (revision + ".tmp")
        temporary.write_bytes(raw)
        temporary.replace(pdf_path)
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return report
