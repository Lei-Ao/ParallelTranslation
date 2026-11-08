"""Manual document glossary and read-only access to retired review records.

No automatic extraction, review, revision, or model job execution lives here.
"""
import copy
import json
import threading
import pymupdf
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from translation_terms import active_entries, contains, csv_text, merge_entries, read_csv

VERSION = 1

def context_material(service, doc):
    from paper_reading import build_structure
    from pdf_layout import inline_math_runs
    with service.PDF_LOCK:
        structure = build_structure(service, doc)
        sections = {bid: u['title'] for u in structure['units'] for bid in u['block_ids']}
        formula_map, gaps, unmapped = {}, [], []
        with pymupdf.open(service.doc_dir(doc['id']) / 'source.pdf') as pdf:
            for page in doc['pages']:
                source_page = pdf[page['number'] - 1]
                raw = source_page.get_text('rawdict')['blocks']
                if not page['blocks'] and (source_page.get_images() or source_page.get_drawings()):
                    gaps.append(f"第 {page['number']} 页含视觉内容但没有可用文字，文字提取未覆盖该页内容。")
                for block in page['blocks']:
                    # Stored bboxes use displayed coordinates; raw text uses unrotated coordinates.
                    box = block['bbox']
                    rect = pymupdf.Rect(box[0] * page['width'], box[1] * page['height'],
                                        box[2] * page['width'], box[3] * page['height']) * source_page.derotation_matrix
                    runs = inline_math_runs(raw, rect)
                    exact = [r['text'] for r in runs if len(r['text'].strip()) >= 2 and block['text'].count(r['text']) == 1]
                    formula_map[block['id']] = exact
                    if any(len(r['text'].strip()) >= 2 and r['text'] not in exact for r in runs):
                        unmapped.append(block['id'])
                        gaps.append(f"段落 {block['id']} 的部分行内公式不能精确映射，需对照原文核查。")
    return dict(sections=sections, formulas=formula_map, gaps=gaps, unmapped_formula_blocks=unmapped)


class Entry(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=500)
    tgt_lng: str = Field(default='', max_length=100)
    enabled: bool = True


class GlossaryEdit(BaseModel):
    revision: int
    entries: list[Entry] = Field(max_length=5000)


class CSVImport(BaseModel):
    revision: int
    text: str = Field(max_length=2_000_000)
    target: str = Field(default='简体中文', max_length=100)


class AcceptRequest(BaseModel):
    block_id: str = Field(max_length=200)


