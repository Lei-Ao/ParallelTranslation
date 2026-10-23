"""Recover decorative initials using source geometry before translation."""
import copy
import re

import pymupdf

from pdf_flow import overlap, visible_drawings


DROP_CAP_SCHEMA = 1
MATH_FONT = re.compile(r"math|symbol|cmmi|cmsy|cmex|msam|msbm|stmary|rsfs", re.I)


def _source_rect(page, block):
    x0, y0, x1, y1 = block["bbox"]
    return pymupdf.Rect(x0 * page.rect.width, y0 * page.rect.height,
                        x1 * page.rect.width, y1 * page.rect.height) * page.derotation_matrix


def _display_rect(page, rect):
    box = rect * page.rotation_matrix
    return [box.x0 / page.rect.width, box.y0 / page.rect.height,
            box.x1 / page.rect.width, box.y1 / page.rect.height]


def _without_initial(text, initial):
    """Only remove an independently extracted line, never a letter inside a word."""
    lines = text.splitlines()
    indices = [i for i, line in enumerate(lines) if line.strip() == initial]
    if len(indices) != 1:
        return None
    return "\n".join(lines[:indices[0]] + lines[indices[0] + 1:]).strip()


def _drop_caps(page, native):
    lines = [line for block in native if block.get("type") == 0 for line in block["lines"]
             if abs(line.get("dir", (1, 0))[0] - 1) < .01
             and abs(line.get("dir", (1, 0))[1]) < .01]
    candidates = []
    for line in lines:
        spans = [s for s in line["spans"] if s["text"].strip()]
        if (len(spans) != 1 or len(spans[0]["text"].strip()) != 1
                or not spans[0]["text"].strip().isalpha()
                or MATH_FONT.search(spans[0]["font"])):
            continue
        initial = spans[0]
        box = pymupdf.Rect(initial["bbox"])
        matches = []
        for first in lines:
            text = "".join(s["text"] for s in first["spans"]).strip()
            if not re.match(r"[^\W\d_]+(?:[,;:!?]\s*|\s+)[^\W\d_]", text):
                continue
            ordinary = [s for s in first["spans"] if s["text"].strip()]
            size = max((s["size"] for s in ordinary), default=0)
            rect = pymupdf.Rect(first["bbox"])
            gap = rect.x0 - box.x1
            if not (size > 0 and initial["size"] >= size * 1.65
                    and box.height >= size * 1.65 and box.width <= size * 4
                    and -.1 * size <= gap <= size * .65
                    and abs(box.y0 - rect.y0) <= size * .65
                    and box.y1 > rect.y1 + size * .4):
                continue
            wrapped = [other for other in lines if other is not first
                       and size * .7 <= other["bbox"][1] - rect.y0 <= size * 1.8
                       and abs(other["bbox"][0] - rect.x0) <= size * .4
                       and other["bbox"][1] < box.y1
                       and len(re.findall(r"[^\W\d_]", "".join(s["text"] for s in other["spans"]))) >= 10]
            if wrapped:
                spaces = [s["bbox"][2] - s["bbox"][0] for row in [first, wrapped[0]]
                          for s in row["spans"] if s["text"].isspace() and s["bbox"][2] > s["bbox"][0]]
                word_space = min(spaces, default=size * .25)
                # A decorative initial may itself be a complete word. Retain a
                # word-sized source gap rather than turning "A method" into "Amethod".
                separator = " " if gap >= word_space * .85 else ""
                matches.append((first, separator))
        if len(matches) == 1:
            candidates.append((initial, *matches[0]))
    if not candidates:
        return []
    rotation = page.rotation
    page.set_rotation(0)
    try:
        graphics = [pymupdf.Rect(image["bbox"]) for image in page.get_image_info()]
        graphics += [pymupdf.Rect(drawing["rect"]) for drawing in visible_drawings(page)]
    finally:
        page.set_rotation(rotation)
    return [(initial, first, separator) for initial, first, separator in candidates
            if not any(overlap(pymupdf.Rect(initial["bbox"]), box) > .1
                       or overlap(pymupdf.Rect(first["bbox"]), box) > .1 for box in graphics)]


