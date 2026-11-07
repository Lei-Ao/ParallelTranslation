"""Locate artwork whose independently positioned labels must stay together.

This is intentionally a preservation detector, not a diagram translator.  It
uses visible artwork and caption geometry; short text alone is never evidence
that a paragraph belongs to a figure.  All coordinates are unrotated.
"""
import re
from statistics import median

import pymupdf

from pdf_flow import visible_drawings


_FIGURE_CAPTION = re.compile(
    r"^\s*(?:fig(?:ure)?\.?\s*(?:[A-Z]?\d+(?:[.\-]\d+)*|[IVXLC]+)|图\s*[A-Z]?\d+)"
    r"(?=$|\s|[.:：、（(])", re.I)
_TABLE_CAPTION = re.compile(
    r"^\s*(?:table\s+(?:[A-Z]?\d+|[IVXLC]+)|表\s*[A-Z]?\d+)(?=$|\s|[.:：、])", re.I)


def is_figure_caption(text):
    return bool(_FIGURE_CAPTION.match(text or ""))


def _overlap(a, b):
    return max(0, min(a.x1, b.x1) - max(a.x0, b.x0)) * max(0, min(a.y1, b.y1) - max(a.y0, b.y0))


def _near(a, b, gap):
    return (a.x0 <= b.x1 + gap and b.x0 <= a.x1 + gap
            and a.y0 <= b.y1 + gap and b.y0 <= a.y1 + gap)


def _text_items(native):
    result = []
    for block in native:
        if block.get("type", 0) != 0:
            continue
        lines = []
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if spans:
                # Some producers paint whitespace in separate spans.  Those
                # spans do not contribute style metrics, but removing them
                # from text would turn a paragraph into one unbroken token.
                lines.append({"text": "".join(s.get("text", "") for s in line.get("spans", [])).strip(),
                              "rect": pymupdf.Rect(line["bbox"]),
                              "size": max(s.get("size", 0) for s in spans),
                              "math": any(re.search(r"math|symbol|cmmi|cmsy|cmex", s.get("font", ""), re.I)
                                          for s in spans)})
        if not lines:
            continue
        text = " ".join(line["text"] for line in lines)
        result.append({"text": text, "rect": pymupdf.Rect(block["bbox"]), "lines": lines,
                       "size": max(line["size"] for line in lines),
                       "caption": "figure" if is_figure_caption(text) else
                                  "table" if _TABLE_CAPTION.match(text) else None})
    return result


def _mark_display_equations(text):
    """A numbered display equation is document structure, not an outside label."""
    numbered = [item for item in text if re.search(r"[（(]\s*\d{1,4}\s*[）)]\s*[.,;]?\s*$", item["text"])]
    for item in text:
        if not re.search(r"[=≤≥]", item["text"]):
            continue
        box = item["rect"]
        for number in numbered:
            other = number["rect"]
            vertical = min(box.y1, other.y1) - max(box.y0, other.y0)
            if (other.x1 >= box.x1 and vertical >= min(box.height, other.height) * .35
                    and other.x0 - box.x1 <= max(120, item["size"] * 12)):
                item["equation"] = number["equation"] = True


def _paragraph(item, region):
    """Dense flowing text is a barrier, even when printed on a background."""
    text = item["text"]
    substantial = len(re.findall(r"\b[A-Za-z]{2,}\b", text)) >= 20 or len(re.findall(r"[\u3400-\u9fff]", text)) >= 40
    wide = sum(line["rect"].width >= region.width * .6 for line in item["lines"])
    return substantial and (wide >= 2 or len(text) >= 240 and wide >= 1)


