"""Recover list paragraph geometry without changing stored text or block IDs."""
import re
import unicodedata

import pymupdf


BULLETS = "•●▪▫▸▹►‣∙⋅※◦■□◆◇·・"
MARKER = re.compile(
    r"^[ \t]*(?P<marker>[" + re.escape(BULLETS) + r"]|"
    r"[（(](?:\d{1,4}|[A-Za-z]|[ivxlcdmIVXLCDM]{1,8}|[一二三四五六七八九十百零〇]+)[）)]|"
    r"(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,8}|[A-Za-z])[.)）、．]|"
    r"[一二三四五六七八九十百零〇]+、)(?!\d)(?=[ \t\w\u3400-\u9fff]|$)")


class ListLayoutError(ValueError):
    pass


def marker_key(value):
    if value in BULLETS:
        return "bullet"
    value = unicodedata.normalize("NFKC", value).strip("().、．").casefold()
    return str(int(value)) if value.isdecimal() else value


def source_list_structure(page, style, rect):
    """Measure marker and body axes from actual characters on visual baselines."""
    if not any(MARKER.match("".join(s["text"] for s in line["spans"]))
               for line in style.get("lines", [])):
        return None
    size = style["size"]
    owned = {tuple(round(v, 2) for v in s["bbox"]) for s in style.get("spans", [])}
    rows = []
    for block in page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if (tuple(round(v, 2) for v in span["bbox"]) not in owned
                        or not size * .75 <= span["size"] <= size * 1.25):
                    continue
                baseline = span["origin"][1]
                row = next((item for item in rows if abs(item["baseline"] - baseline) <= size * .25), None)
                if row is None:
                    row = {"baseline": baseline, "chars": []}
                    rows.append(row)
                row["chars"].extend({**c, "flags": span["flags"]} for c in span["chars"] if c["c"].strip())
    rows.sort(key=lambda row: row["baseline"])
    for row in rows:
        chars = sorted(row["chars"], key=lambda c: c["bbox"][0])
        text, positions, previous = [], [], None
        for char in chars:
            if previous is not None and char["bbox"][0] - previous["bbox"][2] > size * .12:
                text.append(" ")
                positions.append(None)
            text.append(char["c"])
            positions.extend([char] * len(char["c"]))
            previous = char
        row["text"], row["positions"] = "".join(text), positions
        row["left"] = min(c["bbox"][0] for c in chars)
        row["right"] = max(c["bbox"][2] for c in chars)
        row["top"] = min(c["bbox"][1] for c in chars)
        row["bottom"] = max(c["bbox"][3] for c in chars)
    groups = []
    for row in rows:
        match = MARKER.match(row["text"])
        content = next((i for i in range(match.end(), len(row["text"]))
                        if not row["text"][i].isspace()), None) if match else None
        body = row["positions"][content]["bbox"][0] if content is not None else None
        if body is not None and size * .4 <= body - row["left"] <= size * 6:
            groups.append({"marker": match["marker"], "body": body, "rows": [row]})
            continue
        prior = groups[-1] if groups else None
        if prior and prior.get("marker"):
            continuation = (abs(row["left"] - prior["body"]) <= size * .25
                            and row["baseline"] - prior["rows"][-1]["baseline"] <= size * 1.8)
        else:
            continuation = bool(prior and row["baseline"] - prior["rows"][-1]["baseline"] <= size * 1.8)
            if continuation:
                left = min(r["left"] for r in prior["rows"])
                if (row["left"] > left + size * .35
                        and prior["rows"][-1]["right"] < rect.x1 - size):
                    continuation = False
        if continuation:
            prior["rows"].append(row)
        else:
            groups.append({"marker": None, "rows": [row]})
    def marker_family(marker):
        key = marker_key(marker)
        return "bullet" if key == "bullet" else "number" if key.isdecimal() else "letter"

    confirmed = set()
    for index, group in enumerate(groups):
        if not group["marker"]:
            continue
        if len(group["rows"]) > 1:
            confirmed.add(index)
            continue
        for other in (index - 1, index + 1):
            if not 0 <= other < len(groups) or not groups[other]["marker"]:
                continue
            sibling = groups[other]
            if (marker_family(group["marker"]) == marker_family(sibling["marker"])
                    and group["marker"][-1] == sibling["marker"][-1]
                    and abs(group["body"] - sibling["body"]) <= size * .25):
                confirmed.add(index)
    if not confirmed:
        return None  # Run-in headings, decimals and abbreviations are not lists.
    merged = []
    for index, group in enumerate(groups):
        if index not in confirmed:
            group["marker"] = None
            group.pop("body", None)
        if merged and not group["marker"] and not merged[-1]["marker"]:
            merged[-1]["rows"].extend(group["rows"])
        else:
            merged.append(group)
    groups = merged
    distances = sorted(b["baseline"] - a["baseline"] for a, b in zip(rows, rows[1:])
                       if size * .75 <= b["baseline"] - a["baseline"] <= size * 1.8)
    leading = distances[len(distances) // 2] if distances else size * 1.2
    result, previous = [], None
    for group in groups:
        lines = group["rows"]
        left = min(r["left"] for r in lines)
        body = group.get("body", left)
        alignment = "justify" if len(lines) > 1 and all(abs(r["right"] - rect.x1) <= size * .3 for r in lines[:-1]) else "left"
        result.append({"marker": group["marker"], "left": lines[0]["left"] - rect.x0 if group["marker"] else left - rect.x0,
                       "body": body - rect.x0, "indent": max(0, lines[0]["left"] - left),
                       "alignment": alignment,
                       "space_before": max(0, lines[0]["baseline"] - previous - leading) if previous is not None else 0,
                       "marker_bold": bool(lines[0]["positions"][0]["flags"] & 16)})
        previous = lines[-1]["baseline"]
    return result


def translated_list_layout(value, structure, join_lines):
    """Map only unambiguous paragraph boundaries; restore source marker spelling."""
    def compatible(parts):
        if len(parts) != len(structure):
            return False
        for part, source in zip(parts, structure):
            marker = MARKER.match(part.strip())
            if bool(marker) != bool(source["marker"]):
                return False
            if marker and marker_key(marker["marker"]) != marker_key(source["marker"]):
                return False
        return True

    if all(item["marker"] for item in structure):
        starts = [m.start() for m in re.finditer(r"(?m)^[ \t]*\S", value)
                  if MARKER.match(value[m.start():].split("\n", 1)[0])]
        parts = [value[a:b].strip() for a, b in zip(starts, starts[1:] + [len(value)])]
        if starts and value[:starts[0]].strip():
            parts.insert(0, value[:starts[0]].strip())
    else:
        parts = [line.strip() for line in value.splitlines() if line.strip()]
        if not compatible(parts):
            parts = [part.strip() for part in re.split(r"\n[ \t]*\n+", value) if part.strip()]
    if not compatible(parts):
        raise ListLayoutError("无法可靠对应原文列表与后续段落，保留完整原文；请按原条目和段落分行编辑译文后重新排版")
    pieces, paragraphs, offset = [], [], 0
    for part, source in zip(parts, structure):
        part = join_lines(part)
        prefix = 0
        if source["marker"]:
            match = MARKER.match(part)
            part = source["marker"] + " " + part[match.end():].lstrip()
            prefix = len(source["marker"]) + 1
        paragraphs.append({**source, "start": offset, "end": offset + len(part), "prefix_end": offset + prefix})
        pieces.append(part)
        offset += len(part) + 1
    return "\n".join(pieces), paragraphs
