"""Column-local flow and lossless relocation of original formula fragments."""
import re

import pymupdf


def overlap(a, b):
    return max(0, min(a.x1, b.x1) - max(a.x0, b.x0)) * max(0, min(a.y1, b.y1) - max(a.y0, b.y0))


def text_ink_bounds(operations, region):
    """Use painted text bounds only when paint operations belong to one region.

    Bboxlog includes a small antialiasing margin, consistently on source and
    translation. Cross-region operators are ambiguous and retain legacy spacing.
    Empty space glyphs and invisible OCR/search text do not measure visible ink.
    """
    bounds = None
    allowance = region + (-1.5, -1.5, 1.5, 1.5)
    for kind, coordinates, *_ in operations:
        if kind not in {"fill-text", "stroke-text"}:
            continue
        box = pymupdf.Rect(coordinates)
        if box.is_empty or not overlap(box, region):
            continue
        if not allowance.contains(box):
            return None
        bounds = box if bounds is None else bounds | box
    return bounds


def heading(text):
    # Section numbers are often extracted on their own line ("1 \nIntroduction"),
    # so the separating whitespace must be allowed to span line breaks.
    return len(text.strip()) <= 120 and bool(re.match(
        r"^\s*(?:[A-Z]\.\s+|[IVXLC]+\.\s+|\d+(?:\.\d+)*\.?\s+)\S", text))


def lane_options(box, lanes, allowance=8):
    choices = []
    for index, (left, right) in enumerate(lanes):
        # Short prose may end before the equation number at the column edge.
        # Permit that small overhang without ever entering the next column.
        next_column = min((start for start, _ in lanes if start > right), default=float("inf"))
        limit = min(right + allowance, next_column - 8)
        if box.x0 >= left - 8 and box.x1 <= limit:
            choices.append((index, right - left))
    return choices


def visible_drawings(page):
    """Use the visible intersection of each path and its enclosing PDF clips."""
    clips, drawings = [], []
    for drawing in page.get_drawings(extended=True):
        level = drawing.get("level", 0)
        clips = [(depth, box) for depth, box in clips if depth < level]
        if drawing["type"] == "clip":
            clips.append((level, pymupdf.Rect(drawing["scissor"])))
            continue
        if drawing["type"] not in {"s", "f", "fs"} or "rect" not in drawing:
            continue
        box = pymupdf.Rect(drawing["rect"])
        for _, clip in clips:
            # Do not use Rect intersection for a zero-height/width stroke:
            # a visible horizontal or vertical line is still an obstacle.
            if box.x1 < clip.x0 or box.x0 > clip.x1 or box.y1 < clip.y0 or box.y0 > clip.y1:
                break
            box = pymupdf.Rect(max(box.x0, clip.x0), max(box.y0, clip.y0),
                               min(box.x1, clip.x1), min(box.y1, clip.y1))
        else:
            drawings.append({**drawing, "rect": box})
    return drawings