def _body_barrier(item):
    """Separate artwork from prose, but keep lettered subfigure descriptions."""
    text = item["text"]
    if re.match(r"^\s*\([a-z]\)\s*", text, re.I):
        return False
    words = re.findall(r"\b[A-Za-z]{2,}\b", text)
    function_words = re.findall(r"\b(?:the|of|and|to|in|with|for|is|are|this|that|from)\b", text, re.I)
    heading = re.match(r"^\s*(?:[IVXLC]+\.\s+|\d+(?:\.\d+)*\.?\s+)(.*)", text)
    return (len(words) >= 8 and len(function_words) >= 2
            or len(words) >= 3 and bool(function_words) and bool(re.search(r"[.!?;]\s*$", text))
            or len(re.findall(r"[\u3400-\u9fff]", text)) >= 20
            or bool(heading and re.match(r"[A-Za-z\u3400-\u9fff]", heading[1])
                    and re.search(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", heading[1])))


def _barrier_rects(item):
    if item["caption"]:
        return [item["rect"]]
    # One extracted body block can wrap around a figure.  Its bounding box
    # covers empty space occupied by the figure, but its actual lines do not.
    return [line["rect"] for line in item["lines"]] if _body_barrier(item) or item.get("equation") else []


def _caption_regions(groups, text, size, bounds):
    """Gather disconnected panels, legends and surrounding labels of a figure.

    A caption belongs to the nearest artwork band, rather than only the nearest
    connected path.  Growth stays on that side of the caption and stops at
    intervening prose or another caption in the same horizontal lane.
    """
    result = []
    for caption in (item for item in text if item["caption"] == "figure"):
        box = caption["rect"]
        reach = max(24, caption["size"] * 5)

        def display_barrier(item):
            # A large section title above a diagram is separate document
            # structure.  Small outside titles/axis labels may still join it.
            return (not item["caption"]
                    and item["size"] >= max(caption["size"] * 1.35, caption["size"] + 2)
                    and bool(re.search(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", item["text"]))
                    and not re.match(r"^\s*\([a-z]\)", item["text"], re.I))

        def barrier_rects(item):
            return ([line["rect"] for line in item["lines"]]
                    if display_barrier(item) else _barrier_rects(item))

        barriers = [barrier for item in text if item is not caption
                    for barrier in barrier_rects(item)]
        candidates = []
        for index, group in enumerate(groups):
            region = group["rect"]
            if (region.width < max(size * 3, bounds.width * .035)
                    or region.height < max(size * 1.8, bounds.height * .018)):
                continue
            horizontal = min(box.x1, region.x1) - max(box.x0, region.x0)
            distance = max(box.y0 - region.y1, region.y0 - box.y1, 0)
            if horizontal <= min(box.width, region.width) * .2 or distance > reach:
                continue
            corridor = pymupdf.Rect(max(box.x0, region.x0), min(box.y1, region.y1),
                                     min(box.x1, region.x1), max(box.y0, region.y0))
            if any(_overlap(barrier, corridor) > .1 for barrier in barriers):
                continue
            if any(_paragraph(item, region) for item in text if not item["caption"]
                   and region.contains((item["rect"].tl + item["rect"].br) * .5)):
                continue
            candidates.append((distance, index))
        if not candidates:
            continue
        _, seed = min(candidates)
        region = pymupdf.Rect(groups[seed]["rect"])
        above = region.y0 < box.y0
        lane = pymupdf.Rect(max(bounds.x0, min(box.x0, region.x0) - size), bounds.y0,
                            min(bounds.x1, max(box.x1, region.x1) + size), bounds.y1)

        def in_lane(rect):
            horizontal = min(lane.x1, rect.x1) - max(lane.x0, rect.x0)
            return horizontal >= min(lane.width, rect.width) * .5 and lane.x0 <= (rect.x0 + rect.x1) / 2 <= lane.x1

        barriers = [barrier for item in text if item is not caption
                    for barrier in barrier_rects(item)]

        def blocked(candidate):
            combined = pymupdf.Rect(region) | candidate
            # Test only newly crossed space.  A line inside an established
            # diagram is not a paragraph separating two different figures.
            return any(_overlap(combined, barrier) > .1
                       and _overlap(region, barrier) < .1 for barrier in barriers)

        included = {seed}
        changed = True
        while changed:
            changed = False
            for index, group in enumerate(groups):
                other = group["rect"]
                # A shared legend can be wider than the left-aligned caption
                # and the nearest panel.  Its strong horizontal overlap links
                # the other panels without treating arbitrary nearby text as
                # artwork.  Another caption in that adjacent lane blocks it.
                horizontal = min(region.x1, other.x1) - max(region.x0, other.x0)
                broad = horizontal >= min(region.width, other.width) * .5
                peer = any(item is not caption and item["caption"]
                           and abs(item["rect"].y0 - box.y0) < max(box.height, item["rect"].height) + size
                           and _overlap(pymupdf.Rect(min(region.x0, other.x0), box.y0,
                                                    max(region.x1, other.x1), box.y1), item["rect"]) > .1
                           for item in text)
                if index in included or not in_lane(other) and (not broad or peer):
                    continue
                if max(other.width, other.height) < size * 1.2:
                    continue
                if above and other.y1 > box.y0 + size * .25:
                    continue
                if not above and other.y0 < box.y1 - size * .25:
                    continue
                distance = max(region.y0 - other.y1, other.y0 - region.y1, 0)
                if distance > max(12, size * 3) or blocked(other):
                    continue
                region |= other
                lane.x0 = max(bounds.x0, min(lane.x0, region.x0 - size))
                lane.x1 = min(bounds.x1, max(lane.x1, region.x1 + size))
                included.add(index)
                changed = True
        # Text geometry adds axis labels, an external diagram title, and the
        # (a)/(b) descriptions between panels and the main caption.  This also
        # keeps one extracted block spanning several panels together.
        changed = True
        while changed:
            changed = False
            for item in text:
                other = item["rect"]
                if (item["caption"] or item.get("equation") or _body_barrier(item)
                        or display_barrier(item) or not in_lane(other)):
                    continue
                if region.contains(other):
                    continue
                if above and other.y1 >= box.y0 - .1:
                    continue
                if not above and other.y0 <= box.y1 + .1:
                    continue
                distance = max(region.y0 - other.y1, other.y0 - region.y1, 0)
                horizontal = min(region.x1, other.x1) - max(region.x0, other.x0)
                if distance > max(size, item["size"]) * 2 or horizontal <= 0 or blocked(other):
                    continue
                region |= other
                changed = True
        result.append(region)
    return result


def figure_regions(page, native, drawings=None, images=None):
    """Return conservative artwork boxes, keeping caption blocks translatable.

    Captions attach to the nearest visible artwork, on either side.  Without
    a caption, raster artwork or a connected diagram with several independent
    natural-language labels is sufficient evidence.  Plain paragraph shading,
    page backgrounds, isolated rules and ordinary display equations are not.
    The caller must exclude caption blocks when matching text to these boxes.
    """
    text = _text_items(native)
    _mark_display_equations(text)
    sizes = [line["size"] for item in text for line in item["lines"] if line["size"] > 0]
    size = median(sizes) if sizes else 10
    gap = max(.8, min(3, size * .3))
    bounds = page.rect
    primitives, seen = [], set()

    def add(coordinates, image=False):
        rect = pymupdf.Rect(coordinates)
        if rect.is_infinite or not rect.is_valid:
            return
        rect = pymupdf.Rect(max(bounds.x0, rect.x0), max(bounds.y0, rect.y0),
                            min(bounds.x1, rect.x1), min(bounds.y1, rect.y1))
        if rect.x1 < rect.x0 or rect.y1 < rect.y0:
            return
        # A page-sized image is usually a scan/OCR layer.  Background fills
        # cannot make an entire paper count as one diagram.
        if rect.width >= bounds.width * .9 and rect.height >= bounds.height * .9:
            return
        key = (tuple(round(value, 3) for value in rect), image)
        if key not in seen:
            primitives.append({"rect": rect, "image": image})
            seen.add(key)

    for image in (page.get_image_info() if images is None else images):
        add(image["bbox"], True)
    for drawing in visible_drawings(page) if drawings is None else drawings:
        if drawing.get("rect") is not None:
            add(drawing["rect"])
    if not primitives:
        return []

    # Sweep the x axis before the exact proximity test so disjoint columns do
    # not incur pairwise comparisons or become a single artwork component.
    parents = list(range(len(primitives)))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    active = []
    for index in sorted(range(len(primitives)), key=lambda i: primitives[i]["rect"].x0):
        box = primitives[index]["rect"]
        active = [i for i in active if primitives[i]["rect"].x1 + gap >= box.x0]
        for other in active:
            if _near(box, primitives[other]["rect"], gap):
                parents[root(index)] = root(other)
        active.append(index)
    groups = {}
    for index, primitive in enumerate(primitives):
        key = root(index)
        if key not in groups:
            groups[key] = {"rect": pymupdf.Rect(primitive["rect"]), "count": 0, "image": False}
        group = groups[key]
        group["rect"] |= primitive["rect"]
        group["count"] += 1
        group["image"] |= primitive["image"]
    all_groups = list(groups.values())
    caption_regions = _caption_regions(all_groups, text, size, bounds)
    groups = [group for group in all_groups
              if group["rect"].width >= max(size * 3, bounds.width * .035)
              and group["rect"].height >= max(size * 1.8, bounds.height * .018)]

    anchors = {}
    for caption in (item for item in text if item["caption"]):
        candidates = []
        box = caption["rect"]
        for index, group in enumerate(groups):
            region = group["rect"]
            horizontal = min(box.x1, region.x1) - max(box.x0, region.x0)
            if horizontal <= min(box.width, region.width) * .2:
                continue
            distance = max(box.y0 - region.y1, region.y0 - box.y1, 0)
            if distance > max(18, caption["size"] * 3):
                continue
            corridor = pymupdf.Rect(max(box.x0, region.x0), min(box.y1, region.y1),
                                     min(box.x1, region.x1), max(box.y0, region.y0))
            if any(item is not caption and not item["caption"] and _paragraph(item, region)
                   and _overlap(item["rect"], corridor) > 0 for item in text):
                continue
            candidates.append((distance, index))
        if candidates:
            distance, index = min(candidates)
            anchors.setdefault(index, []).append((distance, caption["caption"]))

    regions = list(caption_regions)
    for index, group in enumerate(groups):
        region = group["rect"]
        inside = [item for item in text if not item["caption"]
                  and region.contains((item["rect"].tl + item["rect"].br) * .5)
                  and _overlap(item["rect"], region) >= item["rect"].get_area() * .5]
        if any(_paragraph(item, region) for item in inside):
            continue
        attached = min(anchors.get(index, ()), default=None)
        if attached and attached[1] == "table":
            continue
        anchored = bool(attached and attached[1] == "figure")
        labels = [line for item in inside for line in item["lines"]
                  if (re.search(r"[A-Za-z]{2,}|[\u3400-\u9fff]{2,}", line["text"])
                      and len(line["text"]) <= 100 and not line["math"]
                      and not re.search(r"[=≤≥∑∫√∏∂∇]", line["text"]))]
        centers = [(line["rect"].x0 + line["rect"].x1) / 2 for line in labels]
        rows = {round((line["rect"].y0 + line["rect"].y1) / (max(size, 1) * .7)) for line in labels}
        complex_diagram = (group["count"] >= 4 and len(labels) >= 3 and len(rows) >= 2
                           and max(centers, default=0) - min(centers, default=0) > size * 3)
        if anchored or group["image"] or complex_diagram:
            regions.append(pymupdf.Rect(region))
    # Caption bands subsume their connected path/image parts.  Keep only the
    # outer boxes so matching and reports do not depend on primitive order.
    unique = []
    for region in sorted(regions, key=lambda rect: rect.get_area(), reverse=True):
        if not any(other.contains(region) for other in unique):
            unique.append(region)
    return sorted(unique, key=lambda rect: (rect.y0, rect.x0))
