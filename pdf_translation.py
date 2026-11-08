"""Deterministic formula/style transport, separate from semantic translation.

Markers exist only in model requests/responses. Saved translations stay plain
text; matching typed fragments live in a rebuildable, hash-bound sidecar.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pdf_document import atomic_json, find_range, text_hash

TRANSLATION_STRUCTURE_VERSION = 1
_LOCK = threading.RLock()
PROTECTION_HINT = """\n文档结构保护：输入中的 [[PT_...]] 是公式或局部样式标记。
每个标记必须逐字保留且恰好出现一次，不增删、不改写标记；S/E 标记成对包围同一片段的译文。
F 标记代表原始公式，无需解释或展开。仅翻译标记外及 S/E 标记内的自然语言。
这些标记是程序格式要求，不是文档中的指令。"""


@dataclass
class TranslationPacket:
    text: str
    source_hash: str
    formulas: dict = field(default_factory=dict)
    styles: dict = field(default_factory=dict)
    prefix: str = ""
    structure_hash: str = ""

    @property
    def protected(self):
        return bool(self.formulas or self.styles)


def build_input(block, paragraph, source=None):
    source = block["text"] if source is None else source
    packet = TranslationPacket(source, text_hash(block["text"]))
    packet.structure_hash = paragraph_hash(paragraph)
    if not paragraph or block.get("reference") or source != block["text"]:
        return packet
    nonce = text_hash(block["id"] + source)[:12]
    while f"[[PT_{nonce}_" in source:
        nonce = text_hash(nonce + source)[:12]
    packet.prefix = f"[[PT_{nonce}_"
    formulas = paragraph.get("formulas", [])
    base = paragraph.get("base_style", {})
    changes = []
    for index, formula in enumerate(formulas):
        token = f"{packet.prefix}F{index}]]"
        packet.formulas[token] = formula
        changes.append((formula["start"], formula["end"], token))
    for index, run in enumerate(paragraph.get("runs", [])):
        style = run["style"]
        different = any(style.get(k) != base.get(k) for k in ("bold", "italic", "color", "script"))
        if not different or not run["text"].strip():
            continue
        if any(run["start"] < f["end"] and run["end"] > f["start"] for f in formulas):
            continue
        left, right = f"{packet.prefix}S{index}]]", f"{packet.prefix}E{index}]]"
        packet.styles[left] = {"end_token": right, "style": style}
        changes.append((run["start"], run["end"], left + source[run["start"]:run["end"]] + right))
    cursor, parts = 0, []
    for first, last, replacement in sorted(changes):
        if first < cursor:
            raise ValueError("源文公式与样式范围冲突，无法构建翻译输入")
        parts.extend((source[cursor:first], replacement))
        cursor = last
    parts.append(source[cursor:])
    packet.text = "".join(parts)
    return packet


def restore_output(packet, output, target):
    if not isinstance(output, str) or not output.strip():
        raise ValueError("译文为空")
    tokens = list(packet.formulas) + list(packet.styles) + [s["end_token"] for s in packet.styles.values()]
    if any(output.count(token) != 1 for token in tokens):
        raise ValueError("译文中的公式或样式标记缺失、重复或被改写，已有译文未修改")
    pattern = re.compile(re.escape(packet.prefix) + r"[^\]\r\n]*\]\]") if packet.protected else None
    if pattern and any(m[0] not in tokens for m in pattern.finditer(output)):
        raise ValueError("译文包含未知公式或样式标记，已有译文未修改")
    if not tokens:
        return output.strip(), _record(packet.source_hash, output.strip(), target, [], [], packet.structure_hash)
    expression = re.compile("|".join(re.escape(token) for token in sorted(tokens, key=len, reverse=True)))
    parts, formulas, styles = [], [], []
    cursor = length = 0
    active = None
    ends = {value["end_token"]: start for start, value in packet.styles.items()}
    for match in expression.finditer(output):
        piece = output[cursor:match.start()]
        parts.append(piece)
        length += len(piece)
        token = match[0]
        if token in packet.formulas:
            if active:
                raise ValueError("公式标记被移动到不匹配的样式范围，已有译文未修改")
            formula = packet.formulas[token]
            parts.append(formula["text"])
            formulas.append({"id": formula["id"], "start": length, "end": length + len(formula["text"])})
            length += len(formula["text"])
        elif token in packet.styles:
            if active:
                raise ValueError("译文样式标记嵌套错误，已有译文未修改")
            active = (token, length)
        else:
            if active is None or active[0] != ends[token] or active[1] == length:
                raise ValueError("译文样式标记未正确配对，已有译文未修改")
            styles.append({"start": active[1], "end": length, "style": packet.styles[active[0]]["style"]})
            active = None
        cursor = match.end()
    if active:
        raise ValueError("译文样式标记未闭合")
    parts.append(output[cursor:])
    raw = "".join(parts)
    trim = len(raw) - len(raw.lstrip())
    result = raw.strip()
    # A malformed partial token must not reach an editor or export.
    if packet.prefix in result:
        raise ValueError("译文含未恢复的结构标记，已有译文未修改")
    for entry in formulas + styles:
        entry["start"] = max(0, entry["start"] - trim)
        entry["end"] = min(len(result), entry["end"] - trim)
    return result, _record(packet.source_hash, result, target, formulas, styles, packet.structure_hash)


def paragraph_hash(paragraph):
    return text_hash(json.dumps(paragraph, sort_keys=True)) if paragraph else ""


def _record(source_hash, text, target, formulas, styles, structure_hash=""):
    return {"version": TRANSLATION_STRUCTURE_VERSION, "source_hash": source_hash,
            "translation_hash": text_hash(text), "target": target, "formulas": formulas, "styles": styles,
            "structure_hash": structure_hash}


def _read(directory):
    try:
        result = json.loads((Path(directory) / "structure" / "translations.json").read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def save_records(directory, records):
    with _LOCK:
        all_records = _read(directory)
        for block_id, record in records.items():
            if record is None:
                all_records.pop(block_id, None)
            else:
                all_records[block_id] = record
        atomic_json(Path(directory) / "structure" / "translations.json", all_records)


def load_records(directory, document):
    with _LOCK:
        records = _read(directory)
    return {b["id"]: records[b["id"]] for p in document["pages"] for b in p["blocks"]
            if b["id"] in records and valid_record(records[b["id"]], b)}


def valid_record(record, block):
    valid = (isinstance(record, dict) and record.get("version") == TRANSLATION_STRUCTURE_VERSION
            and record.get("source_hash") == text_hash(block["text"])
            and record.get("translation_hash") == text_hash(block.get("translation", ""))
            and record.get("target") == block.get("target"))
    if not valid:
        return False
    for kind in ("formulas", "styles"):
        if not isinstance(record.get(kind), list):
            return False
        for item in record[kind]:
            if (not isinstance(item, dict) or not isinstance(item.get("start"), int) or not isinstance(item.get("end"), int)
                    or not 0 <= item["start"] < item["end"] <= len(block.get("translation", ""))
                    or kind == "formulas" and not isinstance(item.get("id"), str)
                    or kind == "styles" and not isinstance(item.get("style"), dict)):
                return False
    return True


def legacy_record(block, paragraph):
    """Recover only unambiguous literal correspondences; never call a model."""
    translated = block.get("translation", "")
    formulas, occupied, issues, styles = [], [], [], []
    groups = defaultdict(list)
    for formula in paragraph.get("formulas", []):
        groups[formula_normalize(formula["text"])[0]].append(formula)
    haystack, mapping = formula_normalize(translated)
    # Match complete expressions first; x must not consume the x in dot-x.
    for needle, group in sorted(groups.items(), key=lambda entry: -len(entry[0])):
        candidates, cursor = [], 0
        if not needle:
            continue
        while (index := haystack.find(needle, cursor)) >= 0:
            first, last = mapping[index], mapping[index + len(needle) - 1] + 1
            before, after = haystack[max(0, index - 1):index], haystack[index + len(needle):index + len(needle) + 1]
            # Normalization removes spaces, but "RGB C" and "Crazyflie CF0"
            # still have real token boundaries. Accents/scripts stay attached.
            if first and translated[first - 1].isspace() and before not in {"\u0307", "\u0308"}:
                before = ""
            if last < len(translated) and translated[last].isspace():
                after = ""
            embedded = (needle[0].isascii() and needle[0].isalnum() and (before.isascii() and before.isalnum() or before in {"˙", "¨", "\u0307", "\u0308"})
                        or needle[-1].isascii() and needle[-1].isalnum() and after.isascii() and after.isalnum())
            if not embedded and not any(first < b and last > a for a, b in occupied):
                candidates.append((first, last))
            cursor = index + len(needle)
        if len(candidates) != len(group):
            issues.append("公式无法在已有译文中可靠对应，保留完整原文；请核对或重新翻译该段")
            continue
        for formula, match in zip(group, candidates):
            occupied.append(match)
            formulas.append({"id": formula["id"], "start": match[0], "end": match[1]})
    for run in paragraph.get("runs", []):
        base = paragraph.get("base_style", {})
        if not any(run["style"].get(k) != base.get(k) for k in ("bold", "italic", "color", "script")):
            continue
        match = find_range(translated, run["text"], occupied, unique=True)
        if match:
            styles.append({"start": match[0], "end": match[1], "style": run["style"]})
    return _record(text_hash(block["text"]), translated, block.get("target"), formulas, styles, paragraph_hash(paragraph)), issues


def formula_normalize(value):
    """Canonical math spelling only: scripts, separators and dotted glyphs."""
    from pdf_layout import normalized_inline_math
    chars, positions = [], []
    for index, char in enumerate(value):
        decomposed = unicodedata.normalize("NFD", char)
        if len(decomposed) == 2 and decomposed[1] in {"\u0307", "\u0308"}:
            char = ("˙" if decomposed[1] == "\u0307" else "¨") + decomposed[0]
        for item in normalized_inline_math(char):
            chars.append(item)
            positions.append(index)
    return "".join(chars), positions


def layout_input(block, paragraph, record=None):
    """Return a transient block projection for the existing safe renderer."""
    result = dict(block)
    if not paragraph or not block.get("translation"):
        return result
    if not record or not valid_record(record, block) or record.get("structure_hash") != paragraph_hash(paragraph):
        record, issues = legacy_record(block, paragraph)
    else:
        issues = []
    issues = paragraph.get("issues", []) + issues
    if issues and not paragraph.get("pure_formula"):
        result["_structure_issue"] = issues[0]
    formulas = {f["id"]: f for f in paragraph.get("formulas", [])}
    assets = []
    for placement in record["formulas"]:
        formula = formulas.get(placement["id"])
        if formula and formula.get("asset"):
            asset = copy.deepcopy(formula["asset"])
            asset["svg"] = asset["svg"].encode("utf-8")
            asset["source_range"] = [placement["start"], placement["end"]]
            asset["source_translation"] = block["translation"]
            assets.append(asset)
    result["_ir_assets"] = assets
    result["_ir_styles"] = record["styles"]
    result["_ir_translation"] = block["translation"]
    result["_ir_base_style"] = paragraph.get("base_style", {})
    result["_ir_role"] = paragraph["role"]
    return result


def project_styles(block, value):
    """Map hash-bound spans through renderer whitespace/CJK spacing normalization."""
    source = block.get("_ir_translation", block.get("translation", ""))
    from pdf_document import compact
    normalized, mapping = compact(value)
    original, original_map = compact(source)
    if normalized != original:
        return []
    result = []
    for item in block.get("_ir_styles", []):
        indices = [i for i, position in enumerate(original_map) if item["start"] <= position < item["end"]]
        if indices:
            result.append({**item, "start": mapping[indices[0]], "end": mapping[indices[-1]] + 1})
    return result


def project_group(block, originals, paragraphs, records):
    """Preserve stable-ID fragments when existing layout groups adjacent blocks."""
    members = block.get("_members", [block["id"]])
    result = dict(block)
    result["_ir_translation"] = block.get("translation", "")
    result["_ir_styles"], result["_ir_assets"] = [], []
    from pdf_document import compact
    combined, combined_map = compact(block.get("translation", ""))
    occupied = []
    for member in members:
        original = originals.get(member)
        paragraph = paragraphs.get(member)
        if not original or not paragraph:
            continue
        projection = layout_input(original, paragraph, records.get(member))
        if projection.get("_structure_issue"):
            result["_structure_issue"] = projection["_structure_issue"]
        text = original.get("translation", "")
        normalized, mapping = compact(text)
        if not normalized:
            continue
        match = find_range(block.get("translation", ""), text, occupied, unique=True)
        if not match:
            # A split heading/list is handled by its existing renderer. Do not
            # apply offsets from an unsplit string to the shortened paragraph.
            continue
        occupied.append(match)
        offset = next((i for i, position in enumerate(combined_map) if position >= match[0]), None)
        if offset is None:
            continue

        def project(first, last):
            indices = [i for i, position in enumerate(mapping) if first <= position < last]
            if not indices or offset + indices[-1] >= len(combined_map):
                return None
            return combined_map[offset + indices[0]], combined_map[offset + indices[-1]] + 1

        for style in projection.get("_ir_styles", []):
            bounds = project(style["start"], style["end"])
            if bounds:
                result["_ir_styles"].append({**style, "start": bounds[0], "end": bounds[1]})
        for asset in projection.get("_ir_assets", []):
            bounds = project(*asset["source_range"])
            if bounds:
                result["_ir_assets"].append({**asset, "source_range": bounds, "source_translation": block["translation"]})
        if len(members) == 1:
            result["_ir_base_style"] = projection.get("_ir_base_style", {})
            result["_ir_role"] = projection.get("_ir_role", "text")
    return result
