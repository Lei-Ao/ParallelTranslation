"""PDF and paragraph selection. Canonical quotes are reconstructed server-side."""
import hashlib
import json
import math
import threading
from collections import OrderedDict
from functools import lru_cache
from typing import Literal

import pymupdf
from fastapi import HTTPException
from pydantic import BaseModel, Field
from paper_analysis import digest
from pdf_selection import page_selection

TEXT_VERSION = 1
_cache = OrderedDict()
_lock = threading.RLock()


class TextRange(BaseModel):
    run_id: str = Field(max_length=80)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class CitationRequest(BaseModel):
    page: int = Field(ge=1)
    text_version: str = Field(max_length=80)
    ranges: list[TextRange] = Field(min_length=1, max_length=300)
    view: Literal['source', 'translation', 'paragraph'] = 'source'
    translated: bool = True
    revision: str | None = Field(default=None, pattern=r'^[a-f0-9]{24}$')
    block_id: str | None = Field(default=None, max_length=80)


@lru_cache(maxsize=32)
def _pdf_hash(path, mtime_ns, size):
    fingerprint = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            fingerprint.update(chunk)
    return fingerprint.hexdigest()


def text_layer(service, doc_id, number, revision=None):
    folder = service.doc_dir(doc_id)
    pdf_path = service.layout_file(doc_id, revision) if revision else folder / 'source.pdf'
    stamp = pdf_path.stat()
    key = (str(pdf_path.resolve()), stamp.st_mtime_ns, stamp.st_size, number, TEXT_VERSION)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    doc = service.load_doc(doc_id)
    if not 1 <= number <= len(doc['pages']):
        raise HTTPException(422, '文字层页码不存在')
    fingerprint = _pdf_hash(pdf_path.resolve(), stamp.st_mtime_ns, stamp.st_size)
    runs, warnings, citation_runs = [], [], {}
    with service.PDF_LOCK, pymupdf.open(pdf_path) as pdf:
        page = pdf[number - 1]; width, height = page.rect.width, page.rect.height
        def point(p):
            p = pymupdf.Point(p) * page.rotation_matrix
            return [p.x / width, p.y / height]
        native = page.get_text('rawdict', flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
        for block in native['blocks']:
            if block['type'] != 0:
                continue
            for line in block['lines']:
                for span in line['spans']:
                    chars = span.get('chars', [])
                    text = ''.join(c['c'] for c in chars)
                    if not text.strip():
                        continue
                    if '\ufffd' in text or any(ord(c) < 32 and c not in '\t\n' for c in text):
                        warnings.append('部分文字编码无法可靠识别，请框选原图。'); continue
                    try:
                        quad = pymupdf.recover_quad(line['dir'], span)
                        corners = [point(p) for p in (quad.ul, quad.ur, quad.ll, quad.lr)]
                        boxes = []
                        for char in chars:
                            q = pymupdf.recover_char_quad(line['dir'], span, char)
                            points = [point(p) for p in (q.ul, q.ur, q.ll, q.lr)]
                            box = [min(p[0] for p in points), min(p[1] for p in points),
                                   max(p[0] for p in points), max(p[1] for p in points)]
                            boxes.extend([box] * len(char['c']))
                        if not all(math.isfinite(v) for row in corners + boxes for v in row):
                            raise ValueError()
                        if not boxes or any(min(box) < -.02 or max(box) > 1.02 for box in boxes):
                            raise ValueError()
                        center = [(corners[0][i] + corners[3][i]) / 2 for i in (0, 1)]
                        matches = [] if revision else [b for b in doc['pages'][number-1]['blocks']
                                   if b['bbox'][0] <= center[0] <= b['bbox'][2] and b['bbox'][1] <= center[1] <= b['bbox'][3]]
                        block_id = matches[0]['id'] if len(matches) == 1 else None
                        runs.append(dict(id=f'r{len(runs)}', text=text, corners=corners, boxes=boxes,
                                         size=span['size'], font=span['font'], block_id=block_id))
                        citation_runs[id(span)] = runs[-1]['id']
                    except (ValueError, KeyError, TypeError):
                        warnings.append('部分文字几何无法可靠映射，请使用原图区域。')
        lines = page_selection(page, native=native, citation_runs=citation_runs)['lines']
    result = dict(version=digest([TEXT_VERSION, fingerprint, number, runs]), page=number, width=width, height=height,
                  runs=runs, lines=lines, warnings=list(dict.fromkeys(warnings)),
                  note='' if runs else '此页没有可靠文字层，可使用框选图像解读。')
    with _lock:
        _cache[key] = result
        while len(_cache) > 32:
            _cache.popitem(last=False)
    return result


def resolve_citation(service, doc_id, request):
    if request.view == 'paragraph':
        doc = service.load_doc(doc_id)
        block = next((b for p in doc['pages'] if p['number'] == request.page
                      for b in p['blocks'] if b['id'] == request.block_id), None)
        if not block:
            raise HTTPException(422, '引用段落不存在')
        translated = bool(request.translated) and bool(block.get('translation'))
        text = block['translation'] if translated else block['text']
        if request.text_version != digest(text):
            raise HTTPException(409, '段落内容已改变，请重新选择')
        if len(request.ranges) != 1:
            raise HTTPException(422, '段落引用字符范围无效')
        item = request.ranges[0]
        if item.run_id != block['id'] or not 0 <= item.start < item.end <= len(text):
            raise HTTPException(422, '段落引用字符范围无效')
        if item.end - item.start > 16000:
            raise HTTPException(422, '引用过长，请分批选择')
        source = dict(doc_id=doc_id, page=request.page, view='paragraph', block_id=block['id'],
                      translated=translated, text_version=request.text_version,
                      ranges=[item.model_dump()], text=text[item.start:item.end])
        return dict(id='quote-' + digest(source)[:20], text=text[item.start:item.end], source=source, block_ids=[block['id']],
                    request=request.model_dump())
    if request.view == 'translation' and not request.revision:
        raise HTTPException(422, '请选择当前译文 PDF 版本')
    revision = request.revision if request.view == 'translation' else None
    layer = text_layer(service, doc_id, request.page, revision)
    if request.text_version != layer['version']:
        raise HTTPException(409, '文字层版本已改变，请重新选择')
    lookup = {r['id']: r for r in layer['runs']}
    visual_order = dict.fromkeys(
        glyph[9] for line in layer['lines'] for glyph in line if len(glyph) > 9
    )
    selected_ids = [item.run_id for item in request.ranges]
    selected_set = set(selected_ids)
    # Accept both the original contributor's PDF drawing order and the reader's visual order.
    if len(selected_ids) != len(selected_set) or not any(
        selected_ids == [key for key in order if key in selected_set] for order in (lookup, visual_order)
    ):
        raise HTTPException(422, '引用字符范围无效或顺序重复')
    selected, boxes, block_ids, canonical = [], [], set(), []
    context_ambiguous = False
    for item in request.ranges:
        run = lookup.get(item.run_id)
        if not run or not 0 <= item.start < item.end <= len(run['text']):
            raise HTTPException(422, '引用字符范围无效或顺序重复')
        selected.append(run['text'][item.start:item.end])
        chosen = run['boxes'][item.start:item.end]
        boxes.append([max(0, min(b[0] for b in chosen)), max(0, min(b[1] for b in chosen)),
                      min(1, max(b[2] for b in chosen)), min(1, max(b[3] for b in chosen))])
        if run['block_id']:
            block_ids.add(run['block_id'])
        elif not revision:
            context_ambiguous = True
        canonical.append(item.model_dump())
    text = '\n'.join(selected)
    if len(text) > 16000:
        raise HTTPException(422, '引用过长，请分批选择')
    source = dict(doc_id=doc_id, page=request.page, rects=boxes, text_version=layer['version'], ranges=canonical)
    if revision:
        source.update(view='translation', revision=revision)
    if len(block_ids) == 1:
        source['block_id'] = next(iter(block_ids))
    if revision:
        report_path = service.doc_dir(doc_id) / 'layout' / (revision + '.json')
        placements = {}
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding='utf-8'))
                if isinstance(report, dict) and isinstance(report.get('placements'), dict):
                    placements = report['placements']
            except (OSError, ValueError, TypeError):
                pass  # The exact selected text remains valid; context falls back to its page.
        page_blocks = service.load_doc(doc_id)['pages'][request.page-1]['blocks']
        for rect in boxes:
            matches = []
            for block in page_blocks:
                box = placements.get(block['id'])
                try:
                    if box and (pymupdf.Rect(box) & pymupdf.Rect(rect)).get_area() > 0:
                        matches.append(block['id'])
                except (ValueError, TypeError):
                    continue
            block_ids.update(matches)
            context_ambiguous |= len(matches) != 1
    ordered_ids = [b['id'] for p in service.load_doc(doc_id)['pages'] for b in p['blocks'] if b['id'] in block_ids]
    return dict(id='quote-' + digest(source)[:20], text=text, source=source, block_ids=ordered_ids,
                context_ambiguous=context_ambiguous, request=request.model_dump())


def install(service):
    @service.app.get('/api/documents/{doc_id}/reading/text/{page}')
    def get_text(doc_id: str, page: int, revision: str | None = None, display: bool = False):
        layer = text_layer(service, doc_id, page, revision)
        return {key: value for key, value in layer.items() if key != ('runs' if display else 'lines')}

    @service.app.post('/api/documents/{doc_id}/reading/citations')
    def quote(doc_id: str, request: CitationRequest):
        return resolve_citation(service, doc_id, request)