def normalize_drop_caps(document, pdf):
    """Restore whole words without renumbering body paragraphs or discarding translations.

    Original affected records are retained for recovery. A glyph-only record has
    no independent paragraph meaning; its former ID remains an explicit alias.
    """
    if document.get("drop_cap_schema") == DROP_CAP_SCHEMA:
        return False
    changed = False
    for saved_page, page in zip(document["pages"], pdf):
        native = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
        for initial, first, separator in _drop_caps(page, native):
            blocks = saved_page["blocks"]
            glyph = initial["text"].strip()
            cap_rect, first_rect = pymupdf.Rect(initial["bbox"]), pymupdf.Rect(first["bbox"])
            first_word = re.match(r"[^\W\d_]+", "".join(s["text"] for s in first["spans"]).strip())
            if first_word is None:
                continue
            owners = [b for b in blocks if overlap(_source_rect(page, b), cap_rect) >= cap_rect.get_area() * .8
                      and _without_initial(b["text"], glyph) is not None]
            bodies = [b for b in blocks if overlap(_source_rect(page, b), first_rect) >= first_rect.get_area() * .8
                      and not re.search(r"reference|table|figure|caption|formula|equation", b.get("kind", ""), re.I)
                      and (b["text"].lstrip().startswith(first_word[0])
                           or (_without_initial(b["text"], glyph) or "").lstrip().startswith(first_word[0]))]
            if len(owners) != 1 or len(bodies) != 1:
                continue
            owner, body = owners[0], bodies[0]
            remainder = _without_initial(owner["text"], glyph)
            if remainder is None:
                continue
            body_text = remainder if owner is body else body["text"]
            if not body_text.lstrip().startswith(first_word[0]):
                continue
            owner_translation = _without_initial(owner.get("translation", ""), glyph)
            if owner.get("edited") and (owner_translation is not None
                                        or not remainder and owner.get("translation", "").strip()):
                owner["drop_cap_preserve"] = body["drop_cap_preserve"] = "首字下沉与手工译文混排，已保留完整原文；请核对并编辑相关译文后重试"
                changed = True
                continue
            retained = []
            if owner is not body and remainder:
                source_text = "".join(remainder.split())
                retained = [pymupdf.Rect(line["bbox"]) for block in native if block.get("type") == 0
                            for line in block["lines"]
                            if (line_text := "".join("".join(s["text"] for s in line["spans"]).split()))
                            and line_text in source_text
                            and overlap(_source_rect(page, owner), pymupdf.Rect(line["bbox"]))
                            >= pymupdf.Rect(line["bbox"]).get_area() * .8
                            and all(span is not initial for span in line["spans"])]
                if not retained:
                    owner["drop_cap_preserve"] = body["drop_cap_preserve"] = "无法可靠分离首字下沉与相邻文字，保留完整原文"
                    changed = True
                    continue
            legacy = document.setdefault("drop_cap_legacy", [])
            backed_up = {b["id"] for b in legacy}
            for block in (owner, body):
                if block["id"] not in backed_up:
                    legacy.append(copy.deepcopy(block))
                    backed_up.add(block["id"])
            body_rect = _source_rect(page, body) | cap_rect
            if owner is not body:
                if remainder:
                    owner["text"] = remainder
                    region = pymupdf.Rect(retained[0])
                    for box in retained[1:]:
                        region |= box
                    owner["bbox"] = _display_rect(page, region)
                    if owner_translation is not None:
                        owner["translation"] = owner_translation
                else:
                    document.setdefault("drop_cap_aliases", {})[owner["id"]] = body["id"]
                    blocks.remove(owner)
            elif owner_translation is not None:
                body["translation"] = owner_translation
            body["text"] = glyph + separator + body_text.lstrip()
            body["bbox"] = _display_rect(page, body_rect)
            body["drop_cap"] = {"initial": glyph, "separator": separator, "bbox": _display_rect(page, cap_rect)}
            owner.pop("drop_cap_preserve", None)
            body.pop("drop_cap_preserve", None)
            if body.get("translation"):
                body["drop_cap_review"] = True
            changed = True
    if changed and "total_blocks" in document:
        document["total_blocks"] = sum(len(page["blocks"]) for page in document["pages"])
    pending = any(b.get("drop_cap_preserve") for p in document["pages"] for b in p["blocks"])
    document["drop_cap_schema"] = 0 if pending else DROP_CAP_SCHEMA
    return True