def original_formula_units(page, overlays, candidates, native, graphics, lanes=(), occupied_spans=(), glyph_bounds=None):
    """Copy safe display equations as vector crops, never re-typeset mathematics.

    Fragmented rows, fraction bars and equation labels travel as one unit.
    Crops touching foreign text, images or partially crossing paths stay fixed.
    """
    spans = [span for item in native for line in item["lines"] for span in line["spans"] if span["text"].strip()]
    images = [pymupdf.Rect(item["bbox"]) for item in page.get_image_info()]
    mid = page.rect.width / 2
    lines = [pymupdf.Rect(line["bbox"]) for item in native for line in item["lines"]]
    double_column = (any(box.width > page.rect.width * .2 and box.x1 <= mid + 6 for box in lines)
                     and any(box.width > page.rect.width * .2 and box.x0 >= mid - 6 for box in lines))
    groups = []
    def lane_index(box):
        choices = lane_options(box, lanes, allowance=36)
        return min(choices, key=lambda item: item[1])[0] if choices else None

    for block, box in candidates:
        members, region = [block], pymupdf.Rect(box)
        touching = []
        for index, (other_members, other_box) in enumerate(groups):
            horizontal = min(region.x1, other_box.x1) - max(region.x0, other_box.x0)
            vertical_gap = max(region.y0, other_box.y0) - min(region.y1, other_box.y1)
            label = any(re.fullmatch(r"\s*\(\d+\)\s*", member["text"]) for member in members + other_members)
            aligned_label = label and vertical_gap <= 0 and horizontal > -page.rect.width * .45
            first_lane, second_lane = lane_index(region), lane_index(other_box)
            cross_column = ((first_lane != second_lane) if first_lane is not None and second_lane is not None
                            else double_column and ((region.x1 <= mid and other_box.x0 >= mid)
                                                    or (other_box.x1 <= mid and region.x0 >= mid)))
            if not cross_column and (horizontal > -16 or aligned_label) and vertical_gap <= 8:
                touching.append(index)
                members.extend(other_members)
                region |= other_box
        for index in reversed(touching):
            groups.pop(index)
        groups.append((members, region))
    prepared, snapshot = [], None
    occupied = {id(span) for span in occupied_spans}
    for members, region in groups:
        if all(member.get("_math_fragment") for member in members):
            continue
        owned = [span for span in spans if region.contains(pymupdf.Rect(span["bbox"]).tl + (pymupdf.Rect(span["bbox"]).br - pymupdf.Rect(span["bbox"]).tl) * .5)]
        if not owned or any(id(span) in occupied for span in owned):
            continue
        crop = pymupdf.Rect(region)
        for span in owned:
            crop |= pymupdf.Rect(span["bbox"])
        crop = (crop + (-.75, -.75, .75, .75)) & page.rect
        own_ids = {id(span) for span in owned}
        foreign = [pymupdf.Rect(span["bbox"]) for span in spans if id(span) not in own_ids]
        if any(overlap(crop, box) > .1 for box in foreign):
            measured = glyph_bounds(owned) if glyph_bounds is not None else None
            if measured is None:
                continue
            measured = pymupdf.Rect(measured)
            for drawing in graphics:
                padded = pymupdf.Rect(drawing) + (-.2, -.2, .2, .2)
                if crop.contains(padded):
                    measured |= padded
            crop = (measured + (-.2, -.2, .2, .2)) & page.rect
            if any(overlap(crop, box) > .1 for box in foreign):
                continue
        if any(overlap(crop, image) > .1 for image in images):
            continue
        carried = []
        unsafe = False
        for drawing in graphics:
            padded = pymupdf.Rect(drawing) + (-.2, -.2, .2, .2)
            if overlap(crop, padded) <= .1:
                continue
            if not crop.contains(padded):
                unsafe = True
                break
            carried.append(tuple(drawing))
        if unsafe:
            continue
        if snapshot is None:
            snapshot = len(overlays)
            with pymupdf.open() as vector_source:
                vector_source.insert_pdf(page.parent, from_page=page.number, to_page=page.number, links=False, annots=False)
                vector_page = vector_source[0]
                for image in images:
                    vector_page.add_redact_annot(image, fill=False, cross_out=False)
                if images:
                    vector_page.apply_redactions(images=1, graphics=0, text=1)
                    vector_page.clean_contents()
                overlays.insert_pdf(vector_source, links=False, annots=False)
        block = {**members[0], "text": " ".join(member["text"] for member in members),
                 "_members": [member["id"] for member in members], "_formula": True}
        style = {"spans": owned, "size": max(8, min(12, max(span["size"] for span in owned))), "lines": []}
        result = {"overlay": snapshot, "rect": list(crop), "clip": list(crop), "scale": 1,
                  "original": True, "carried_graphics": carried}
        prepared.append((block, style, result))
    return prepared


def column_lanes(prepared, source_rects, page_width, prose):
    """Discover body columns from source prose, independently of column count."""
    lanes = []
    for block, style, _ in prepared:
        if block.get("_cover_metadata") or not prose(block["text"]) or caption(block):
            continue
        box = source_rects[block["id"]]
        if box.width < max(65, page_width * .12):
            continue
        tolerance = max(6, style["size"] * .8)
        lane = next((item for item in lanes if abs(item[0] - box.x0) <= tolerance
                     and min(item[1] - item[0], box.width) >= max(item[1] - item[0], box.width) * .7), None)
        if lane is None:
            lanes.append([box.x0, box.x1])
        else:
            lane[0], lane[1] = min(lane[0], box.x0), max(lane[1], box.x1)
    return lanes


def caption(block):
    return bool(re.match(r"^\s*(?:fig(?:ure)?[.\s]|table\b|algorithm\b|图\s*\d|表\s*\d)", block["text"], re.I))