def install(service):
    app, lock = service.app, threading.RLock()

    def load(doc_id):
        path = service.doc_dir(doc_id) / 'translation-quality.json'
        if not path.exists():
            return dict(version=VERSION, revision=0, entries=[], jobs={}, cache={})
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if (data['version'] != VERSION or not isinstance(data['entries'], list)
                    or not isinstance(data['jobs'], dict) or not isinstance(data['cache'], dict)
                    or type(data['revision']) is not int):
                raise ValueError()
            return data
        except (ValueError, KeyError, TypeError, OSError):
            raise HTTPException(409, '历史翻译数据无法读取或版本不兼容，已保留原文件')

    def save(doc_id, data):
        path = service.doc_dir(doc_id)
        # Cache is disposable; completed review records are not.
        while len(data['cache']) > 1500:
            del data['cache'][next(iter(data['cache']))]
        temporary = path / 'translation-quality.tmp'
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path / 'translation-quality.json')

    def reconcile(doc_id, data):
        # Present retired jobs as stopped without rewriting historical data on reads.
        for job in data['jobs'].values():
            if job['status'] in {'queued', 'running', 'pausing'}:
                job.update(status='interrupted', stage='自动审校已移除；请使用翻译按钮继续未译段落')

    def mutate_job(doc_id, job_id, callback):
        with lock:
            data = load(doc_id)
            callback(data['jobs'][job_id])
            save(doc_id, data)

    def summary(job):
        result = {k: copy.deepcopy(job[k]) for k in ('id', 'action', 'status', 'stage', 'target', 'created',
                  'block_ids', 'completed_ids', 'gaps', 'metrics', 'error', 'glossary_revision', 'source_signature')}
        result['records'] = {bid: {k: r.get(k) for k in ('status', 'issues', 'error', 'accepted', 'published')}
                             for bid, r in job['records'].items()}
        result['progress'] = copy.deepcopy(job.get('progress'))
        result['activity'] = copy.deepcopy(job.get('activity'))
        return result

    @app.get('/api/documents/{doc_id}/quality')
    def get_quality(doc_id: str):
        with lock:
            data = load(doc_id)
            reconcile(doc_id, data)
            return dict(version=VERSION, revision=data['revision'], entries=[e for e in data['entries'] if e.get('origin') == 'manual'],
                        jobs=[summary(j) for j in data['jobs'].values()])

    def update_glossary(doc_id, revision, entries, replace):
        for entry in entries:
            entry['source'], entry['target'] = entry['source'].strip(), entry['target'].strip()
            if not entry['source'] or not entry['target']:
                raise HTTPException(400, '术语原词和译法不能为空')
        with lock:
            data = load(doc_id)
            if revision != data['revision']:
                raise HTTPException(409, '术语表已更新，请重新打开后修改')
            if replace:
                # Retain automatic entries; editing a row creates an explicit override.
                automatic = [e for e in data['entries'] if e.get('origin') == 'auto']
                data['entries'] = merge_entries([], entries, automatic)
            else:
                data['entries'] = merge_entries([], entries, data['entries'])
            data['revision'] += 1
            save(doc_id, data)
        doc = service.snapshot_doc(doc_id)
        affected = [b['id'] for b in service.all_blocks(doc) if b.get('translation')
                    and any(contains(b['text'], e['source']) for e in entries)]
        return dict(revision=data['revision'], entries=data['entries'], affected=affected)

    @app.put('/api/documents/{doc_id}/quality/glossary')
    def put_glossary(doc_id: str, request: GlossaryEdit):
        entries = [dict(**e.model_dump(), origin='manual', source_ids=[], alternatives=[]) for e in request.entries]
        return update_glossary(doc_id, request.revision, entries, True)

    @app.post('/api/documents/{doc_id}/quality/glossary/import')
    def import_glossary(doc_id: str, request: CSVImport):
        try:
            entries = read_csv(request.text, request.target)
        except ValueError as error:
            raise HTTPException(400, str(error))
        return update_glossary(doc_id, request.revision, entries, False)

    @app.get('/api/documents/{doc_id}/quality/glossary.csv')
    def export_glossary(doc_id: str):
        with lock:
            text = csv_text([e for e in load(doc_id)['entries'] if e.get('origin') == 'manual'])
        return Response('\ufeff' + text, media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="glossary.csv"'})

    @app.get('/api/documents/{doc_id}/quality/jobs/{job_id}')
    def get_job(doc_id: str, job_id: str, compact: bool = False):
        with lock:
            data = load(doc_id)
            reconcile(doc_id, data)
            if job_id not in data['jobs']:
                raise HTTPException(404, '历史任务不存在')
            return summary(data['jobs'][job_id]) if compact else copy.deepcopy(data['jobs'][job_id])

    @app.post('/api/documents/{doc_id}/quality/jobs')
    @app.post('/api/documents/{doc_id}/quality/jobs/{job_id}/resume')
    @app.post('/api/documents/{doc_id}/quality/jobs/{job_id}/pause')
    def retired_job(doc_id: str, job_id: str = ''):
        service.doc_dir(doc_id)
        raise HTTPException(410, '自动审校流程已移除，请刷新页面，使用翻译按钮继续未译段落')

    def manual_prompt(doc_id, target, text):
        with lock:
            entries = [e for e in load(doc_id)['entries'] if e.get('origin') == 'manual']
        matched = active_entries(entries, target, text)
        if not matched:
            return ''
        return '\n\n本文人工术语（优先于通用词表；仅适用于当前语境）：\n' + '\n'.join(
            e['source'] + ' = ' + e['target'] for e in matched)

    @app.post('/api/documents/{doc_id}/quality/jobs/{job_id}/accept')
    def accept(doc_id: str, job_id: str, request: AcceptRequest):
        job = get_job(doc_id, job_id)
        record = job['records'].get(request.block_id)
        if not record or not record.get('candidate') or record['status'] in {'blocked', 'unreviewed'}:
            raise HTTPException(409, '没有可接受的完整候选译文')
        if record.get('accepted') or record.get('published'):
            raise HTTPException(409, '该版本已经应用')
        with service.doc_lock(doc_id), service.WRITE_LOCK:
            doc = service.load_doc(doc_id)
            _, _, current = service.find_block(doc, request.block_id)
            service.ensure_unchanged(current, record['original'])
            current.update(translation=record['candidate'], target=job['target'], edited=True)
            service.save_doc(service.doc_dir(doc_id), doc)
            saved = copy.deepcopy(current)
        mutate_job(doc_id, job_id, lambda j: j['records'][request.block_id].update(accepted=True))
        return saved

    return dict(load=load, manual_prompt=manual_prompt)
