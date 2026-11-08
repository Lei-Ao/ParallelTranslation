"""Versioned, rebuildable source structure shared by translation and layout.

Geometry uses unrotated crop-local PDF points. Existing block IDs and saved
texts remain the external contract. No translations are written here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

import pdf_model
from performance import count, span

STRUCTURE_VERSION = 5
_LOCK = threading.RLock()
_PROGRESS = {}
_CACHE = OrderedDict()
_CACHE_SIZES = {}
_HASH_CACHE = OrderedDict()


@dataclass
class TextStyle:
    font: str
    size: float
    bold: bool = False
    italic: bool = False
    color: int = 0
    script: str = ""


@dataclass
class TextRun:
    start: int
    end: int
    text: str
    bbox: list
    baseline: float
    style: dict
    character_ids: list = field(default_factory=list)


@dataclass
class FormulaObject:
    id: str
    start: int
    end: int
    text: str
    bbox: list
    baseline: float
    character_ids: list
    asset: dict | None = None


@dataclass
class ParagraphStructure:
    block_id: str
    source_hash: str
    bbox: list
    role: str
    base_style: dict
    runs: list
    formulas: list
    pure_formula: bool = False
    issues: list = field(default_factory=list)
    member_ids: list = field(default_factory=list)


@dataclass
class PageStructure:
    version: int
    number: int
    width: float
    height: float
    rotation: int
    cropbox: list
    rotation_matrix: list
    derotation_matrix: list
    model: dict
    regions: list
    paragraphs: list
    native: list
    raw: list
    obstacles: list
    reading_order: list
    issues: list = field(default_factory=list)
    preserve_page: bool = False
    characters: list = field(default_factory=list)


def text_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def _remember(key, path, value):
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    _CACHE_SIZES[key] = path.stat().st_size
    while len(_CACHE) > 8 or sum(_CACHE_SIZES.get(k, 0) for k in _CACHE) > 16 * 1024 * 1024:
        evicted, _ = _CACHE.popitem(last=False)
        _CACHE_SIZES.pop(evicted, None)


def compact(value):
    """Index-preserving normalization, never a fuzzy semantic match."""
    chars, positions = [], []
    for index, char in enumerate(value):
        for item in unicodedata.normalize("NFKC", char):
            if not item.isspace() and item != "\u00ad":
                chars.append(item)
                positions.append(index)
    return "".join(chars), positions


def find_range(source, text, occupied=(), *, unique=False):
    haystack, mapping = compact(source)
    needle, _ = compact(text)
    if not needle:
        return None
    found = []
    cursor = 0
    while (index := haystack.find(needle, cursor)) >= 0:
        first, last = mapping[index], mapping[index + len(needle) - 1] + 1
        before = source[first - 1:first] if first else ""
        after = source[last:last + 1]
        # Never bind variable x to the x inside "example".
        embedded = (needle[0].isascii() and needle[0].isalpha() and before.isascii() and before.isalpha()
                    or needle[-1].isascii() and needle[-1].isalpha() and after.isascii() and after.isalpha())
        if not embedded and not any(first < b and last > a for a, b in occupied):
            found.append((first, last))
        cursor = index + 1
    return found[0] if found and (not unique or len(found) == 1) else None


def _intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def file_hash(path):
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if key not in _HASH_CACHE:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        _HASH_CACHE[key] = digest.hexdigest()
        if len(_HASH_CACHE) > 32:
            _HASH_CACHE.popitem(last=False)
    return _HASH_CACHE[key]


def progress(directory):
    return copy.deepcopy(_PROGRESS.get(str(Path(directory).resolve()), {"state": "idle", "message": "等待分析"}))


def _styles(native, rect, source, raw, character_spans=None):
    spans = [s for b in native for line in b.get("lines", []) for s in line["spans"]
             if s["text"].strip() and _intersection(s["bbox"], rect) >= _area(s["bbox"]) * .6]
    ordinary = [s for s in spans if not re.search(r"math|symbol|cmmi|cmsy|cmex", s["font"], re.I)
                and re.search(r"[A-Za-z\u3400-\u9fff]{2,}", s["text"])] or spans
    if not ordinary:
        return {}, []
    dominant = max(ordinary, key=lambda s: len(s["text"].strip()))
    sizes = Counter()
    for s in ordinary:
        sizes[round(s["size"], 1)] += len(s["text"])
    size = sizes.most_common(1)[0][0]
    base = asdict(TextStyle(dominant["font"], size,
                          all(s["flags"] & 16 for s in ordinary),
                          all(s["flags"] & 2 for s in ordinary), dominant["color"]))
    runs, occupied = [], []
    character_spans = character_spans if character_spans is not None else _character_spans(raw)
    for s in spans:
        match = find_range(source, s["text"], occupied)
        if not match:
            continue
        occupied.append(match)
        script = "sup" if s["flags"] & 1 and s["size"] < size * .9 else ""
        own = character_spans.get((tuple(s["bbox"]), s["font"]), [])
        run = TextRun(*match, source[match[0]:match[1]], list(s["bbox"]), s.get("origin", [0, s["bbox"][3]])[1],
                      asdict(TextStyle(s["font"], s["size"], bool(s["flags"] & 16), bool(s["flags"] & 2), s["color"], script)),
                      [key for _, key in own])
        runs.append(asdict(run))
    return base, sorted(runs, key=lambda r: r["start"])


def glyph_id(char):
    return text_hash(json.dumps([char["bbox"], char["origin"], char["c"]]))[:16]


def _character_spans(raw):
    return {(tuple(s["bbox"]), s["font"]): [(c, glyph_id(c)) for c in s.get("chars", [])]
            for b in raw for line in b.get("lines", []) for s in line["spans"]}


def _role(rect, regions, source, obstacles):
    candidates = [r for r in regions if r["confidence"] >= .5 and _intersection(rect, r["bbox"]) >= _area(rect) * .8]
    labels = {r["label"] for r in candidates}
    # A model category is evidence, not permission to erase or relocate content.
    if len(labels) > 1:
        return "unknown"
    if not candidates:
        return "text"
    role = candidates[0]["label"]
    if role in {"figure", "table"} and not any(_intersection(rect, box) > 0 for box in obstacles):
        return "unknown"
    return role


def _equation(text, fonts, role):
    words = re.findall(r"[A-Za-z]{3,}", text)
    prose = [w for w in words if w.lower() not in {"sin", "cos", "tan", "log", "exp", "max", "min", "lim", "arg", "det", "diag"}]
    evidence = re.search(r"[=≤≥∑∫√∏∂∇]", text) or any(re.search(r"math|cmsy|cmmi|cmex", f, re.I) for f in fonts)
    return bool(text.strip() and not prose and evidence and (len(text) < 500 or role == "isolate_formula"))


def _formula_objects(page, block, rect, raw, native, role, get_assets):
    # Reuse the established glyph-only extractor; do not crop page screenshots.
    from pdf_layout import inline_math_runs, inline_math_assets, inline_formula_cluster, inspect_block, _inline_math_run
    source = block["text"]
    runs = inline_math_runs(raw, rect, combine=True)
    # Unicode operators and ordinary-font italic variables with scripts are
    # additional evidence. A lone italic prose word is not a formula.
    for b in raw:
        for line in b.get("lines", []):
            for si, s in enumerate(line["spans"]):
                chars = [{**c, "font": s["font"], "size": s["size"]} for c in s.get("chars", [])]
                text = "".join(c["c"] for c in chars)
                if not text.strip() or _intersection(rect, s["bbox"]) < _area(s["bbox"]) * .8:
                    continue
                mathematical = bool(re.search(r"[=≤≥∑∫√∏∂∇α-ωΑ-Ω]", text))
                following = line["spans"][si + 1:si + 2]
                scripted_variable = (bool(s.get("flags", 0) & 2) and bool(re.fullmatch(r"\s*[A-Za-z]\s*", text))
                                     and following and following[0]["size"] < s["size"] * .85
                                     and following[0]["bbox"][0] - s["bbox"][2] < s["size"] * .5)
                if scripted_variable:
                    chars = chars + [{**c, "font": following[0]["font"], "size": following[0]["size"]}
                                     for c in following[0].get("chars", [])]
                    text = "".join(c["c"] for c in chars)
                    mathematical = True
                if not mathematical or re.search(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", text):
                    continue
                if any(_intersection(s["bbox"], r["rect"]) >= _area(s["bbox"]) * .5 for r in runs):
                    continue
                run = _inline_math_run(chars, True)
                if run:
                    runs.append(run)
    occupied, formulas, issues = [], [], []
    if runs:
        resources = get_assets()
        for run in sorted(runs, key=lambda r: (r["rect"].y0, r["rect"].x0)):
            match = find_range(source, run["text"], occupied)
            if not match:
                continue  # Existing cluster/geometry handling remains authoritative.
            assets = inline_math_assets(page, [run], None, **resources)
            if not assets:
                issues.append("公式字形无法完整提取，保留原文")
                continue
            occupied.append(match)
            asset = assets[0]
            asset = {**asset, "svg": asset["svg"].decode("utf-8"), "run": {**run, "rect": list(run["rect"])}}
            chars = run["chars"]
            formula = FormulaObject(f"{block['id']}:f{len(formulas)}", *match, source[match[0]:match[1]], list(run["rect"]),
                                    max(chars, key=lambda c: c["size"])["origin"][1],
                                    [glyph_id(c) for c in chars], asset)
            formulas.append(asdict(formula))
        style, _ = inspect_block(native, rect, combine=True)
        if style:
            cluster = inline_formula_cluster(page, block, rect, raw, style, source, **resources)
            if cluster and cluster.get("asset"):
                asset = cluster["asset"]
                # Match the same normalized spelling used by the existing renderer.
                match = find_range(source, asset.get("match_normalized", asset["normalized"]), unique=True)
                if match:
                    formulas = [f for f in formulas if not (f["start"] < match[1] and f["end"] > match[0])]
                    asset = {**asset, "svg": asset["svg"].decode("utf-8"), "cluster_rect": list(cluster["rect"])}
                    formulas.append(asdict(FormulaObject(f"{block['id']}:cluster", *match, source[match[0]:match[1]],
                                                         list(cluster["rect"]), rect.y1, [], asset)))
    return sorted(formulas, key=lambda f: f["start"]), list(dict.fromkeys(issues))


def _build_page(page, saved, model, regions):
    from pdf_layout import source_rect, _svg_math_index
    import xml.etree.ElementTree as ET
    native = [b for b in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"] if b["type"] == 0]
    raw = [b for b in page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"] if b["type"] == 0]
    character_spans = _character_spans(raw)
    rects = {b["id"]: source_rect(page, b) for b in saved["blocks"]}
    rotation, rotation_matrix, derotation_matrix = page.rotation, list(page.rotation_matrix), list(page.derotation_matrix)
    page.set_rotation(0)
    try:
        drawings = page.get_drawings()
        images = page.get_image_info()
        obstacles = [list(d["rect"]) for d in drawings] + [list(i["bbox"]) for i in images]
        traces = page.get_texttrace()
        glyph_count = sum(len(t.get("chars", [])) for t in traces)
        invisible_count = sum(len(t.get("chars", [])) for t in traces if t.get("type") == 3 or t.get("opacity", 1) < .01)
        scanned = (glyph_count > 0 and invisible_count >= glyph_count * .8
                   and any(_intersection(image["bbox"], page.rect) >= page.rect.get_area() * .75 for image in images))
        scan_reason = "扫描图像上的隐藏 OCR 文字层无法安全替换，完整保留原页；本轮不处理扫描件重建"
        resources = {}

        def assets():
            if not resources:
                svg = ET.fromstring(page.get_svg_image(text_as_path=True))
                definitions, uses = _svg_math_index(svg)
                resources.update(svg_root=svg, definitions=definitions, uses=uses, drawings=drawings)
            return resources

        paragraphs = []
        for block in saved["blocks"]:
            rect = rects[block["id"]]
            role = _role(rect, regions, block["text"], obstacles)
            base, runs = _styles(native, rect, block["text"], raw, character_spans)
            pure = _equation(block["text"], [r["style"]["font"] for r in runs], role)
            formulas, issues = ([], []) if scanned or pure or block.get("reference") else _formula_objects(page, block, rect, raw, native, role, assets)
            if scanned:
                issues = [scan_reason]
            paragraphs.append(asdict(ParagraphStructure(block["id"], text_hash(block["text"]), list(rect), role,
                                                        base, runs, formulas, pure, issues, [block["id"]])))
        characters = [{"id": glyph_id(char), "text": char["c"], "bbox": list(char["bbox"]),
                       "origin": list(char["origin"]), "font": s["font"], "size": s["size"], "flags": s["flags"],
                       "owner_ids": [key for key, box in rects.items()
                                     if box.x0 <= (char["bbox"][0] + char["bbox"][2]) * .5 <= box.x1
                                     and box.y0 <= (char["bbox"][1] + char["bbox"][3]) * .5 <= box.y1]}
                      for b in raw for line in b["lines"] for s in line["spans"] for char in s.get("chars", [])]
        return asdict(PageStructure(STRUCTURE_VERSION, saved["number"], page.rect.width, page.rect.height, rotation,
                                    list(page.cropbox), rotation_matrix, derotation_matrix, model, regions, paragraphs,
                                    native, raw, obstacles, [b["id"] for b in saved["blocks"]],
                                    [scan_reason] if scanned else [], scanned, characters))
    finally:
        page.set_rotation(rotation)


def ensure_pages(directory, document, numbers=None, *, summary_only=False):
    """Build requested pages once. Recover model failures without caching success."""
    from pdf_layout import PDF_LOCK
    directory = Path(directory)
    source = directory / "source.pdf"
    wanted = set(numbers) if numbers is not None else {p["number"] for p in document["pages"]}
    selected = [p for p in document["pages"] if p["number"] in wanted]
    result = {}
    progress_key = str(directory.resolve())
    with _LOCK:
        source_digest = file_hash(source)
        for done, saved in enumerate(selected):
            _PROGRESS[progress_key] = {"state": "analyzing", "message": f"正在分析页面结构 {done + 1}/{len(selected)}", "current": done + 1, "total": len(selected)}
            identity = {"source": source_digest, "version": STRUCTURE_VERSION, "model": pdf_model.fingerprint(),
                        "preprocess": pdf_model.PROTOCOL_VERSION, "enabled": pdf_model.configured(),
                        "page": saved["number"], "blocks": [(b["id"], b["text"], b["bbox"]) for b in saved["blocks"]]}
            key = text_hash(json.dumps(identity, sort_keys=True))
            path = directory / "structure" / (key + ".json")
            cache_key = str(path.resolve())
            cached = _CACHE.get(cache_key)
            if cached is None:
                try:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    cached = None
            available = [pdf_model.configured(), pdf_model.fingerprint()]
            valid = (isinstance(cached, dict) and cached.get("identity") == key and cached.get("availability") == available
                     and all(isinstance(cached.get(k), list) for k in ("native", "raw", "paragraphs", "regions"))
                     and isinstance(cached.get("model"), dict) and bool(cached.get("fingerprint"))
                     and (cached["model"].get("state") == "ready" or not pdf_model.configured()
                          or time.time() - cached.get("created", 0) < 60))
            if valid:
                count("structure_cache_hits")
                _remember(cache_key, path, cached)
                result[saved["number"]] = _summary(cached, path) if summary_only else cached
                continue
            model = {"state": "ready", "provider": "translation_api", "fingerprint": pdf_model.fingerprint()}
            regions = []
            try:
                regions = pdf_model.detect_page(source, saved)
            except pdf_model.VisionError as error:
                model.update(state="unavailable", reason=f"视觉 API 版面识别未完成：{error}")
            with PDF_LOCK, pymupdf.open(source) as pdf, span("structure_build"):
                source_page = pdf[saved["number"] - 1]
                try:
                    value = _build_page(source_page, saved, model, regions)
                except (ValueError, RuntimeError, KeyError, TypeError, IndexError) as error:
                    # A damaged character/asset must not make import or saved
                    # translations unusable. Mark every affected block intact.
                    reason = "页面结构无法可靠恢复，保留完整原文（" + type(error).__name__ + "）"
                    native = [b for b in source_page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"] if b["type"] == 0]
                    raw = [b for b in source_page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"] if b["type"] == 0]
                    from pdf_layout import source_rect
                    paragraphs = [asdict(ParagraphStructure(b["id"], text_hash(b["text"]), list(source_rect(source_page, b)),
                                  "unknown", {}, [], [], False, [reason], [b["id"]])) for b in saved["blocks"]]
                    value = asdict(PageStructure(STRUCTURE_VERSION, saved["number"], source_page.cropbox.width,
                        source_page.cropbox.height, source_page.rotation, list(source_page.cropbox), list(source_page.rotation_matrix),
                        list(source_page.derotation_matrix), {**model, "state": "unavailable", "reason": reason}, regions,
                        paragraphs, native, raw, [], [b["id"] for b in saved["blocks"]], [reason]))
            value.update(identity=key, availability=[pdf_model.configured(), pdf_model.fingerprint()], created=time.time())
            value["fingerprint"] = text_hash(json.dumps({k: v for k, v in value.items() if k not in {"created", "availability"}}, sort_keys=True))
            atomic_json(path, value)
            _remember(cache_key, path, value)
            result[saved["number"]] = _summary(value, path) if summary_only else value
        failures = [p["model"].get("reason") for p in result.values() if p["model"]["state"] != "ready"]
        _PROGRESS[progress_key] = {"state": "degraded" if failures else "ready", "message": failures[0] if failures else "页面结构分析完成", "current": len(selected), "total": len(selected)}
    return result


def _summary(value, path):
    # Whole-document preparation/signatures need metadata only. Keep raw glyphs
    # and SVGs page-local rather than retaining hundreds of pages in memory.
    return {"fingerprint": value["fingerprint"], "model": value["model"], "number": value["number"],
            "structure_file": str(path.resolve())}


def paragraph_for(pages, block):
    for page in pages.values():
        for paragraph in page["paragraphs"]:
            if paragraph["block_id"] == block["id"] and paragraph["source_hash"] == text_hash(block["text"]):
                return paragraph
    return None