def reflow_columns(prepared, native, graphics, source_rects, page_width, prose):
    """Remove translation-created gaps while retaining source paragraph spacing.

    Columns and fixed artwork are independent. Equations travel as intact vector
    crops with their labels; large intentional source gaps start a new segment.
    """
    lanes = column_lanes(prepared, source_rects, page_width, prose)
    sizes = sorted((style["size"], len(block["text"])) for block, style, _ in prepared
                   if prose(block["text"]) and not block.get("_cover_metadata") and not caption(block))
    total = sum(weight for _, weight in sizes)
    accumulated, body_size = 0, 10
    for size, weight in sizes:
        accumulated += weight
        if accumulated >= total / 2:
            body_size = size
            break
    owned = {id(span) for _, style, _ in prepared for span in style["spans"]}
    fixed = [pymupdf.Rect(span["bbox"]) for item in native for line in item["lines"]
             for span in line["spans"] if span["text"].strip() and id(span) not in owned]
    carried = {tuple(box) for _, _, result in prepared for box in result.get("carried_graphics", [])}
    fixed.extend(pymupdf.Rect(box) + (-.3, -.3, .3, .3) for box in graphics if tuple(box) not in carried)
    per_lane = {}
    for item in prepared:
        block, style, result = item
        if block.get("_cover_metadata") or style["size"] < body_size * .75:
            continue
        text = block["text"]
        if not (prose(text) or heading(text) or caption(block) or block.get("_formula")
                or len(re.findall(r"[A-Za-z\u3400-\u9fff]", text)) >= 18):
            continue
        box = source_rects[block["id"]]
        choices = lane_options(box, lanes, allowance=36 if block.get("_formula") else 8)
        if choices:
            lane = min(choices, key=lambda pair: pair[1])[0]
            per_lane.setdefault(lane, []).append(item)
    adjustments = []
    for items in per_lane.values():
        items.sort(key=lambda item: (source_rects[item[0]["id"]].y0, source_rects[item[0]["id"]].x0))
        anchored_headings = set()
        for index in reversed(range(len(items))):
            block, style, result = items[index]
            if not heading(block["text"]) or block.get("_formula"):
                continue
            source = source_rects[block["id"]]
            following = items[index + 1] if index + 1 < len(items) else None
            attached = False
            if following and not caption(following[0]) and following[0]["id"] not in anchored_headings:
                next_source = source_rects[following[0]["id"]]
                corridor = pymupdf.Rect(min(source.x0, next_source.x0), source.y1,
                                         max(source.x1, next_source.x1), max(source.y1, next_source.y0))
                attached = (next_source.y0 - source.y1 <= max(24, style["size"] * 2.5)
                            and not any(overlap(corridor, obstacle) > .1 for obstacle in fixed))
            if not attached:
                anchored_headings.add(block["id"])
        previous = None
        for block, style, result in items:
            source = source_rects[block["id"]]
            if caption(block) or block["id"] in anchored_headings:
                previous = (block, style, result)
                continue  # Keep captions and headings attached to fixed content.
            if previous is not None:
                prior, prior_style, prior_result = previous
                anchor = source_rects[prior["id"]]
                source_gap = source.y0 - anchor.y1
                size = max(style["size"], prior_style["size"])
                if -max(3, size * .3) <= source_gap <= max(24, size * 2.5):
                    # Retain measured source spacing, including heading and
                    # equation gaps, instead of imposing a uniform small gap.
                    gap = max(source_gap, 1.5, size * .2)
                    ink_gap = None
                    ink_padding = None
                    if (style.get("ink_bounds") and prior_style.get("ink_bounds")
                            and result.get("ink_insets") is not None
                            and prior_result.get("ink_insets") is not None):
                        ink_gap = style["ink_bounds"][1] - prior_style["ink_bounds"][3]
                        ink_padding = prior_result["ink_insets"][1] + result["ink_insets"][0]
                        # Match the painted gap, subtracting each translated
                        # font frame's empty edges. Never overlap layout boxes.
                        gap = max(1.5, size * .2, ink_gap - ink_padding)
                    box = pymupdf.Rect(result["rect"])
                    top = pymupdf.Rect(prior_result["rect"]).y1 + gap
                    if top < box.y0 - 2:
                        # Moving up only crosses the strip above the old top.
                        # Content below the shortened block no longer blocks it;
                        # keeping the old bottom in the collision test left a
                        # gap under headings whose following text could move.
                        moved = pymupdf.Rect(box.x0, top, box.x1, top + box.height)
                        swept = pymupdf.Rect(box.x0, top, box.x1, max(box.y0, moved.y1))
                        other_text = [pymupdf.Rect(other["rect"]) for _, _, other in prepared if other is not result]
                        if not any(overlap(swept, obstacle) > .1 for obstacle in fixed + other_text):
                            result["rect"] = [moved.x0, moved.y0, moved.x1, moved.y1]
                            adjustments.append({"blocks": block.get("_members", [block["id"]]),
                                                "previous": prior.get("_members", [prior["id"]]),
                                                "previous_bottom": pymupdf.Rect(prior_result["rect"]).y1,
                                                "source_gap": source_gap, "gap": gap,
                                                "source_ink_gap": ink_gap, "ink_padding": ink_padding,
                                                "shift": top - box.y0})
            previous = (block, style, result)
    return adjustments
