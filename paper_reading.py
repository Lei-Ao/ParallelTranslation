"""Version-pinned local Skill sessions. Tools never expose arbitrary paths or commands."""
import asyncio
import base64
import copy
import json
import math
import re
import threading
import uuid

import pymupdf
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal
from paper_analysis import digest, now, outline
from reading_model import invoke
import reading_workflow
import reading_notes
import reading_context
from reading_text import CitationRequest, resolve_citation, install as install_text

VERSION = 1
MAX_TOOLS = 12
PAGE_CHARS = 10000
MAX_IMAGE_BYTES = 64 * 1024 * 1024
CAPTION = re.compile(r'^\s*(?:figure|fig\.?|table|图|表)\s*\d', re.I)


def tool(name, description, properties):
    return dict(type='function', function=dict(name=name, description=description,
                parameters=dict(type='object', properties=properties, additionalProperties=False)))


TOOLS = [
    tool('read_skill', '读取 Skill 的 references 资料，path 为相对路径', {'path': {'type': 'string'}}),
    tool('get_structure', '分页获取目录、阅读单元、图表标题。cursor 从 0 开始', {'cursor': {'type': 'integer'}}),
    tool('read_material', '读取当前论文原文；unit_id/page/block_id 任选一个，不选则全文。cursor 为字符偏移，继续 next_cursor 才算读完。',
         {'unit_id': {'type': 'string'}, 'page': {'type': 'integer'}, 'block_id': {'type': 'string'}, 'cursor': {'type': 'integer'}}),
    tool('render_page', '查看原文页面或区域。page 从 1 开始，bbox 为显示页面归一化 [x0,y0,x1,y1]；返回图片与引用 ID',
         {'page': {'type': 'integer'}, 'bbox': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 4, 'maxItems': 4},
          'render_scale': {'type': 'number', 'minimum': .1, 'maximum': 8},
          'view': {'type': 'string', 'enum': ['source', 'translation']}, 'revision': {'type': 'string'}}),
    tool('search_material', '按术语、定义、公式或图表编号检索当前论文，返回完整原文段落及前后文。', {'query': {'type': 'string'}}),
    tool('save_summary', '仅用户请求小结时使用。先读模板和提问清单；保存为新版本，不覆盖旧文件', {'markdown': {'type': 'string'}}),
]


def skill_snapshot(root):
    directory = root / 'skills' / 'paper-reading-guide'
    files = {p.relative_to(directory).as_posix(): p.read_text(encoding='utf-8')
             for p in directory.rglob('*.md') if 'full-version' not in p.parts and not p.is_symlink()}
    if 'SKILL.md' not in files or 'references/domains/_index.md' not in files:
        raise HTTPException(409, '论文陪读 Skill 文件不完整')
    files['adapter.md'] = (root / 'skills' / 'reading-adapter.md').read_text(encoding='utf-8')
    return dict(version=digest(files), files=files)


def build_structure(service, doc):
    navigation = outline(doc, service.doc_dir(doc['id']), service.PDF_LOCK)
    units, captions = [], []
    carry_title = None
    carry_group = []
    # Split at matched heading blocks and page boundaries, never rewrite block IDs.
    for page in doc['pages']:
        anchors = {}
        for item in navigation['items']:
            if item['page'] != page['number']:
                continue
            match = next((b for b in page['blocks'] if b['id'] == item.get('block_id')), None)
            if match is None:
                title = re.sub(r'\W', '', item['title']).lower()
                match = next((b for b in page['blocks'] if title and title in re.sub(r'\W', '', b['text']).lower()), None)
            if match:
                anchors[match['id']] = item['title']
        title, group, size = carry_title or f"第 {page['number']} 页", carry_group, 0
        size = sum(len(b['text']) for p in doc['pages'] for b in p['blocks'] if b['id'] in group)
        def flush():
            if group:
                first_page = next(p['number'] for p in doc['pages'] if any(b['id']==group[0] for b in p['blocks']))
                units.append(dict(id=digest(group)[:20], title=title, page=first_page,
                                  block_ids=list(group), source=dict(doc_id=doc['id'], page=first_page, block_id=group[0])))
        for block in page['blocks']:
            for line in block['text'].splitlines():
                if CAPTION.match(line):
                    captions.append(dict(text=line, page=page['number'], block_id=block['id']))
            if block['id'] in anchors or size >= 12000:
                flush(); group, size = [], 0
                title = anchors.get(block['id'], title + '（续）')
            group.append(block['id']); size += len(block['text'])
        if navigation['mode'] != 'pages' and page['blocks']:
            carry_title, carry_group = title, group
        else:
            flush(); carry_title, carry_group = None, []
        if not page['blocks']:
            units.append(dict(id=f"page-{page['number']}", title=f"第 {page['number']} 页 · 视觉阅读", page=page['number'],
                              block_ids=[], source=dict(doc_id=doc['id'], page=page['number'])))
    if carry_group:
        flush()
    return dict(outline=navigation, units=units, captions=captions)


def region(doc, page, bbox=None):
    if type(page) is not int or not 1 <= page <= len(doc['pages']):
        raise HTTPException(422, '页码超出当前论文范围')
    bbox = [0, 0, 1, 1] if bbox is None else bbox
    if (not isinstance(bbox, list) or len(bbox) != 4 or
            any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in bbox) or
            bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise HTTPException(422, '页面区域无效')
    return dict(doc_id=doc['id'], page=page, bbox=bbox)


def render_region(service, doc, source):
    location = region(doc, source['page'], source.get('bbox'))
    requested_scale = source.get('render_scale', 2)
    if type(requested_scale) not in (int, float) or not math.isfinite(requested_scale) or not .1 <= requested_scale <= 8:
        raise HTTPException(422, '渲染倍率必须在0.1到8之间')
    pdf_path = service.doc_dir(doc['id']) / 'source.pdf'
    if source.get('view') == 'translation':
        pdf_path = service.layout_file(doc['id'], source.get('revision', ''))
    with service.PDF_LOCK, pymupdf.open(pdf_path) as pdf:
        page = pdf[source['page'] - 1]
        width, height = page.rect.width, page.rect.height
        x0, y0, x1, y1 = location['bbox']
        clip = pymupdf.Rect(x0*width, y0*height, x1*width, y1*height)
        # get_pixmap clips in displayed coordinates, including rotated pages.
        # Render just this clip at the requested resolution, within the pixel budget.
        scale = min(requested_scale, (3_990_000 / (clip.width * clip.height)) ** .5)
        if 'render_scale' not in source:
            scale = min(scale, 2200 / max(width, height))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
        return pix.tobytes('png')


def read_material(doc, structure, args, limit=PAGE_CHARS):
    selectors = [k for k in ('unit_id', 'page', 'block_id') if args.get(k) is not None]
    if len(selectors) > 1:
        raise HTTPException(422, '每次只能选择一个阅读范围')
    rows = [dict(id=b['id'], page=p['number'], text=b['text'], bbox=b.get('bbox')) for p in doc['pages'] for b in p['blocks']]
    if 'unit_id' in selectors:
        unit = next((u for u in structure['units'] if u['id'] == args['unit_id']), None)
        if not unit:
            raise HTTPException(422, '阅读单元不存在')
        rows = [r for r in rows if r['id'] in unit['block_ids']]
    if 'page' in selectors:
        region(doc, args['page'])
        rows = [r for r in rows if r['page'] == args['page']]
    if 'block_id' in selectors:
        rows = [r for r in rows if r['id'] == args['block_id']]
        if not rows:
            raise HTTPException(422, '段落不属于当前论文')
    cursor = args.get('cursor', 0)
    total = sum(len(r['text']) for r in rows)
    if type(cursor) is not int or not 0 <= cursor <= total:
        raise HTTPException(422, '读取游标无效')
    output, start, left = [], 0, limit
    for row in rows:
        end = start + len(row['text'])
        if end > cursor and left:
            offset = max(0, cursor - start)
            text = row['text'][offset:offset + left]
            output.append(dict(id=row['id'], page=row['page'], text=text, offset=offset,
                               end=offset + len(text), total_chars=len(row['text']), bbox=row['bbox']))
            left -= len(text)
        start = end
    next_cursor = cursor + sum(len(r['text']) for r in output)
    return dict(rows=output, next_cursor=next_cursor if next_cursor < total else None, total_chars=total,
                note='没有文字材料，请渲染页面进行视觉阅读' if not rows else '仅以上原文片段已提供给模型')


def validate_answer(text, sources):
    ids = re.findall(r'\[\[([^\[\]\n]+)\]\]', text)
    if any(i not in sources for i in ids):
        raise HTTPException(502, '模型引用了未提供的来源；本轮未保存回答，可重试')
    return list(dict.fromkeys(ids))


class SessionUpdate(BaseModel):
    revision: int
    unit_id: str | None = None
    status: Literal['completed', 'skipped'] | None = None
    route: Literal['full', 'method', 'experiments'] | None = None
    depth: Literal['normal', 'simple', 'deep'] | None = None
    browse_page: int | None = None


class RevisionRequest(BaseModel):
    revision: int


class FavoriteRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=100)


def conversation_memory(messages):
    return '\n'.join(f"问题：{t['question']}\n回答节选（非完整）：{t['answer'][:800]}" for t in messages[:-6][-12:])


def install(service):
    app, lock, running = service.app, threading.RLock(), {}
    install_text(service)

    class MessageRequest(service.TranslateRequest):
        revision: int
        action: Literal['start', 'message', 'continue', 'simple', 'deep', 'visual', 'summary', 'test'] = 'message'
        question: str = Field(default='', max_length=4000)
        block_id: str | None = None
        page: int | None = None
        bbox: list[float] | None = None
        mode_id: Literal['quickread', 'guided', 'detail', 'followup', 'citation'] | None = None
        citations: list[CitationRequest] = Field(default_factory=list, max_length=12)

    def path(doc_id):
        return service.doc_dir(doc_id) / 'reading.json'

    def read(doc_id):
        p = path(doc_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            if (data['version'] != VERSION or not re.fullmatch(r'[0-9a-f]{32}', data['id'])
                    or not isinstance(data['jobs'], dict) or not isinstance(data['messages'], list)
                    or not isinstance(data['skill']['files'], dict)):
                raise ValueError()
            return data
        except (ValueError, KeyError, TypeError, OSError):
            raise HTTPException(409, '陪读数据无法读取或版本不兼容，原文件已保留')

    def write(doc_id, data):
        p = path(doc_id)
        temp = p.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temp.replace(p)

    def task_key(doc_id, job_id):
        return (str(service.doc_dir(doc_id).resolve()), job_id)

    def current(doc_id):
        data = read(doc_id)
        if data is None:
            raise HTTPException(409, '请先创建陪读会话')
        changed = False
        for job in data['jobs'].values():
            if job['status'] in ('queued', 'running') and task_key(doc_id, job['id']) not in running:
                job.update(status='interrupted', error='服务已重启，本轮中断；已取得材料保留，可重试')
                changed = True
        if changed:
            data['revision'] += 1; write(doc_id, data)
        return data

    def check_revision(data, revision):
        if data['revision'] != revision:
            raise HTTPException(409, '阅读状态已更新，请刷新后重试')

    def public(data):
        result = {k: v for k, v in data.items() if k not in ('skill', 'workflow')}
        result['skill_version'] = data['skill']['version']
        result['workflow_version'] = data.get('workflow', {}).get('version')
        return result

    @app.get('/api/documents/{doc_id}/reading')
    def get_session(doc_id: str):
        with lock:
            return public(current(doc_id)) if read(doc_id) else {'session': None}

    @app.post('/api/documents/{doc_id}/reading')
    def create_session(doc_id: str):
        with lock:
            if read(doc_id):
                return public(current(doc_id))
            doc = service.load_doc(doc_id)
            structure = build_structure(service, doc)
            data = dict(version=VERSION, id=uuid.uuid4().hex, revision=0, created=now(),
                        skill=skill_snapshot(service.ROOT), document_digest=digest(doc['pages']), structure=structure,
                        workflow=reading_workflow.snapshot(service.ROOT), mode_id='quickread',
                        route='full', depth='normal', unit_id=structure['units'][0]['id'], progress={}, browse_page=1,
                        messages=[], memory='', sources={}, coverage={}, jobs={}, summaries=[], last_context=None, tool_progress=[])
            write(doc_id, data)
            return public(data)

    @app.patch('/api/documents/{doc_id}/reading')
    def update_session(doc_id: str, request: SessionUpdate):
        with lock:
            data = current(doc_id); check_revision(data, request.revision)
            if request.unit_id is not None:
                if not any(u['id'] == request.unit_id for u in data['structure']['units']):
                    raise HTTPException(422, '阅读单元不存在')
                data['unit_id'] = request.unit_id
            if request.status:
                data['progress'][data['unit_id']] = request.status
            for key in ('route', 'depth', 'browse_page'):
                value = getattr(request, key)
                if value is not None:
                    if key == 'browse_page':
                        region(service.load_doc(doc_id), value)
                    data[key] = value
            data['revision'] += 1; write(doc_id, data)
            return public(data)

    @app.delete('/api/documents/{doc_id}/reading')
    def clear_session(doc_id: str):
        with lock:
            data = current(doc_id)
            for job in data['jobs'].values():
                task = running.get(task_key(doc_id, job['id']))
                if task:
                    task.get_loop().call_soon_threadsafe(task.cancel)
            # Archive the session and summaries, never overwrite a user's reading record.
            path(doc_id).replace(service.doc_dir(doc_id) / f"reading-archive-{data['id']}.json")
            return {'ok': True}

    @app.get('/api/documents/{doc_id}/reading/jobs/{job_id}')
    def get_job(doc_id: str, job_id: str):
        with lock:
            job = current(doc_id)['jobs'].get(job_id)
            if not job:
                raise HTTPException(404, '陪读任务不存在')
            return job

    @app.delete('/api/documents/{doc_id}/reading/jobs/{job_id}')
    def cancel_job(doc_id: str, job_id: str):
        with lock:
            data = current(doc_id)
            job = data['jobs'].get(job_id)
            if not job:
                raise HTTPException(404, '任务不存在')
            if job['status'] in ('queued', 'running'):
                job.update(status='cancelled', error='已停止，已取得的材料保留')
                data['revision'] += 1; write(doc_id, data)
                task = running.get(task_key(doc_id, job_id))
                if task:
                    task.get_loop().call_soon_threadsafe(task.cancel)
            return job

    @app.get('/api/documents/{doc_id}/reading/summaries/{summary_id}')
    def download_summary(doc_id: str, summary_id: str):
        with lock:
            data = current(doc_id)
            item = next((s for s in data['summaries'] if s['id'] == summary_id), None)
            if not item:
                raise HTTPException(404, '小结不存在')
            return Response(item['markdown'], media_type='text/markdown; charset=utf-8',
                            headers={'Content-Disposition': f'attachment; filename="reading-{summary_id}.md"'})

    @app.get('/api/documents/{doc_id}/reading/images/{source_id}')
    def source_image(doc_id: str, source_id: str):
        with lock:
            source = current(doc_id)['sources'].get(source_id)
            if not source or 'bbox' not in source:
                raise HTTPException(404, '图片来源不存在')
        return Response(render_region(service, service.load_doc(doc_id), source), media_type='image/png')

    @app.get('/api/documents/{doc_id}/reading/notes')
    def list_notes(doc_id: str):
        with lock:
            notes = reading_notes.load(service, doc_id)['notes']
            return {'notes': [reading_notes.metadata(n) for n in sorted(notes, key=lambda n: n['created'], reverse=True)]}

    @app.post('/api/documents/{doc_id}/reading/notes')
    def favorite(doc_id: str, request: FavoriteRequest):
        with lock:
            session = current(doc_id)
            note_id = digest([session['id'], request.message_id])[:32]
            notes = reading_notes.load(service, doc_id)
            existing = next((n for n in notes['notes'] if n['id'] == note_id), None)
            if existing:
                return existing
            turn = next((t for t in session['messages'] if t['id'] == request.message_id), None)
            if turn is None:
                raise HTTPException(404, '助读回答不存在')
            note = reading_notes.snapshot(service.load_doc(doc_id), session, turn)
            notes['notes'].append(note)
            reading_notes.write(service, doc_id, notes)
            return note

    @app.get('/api/documents/{doc_id}/reading/notes/{note_id}')
    def get_note(doc_id: str, note_id: str):
        with lock:
            return reading_notes.find(reading_notes.load(service, doc_id), note_id)

    @app.get('/api/documents/{doc_id}/reading/notes/{note_id}/download')
    def download_note(doc_id: str, note_id: str):
        with lock:
            note = reading_notes.find(reading_notes.load(service, doc_id), note_id)
            return Response(reading_notes.markdown(note), media_type='text/markdown; charset=utf-8',
                headers={'Content-Disposition': f'attachment; filename="note-{note["id"]}.md"'})

    @app.get('/api/documents/{doc_id}/reading/notes/{note_id}/images/{source_id}')
    def note_image(doc_id: str, note_id: str, source_id: str):
        with lock:
            note = reading_notes.find(reading_notes.load(service, doc_id), note_id)
            source = note['sources'].get(source_id)
            if not source or source.get('doc_id') != doc_id or 'bbox' not in source:
                raise HTTPException(404, '笔记图片来源不存在')
        return Response(render_region(service, service.load_doc(doc_id), source), media_type='image/png')

    @app.delete('/api/documents/{doc_id}/reading/notes/{note_id}')
    def delete_note(doc_id: str, note_id: str):
        with lock:
            notes = reading_notes.load(service, doc_id)
            note = next((n for n in notes['notes'] if n['id'] == note_id), None)
            if note is None and note_id not in notes['deleted_legacy']:
                raise HTTPException(404, '笔记不存在')
            legacy = note_id in notes['deleted_legacy'] or bool(note and note.get('legacy'))
            if legacy:
                # Only application-owned summary names can become filesystem paths.
                if not re.fullmatch(r'[0-9a-f]{32}', note_id):
                    raise HTTPException(409, '旧小结标识无效')
                summary_path = service.doc_dir(doc_id) / 'reading-notes' / (note_id + '.md')
                if summary_path.parent.is_symlink() or summary_path.is_symlink():
                    raise HTTPException(409, '旧小结路径无效')
                # Persist the imported snapshot before removing its old file, so
                # either write or file deletion can be retried without losing it.
                if note_id not in notes['deleted_legacy']:
                    notes['deleted_legacy'].append(note_id)
                reading_notes.write(service, doc_id, notes)
                try:
                    summary_path.unlink(missing_ok=True)
                except OSError:
                    raise HTTPException(503, '旧小结文件未能删除，笔记仍可查看，请重试删除')
                session = read(doc_id)
                if session:
                    session['summaries'] = [s for s in session['summaries'] if s['id'] != note_id]
                    session['revision'] += 1; write(doc_id, session)
            notes['notes'] = [n for n in notes['notes'] if n['id'] != note_id]
            reading_notes.write(service, doc_id, notes)
            return {'ok': True}

    @app.delete('/api/documents/{doc_id}/reading/messages/{message_id}')
    def delete_message(doc_id: str, message_id: str, request: RevisionRequest):
        with lock:
            data = current(doc_id); check_revision(data, request.revision)
            if any(j['status'] in ('queued', 'running') for j in data['jobs'].values()):
                raise HTTPException(409, '请等待分析完成或停止任务后删除回答')
            if not any(t['id'] == message_id for t in data['messages']):
                raise HTTPException(404, '助读回答不存在')
            data['messages'] = [t for t in data['messages'] if t['id'] != message_id]
            remaining_ids = {t['id'] for t in data['messages']}
            data['jobs'] = {jid: j for jid, j in data['jobs'].items() if j.get('message_id') in remaining_ids}
            keep_sources = {sid for t in data['messages'] for sid in reading_notes.source_ids(t)}
            data['sources'] = {sid: s for sid, s in data['sources'].items() if sid in keep_sources}
            data.update(memory=conversation_memory(data['messages']), coverage={}, tool_progress=[],
                        last_context=data['messages'][-1].get('context') if data['messages'] else None)
            data['revision'] += 1; write(doc_id, data)
            return public(data)

    @app.delete('/api/documents/{doc_id}/reading/messages')
    def clear_messages(doc_id: str, request: RevisionRequest):
        with lock:
            data = current(doc_id); check_revision(data, request.revision)
            tasks = [running[task_key(doc_id, j['id'])] for j in data['jobs'].values()
                     if task_key(doc_id, j['id']) in running]
            data.update(id=uuid.uuid4().hex, messages=[], memory='', sources={}, coverage={},
                        jobs={}, last_context=None, tool_progress=[])
            data['revision'] += 1; write(doc_id, data)
            for task in tasks:
                task.get_loop().call_soon_threadsafe(task.cancel)
            return public(data)

    async def execute(doc_id, job_id, request, initial, doc, quoted):
        session_id = initial['id']
        sources = copy.deepcopy(initial['sources'])
        coverage = copy.deepcopy(initial['coverage'])
        turn_coverage = {}
        resources, trace, images, pending_notes = set(), [], [], []
        tool_progress = copy.deepcopy(initial.get('tool_progress', []))
        files = initial['skill']['files']
        workflow = initial.get('workflow')
        mode = reading_workflow.mode_for(request.action, request.mode_id, request.question, bool(request.citations))
        structured = None
        required_missing = []
        required_issues = []
        fulltext = bool(workflow and mode == 'quickread' and request.action != 'test')
        scoped = bool(fulltext or mode == 'citation')
        analysis_progress = {}
        total_chars = sum(len(r['text']) for r in reading_context.rows(doc))
        visual_sources, context_sources, context_map = [], [], []
        visual_cache = {}
        image_bytes = 0
        context = dict(unit_id=initial['unit_id'], route=initial['route'], depth=initial['depth'],
                       selected_block=request.block_id, page=request.page, bbox=request.bbox)
        if scoped or request.action in ('continue', 'start'):
            context.update(selected_block=None, page=None, bbox=None)
        elif not request.block_id and request.page is None and initial.get('last_context'):
            context.update(initial['last_context'])

        def live():
            data = read(doc_id)
            if not data or data['id'] != session_id or data['jobs'].get(job_id, {}).get('status') not in ('queued', 'running'):
                raise asyncio.CancelledError()
            return data

        def checkpoint(stage, **progress):
            with lock:
                data = live()
                analysis_progress.update(progress)
                data['sources'].update(sources); data['coverage'].update(coverage)
                data['tool_progress'] = tool_progress[-24:]
                data['jobs'][job_id].update(status='running', stage=stage, trace=list(trace))
                if analysis_progress:
                    data['jobs'][job_id]['progress'] = dict(analysis_progress)
                write(doc_id, data)

        def add_material(args, limit=PAGE_CHARS):
            value = read_material(doc, initial['structure'], args, limit)
            for row in value['rows']:
                sources[row['id']] = dict(doc_id=doc_id, page=row['page'], block_id=row['id'])
                if mode == 'citation' and row['id'] not in context_sources:
                    context_sources.append(row['id'])
                key = row['id']
                for target in (coverage, turn_coverage):
                    spans = target.setdefault(key, dict(total=row['total_chars'], ranges=[]))['ranges']
                    spans.append([row['offset'], row['end']])
                    merged = []
                    for a, b in sorted(spans):
                        if merged and a <= merged[-1][1]:
                            merged[-1][1] = max(merged[-1][1], b)
                        else:
                            merged.append([a, b])
                    target[key]['ranges'] = merged
            return value

        def run_tool(name, args):
            nonlocal image_bytes
            schema = next((t['function']['parameters']['properties'] for t in TOOLS if t['function']['name'] == name), None)
            if schema is None or not isinstance(args, dict) or set(args) - set(schema):
                raise HTTPException(422, '未知工具或参数')
            if name == 'read_skill':
                resource = args.get('path', '')
                if not isinstance(resource, str) or not resource.startswith('references/') or resource not in files:
                    raise HTTPException(422, '只允许读取固定版本 references 资料')
                resources.add(resource)
                return {'path': resource, 'text': files[resource]}, None
            if name == 'get_structure':
                cursor = args.get('cursor', 0)
                if type(cursor) is not int or cursor < 0:
                    raise HTTPException(422, '目录游标无效')
                structure = initial['structure']; items = structure['units']
                return dict(name=doc['name'], page_count=len(doc['pages']), mode=structure['outline']['label'],
                            units=items[cursor:cursor + 25], captions=structure['captions'][cursor:cursor + 25],
                            next_cursor=cursor + 25 if cursor + 25 < max(len(items), len(structure['captions'])) else None), None
            if name == 'read_material':
                value = add_material(args, max(1, total_chars) if scoped else PAGE_CHARS)
                tool_progress.append(dict(tool=name, arguments=args, next_cursor=value['next_cursor'], total_chars=value['total_chars']))
                return value, None
            if name == 'search_material':
                query = args.get('query')
                if not isinstance(query, str) or not query.strip() or len(query) > 500:
                    raise HTTPException(422, '检索问题需要1到500个字符')
                rows = reading_context.search(doc, query)
                material = [add_material({'block_id': r['id']}, max(1, len(r['text']))) for r in rows]
                return dict(material=material, note='完整原文段落及相邻上下文'), None
            if name == 'render_page':
                source = region(doc, args.get('page'), args.get('bbox'))
                source['render_scale'] = args.get('render_scale', 8 if scoped else 2)
                scale = source['render_scale']
                if type(scale) not in (int, float) or not math.isfinite(scale) or not .1 <= scale <= 8:
                    raise HTTPException(422, '渲染倍率必须在0.1到8之间')
                if args.get('view', 'source') not in ('source', 'translation'):
                    raise HTTPException(422, '页面视图无效')
                if args.get('view') == 'translation':
                    revision = args.get('revision')
                    if not isinstance(revision, str):
                        raise HTTPException(422, '查看译文图像需要有效的排版版本')
                    service.layout_file(doc_id, revision)
                    source.update(view='translation', revision=revision)
                source_id = 'image-' + digest(source)[:20]
                if source_id in visual_cache:
                    return dict(id=source_id, source=source, cached=True, note='同一图像已经提供，请使用已有图像'), None
                sources[source_id] = {**source, 'provided': False}
                try:
                    png = render_region(service, doc, source)
                except (OSError, RuntimeError, ValueError):
                    raise HTTPException(422, f"第 {source['page']} 页指定区域无法渲染，请检查原始PDF或排版版本")
                if image_bytes + len(png) > MAX_IMAGE_BYTES:
                    raise HTTPException(413, '本轮页面图像超过本机 64 MiB 容量，请按章节或选区阅读')
                image_bytes += len(png)
                sources[source_id] = {**source, 'provided': True}
                required_issues[:] = [i for i in required_issues if source_id not in i['source_ids']]
                visual_cache[source_id] = True
                (visual_sources if scoped else images).append(source_id)
                return dict(id=source_id, source=source, note='已提供实际图像，请结合全文判读，具体歧义写入issues'), png
            if name == 'save_summary':
                if request.action != 'summary':
                    raise HTTPException(422, '请等待用户点击生成小结')
                if not {'references/summary-template.md', 'references/question-checklist.md'} <= resources:
                    raise HTTPException(422, '请先读取小结模板和提问清单')
                text = args.get('markdown')
                if not isinstance(text, str) or not 1 <= len(text) <= 60000:
                    raise HTTPException(422, '小结长度无效')
                validate_answer(text, sources)
                sid = uuid.uuid4().hex
                pages = sorted({s['page'] for s in sources.values()})
                text += '\n\n## 阅读覆盖说明\n\n已取得材料涉及页：' + '、'.join(map(str, pages)) + '。这不代表整页或全文已读完。\n'
                text += '\n已完成单元：' + str(sum(v == 'completed' for v in initial['progress'].values())) + '/' + str(len(initial['structure']['units'])) + '\n'
                for ref in dict.fromkeys(re.findall(r'\[\[([^\[\]\n]+)\]\]', text)):
                    s = sources[ref]
                    text = text.replace('[[' + ref + ']]', f"（原文第 {s['page']} 页，{s.get('block_id', '图像区域')}）")
                pending_notes.append(dict(id=sid, created=now(), markdown=text))
                return dict(id=sid, status='本轮成功后保存新版本'), None
            raise HTTPException(422, '未知工具')

        def image_message(value, png):
            return {'role': 'user', 'content': [
                {'type': 'text', 'text': json.dumps(value, ensure_ascii=False)},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(png).decode()}}]}

        async def prepare_fulltext():
            checkpoint('正在准备全文原文和页面图', phase='preparing', percent=0,
                       prepared_pages=0, total_pages=len(doc['pages']))
            material = add_material({}, max(1, total_chars))
            prepared = []
            for index, page in enumerate(doc['pages']):
                try:
                    value, png = await asyncio.to_thread(run_tool, 'render_page', {'page': page['number'], 'render_scale': 2})
                except HTTPException as error:
                    raise HTTPException(error.status_code, f"第 {page['number']} 页页面图准备失败。{error.detail}")
                prepared.append(image_message(value, png))
                trace.append(dict(name='prepare_page_image', status='ok', arguments=dict(page=page['number'])))
                checkpoint(f"页面图已准备 {index+1}/{len(doc['pages'])} 页", prepared_pages=index+1,
                           percent=int(70*(index+1)/len(doc['pages'])))
                await asyncio.sleep(0)
            return [material], prepared

        async def execute_calls(messages, response, calls, allowed):
            messages.append({**response, 'tool_calls': calls})
            image_messages = []
            for call in calls:
                name = call['function']['name']
                args = json.loads(call['function']['arguments'])
                png = None
                try:
                    if name not in allowed:
                        raise HTTPException(422, '本阶段只允许指定的补读或区域查看工具')
                    if name == 'render_page':
                        value, png = await asyncio.to_thread(run_tool, name, args)
                    else:
                        value, png = run_tool(name, args)
                    trace.append(dict(name=name, status='ok', arguments={k:v for k,v in args.items() if k != 'markdown'}))
                except HTTPException as error:
                    value = dict(error=error.detail)
                    trace.append(dict(name=name, status='error', error=error.detail))
                    failed = [sid for sid, source in sources.items() if source.get('provided') is False and source['page'] == args.get('page')]
                    if name == 'render_page' and failed:
                        required_issues.append(dict(category='source_unavailable', page=args['page'], source_ids=[failed[-1]],
                            description=error.detail, impact='该区域的视觉内容尚待确认'))
                        value.update(id=failed[-1], source=sources[failed[-1]])
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(value, ensure_ascii=False)})
                if png:
                    image_messages.append(image_message(value, png))
                checkpoint('已处理 ' + name)
            messages.extend(image_messages)

        async def analyze_scoped(messages):
            allowed_names = {'render_page'} if fulltext else {'render_page', 'read_material', 'search_material'}
            allowed_tools = [t for t in TOOLS if t['function']['name'] in allowed_names]
            for index in range(4):
                checkpoint('正在整体分析与视觉核查' if fulltext else '正在结合引用上下文分析',
                           phase='inspecting', percent=None, round=index+1, max_rounds=4)
                response = await invoke(service, request.agent, messages, allowed_tools)
                calls = response.pop('tool_calls', [])
                if len(calls) > 12:
                    raise HTTPException(502, '单轮补读或视觉查看超过12项，请检查模型工具协议后重试')
                if not calls:
                    messages.append(response)
                    break
                await execute_calls(messages, response, calls, allowed_names)
            messages.append({'role': 'user', 'content':
                '进入最终回答阶段。结合以上全部原始材料、图像和补读上下文直接生成工作流 JSON。'
                '本阶段关闭工具。具体疑点写入 issues，missing 保持空数组。后端取材问题如下\n'
                + json.dumps(required_issues,ensure_ascii=False)})
            checkpoint('正在生成最终回答', phase='answering', percent=None)
            response = await invoke(service, request.agent, messages, [])
            if response.get('tool_calls'):
                response = await invoke(service, request.agent, messages + [{'role': 'user', 'content':
                    '请仅返回工作流 JSON，所需工具已关闭，尚有疑点写入 issues。'}], [])
            if response.get('tool_calls'):
                raise HTTPException(502, '模型仍未返回最终回答，请检查接口的无工具回答能力后重试')
            return response

        try:
            checkpoint('加载固定版本 Skill')
            if workflow or mode == 'citation':
                workflow = reading_workflow.effective(workflow, service.ROOT, mode)
            presentation = reading_workflow.presentation(service.ROOT)
            system = files['SKILL.md'] + '\n\n' + files['adapter.md'] + '\n\n' + presentation['text']
            if workflow and request.action != 'test':
                system += '\n\n' + reading_workflow.instructions(workflow, mode)
                for reference in reading_workflow.domain_references(doc):
                    if reference in files:
                        system += '\n\n' + files[reference]
                        resources.add(reference)
                if mode == 'detail' or request.action == 'summary':
                    system += '\n\n' + files['references/question-checklist.md']
                    resources.add('references/question-checklist.md')
            if request.action == 'test':
                system += '\n本轮为能力测试：先调用 get_structure，再 render_page 查看第1页，然后用一句话描述看到的实际内容。'
            if scoped:
                resources.add('reading-workflows/' + workflow['manifest']['modes'][mode]['file'])
                system += ('\n\n本轮为统一上下文阅读。selected_material 提供完整原始材料，后续消息提供页面图。'
                           '结合全部已有材料整体分析；需要时请求指定补读或局部放大工具。'
                           '准备完成后可以简短确认，应用会进入关闭工具的最终回答阶段。最多四轮，每轮最多12项操作。'
                           '本轮规则优先于旧版先读取速览片段或重复请求目录的约定。')
            actions = {'start': '分析整篇论文，生成文章速读并给出建议阅读路线。', 'continue': '继续讲解当前阅读单元，讲完停下来。',
                       'simple': '把当前讨论内容讲直白一点。', 'deep': '深入解释当前公式或机制，必要时看图。',
                       'visual': '查看指定页面或区域并解释图表／公式。', 'summary': '根据已读材料生成并保存阅读小结。',
                       'test': '测试工具调用与实际图片输入能力。', 'message': ''}
            instruction = request.question or actions[request.action]
            materials, prepared_images = [], []
            for quote in quoted:
                sources[quote['id']] = quote['source']
            if workflow and request.action != 'test':
                if fulltext:
                    required = [r['id'] for r in reading_context.rows(doc)]
                    materials, prepared_images = await prepare_fulltext()
                elif mode == 'citation':
                    selected, context_map, targets = reading_context.surrounding(doc, initial['structure'], quoted, request.question, service)
                    required = [r['id'] for r in selected]
                    checkpoint('正在准备引用上下文', phase='context', percent=None)
                    materials = [add_material({'block_id': r['id']}, max(1, len(r['text']))) for r in selected]
                    for target in targets:
                        value, png = await asyncio.to_thread(run_tool, 'render_page', target)
                        if png:
                            prepared_images.append(image_message(value, png))
                else:
                    required, required_missing = reading_workflow.material_plan(doc, initial['structure'], mode, context['unit_id'], context.get('selected_block'))
                    # Interactive modes keep a bounded first batch and cursor-based tools.
                    budget = 24000
                    for block_id in required:
                        if budget <= 0:
                            break
                        material = add_material({'block_id': block_id})
                        materials.append(material); budget -= sum(len(r['text']) for r in material['rows'])
                required_rows = {b['id']: b for p in doc['pages'] for b in p['blocks'] if b['id'] in required}
            else:
                required_rows = {}
            if context.get('selected_block'):
                materials.append(add_material({'block_id': context['selected_block']}))
            messages = [{'role': 'system', 'content': system}]
            if initial['memory'] and not fulltext:
                messages.append({'role': 'user', 'content': '较早对话的节选记忆（不是原文证据）：\n' + initial['memory']})
            for turn in ([] if fulltext else initial['messages'][-6:]):
                prior_question = json.dumps(dict(question=turn['question'], citations=turn['citations']), ensure_ascii=False) if turn.get('citations') else turn['question']
                messages.extend([{'role': 'user', 'content': prior_question}, {'role': 'assistant', 'content': turn['answer']}])
            messages.append({'role': 'user', 'content': json.dumps(dict(instruction=instruction, context=context,
                name=doc['name'], structure=initial['structure']['units'] if scoped else initial['structure']['units'][:25],
                structure_has_more=not scoped and len(initial['structure']['units']) > 25,
                domain_index=files['references/domains/_index.md'], selected_material=materials,
                context_map=context_map, material_coverage=dict(text_sources=list(turn_coverage), image_sources=visual_sources),
                citations=quoted, required_material_ids=list(required_rows),
                material_gaps=required_missing), ensure_ascii=False)})
            messages.extend(prepared_images)
            if request.action == 'visual' and context.get('page'):
                value, png = run_tool('render_page', {'page': context['page'], 'bbox': context.get('bbox') or [0, 0, 1, 1]})
                messages.append({'role': 'user', 'content': [{'type': 'text', 'text': json.dumps(value)},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(png).decode()}}]})
                trace.append(dict(name='render_page', status='ok'))
            answer, limited, tool_count = '', False, 0
            for _ in range(MAX_TOOLS + 1):
                checkpoint('模型正在阅读／组织回答')
                response = await analyze_scoped(messages) if scoped else await invoke(service, request.agent, messages, TOOLS)
                calls = response.pop('tool_calls', [])
                if not calls:
                    answer = response['content']
                    if workflow and request.action != 'test':
                        unread = [bid for bid,b in required_rows.items() if b['text'] and turn_coverage.get(bid,{}).get('ranges') != [[0,len(b['text'])]]]
                        gaps = required_missing + (['必读原文尚未完整获取：'+'、'.join(unread)] if unread else [])
                        provided_ids = set(turn_coverage) | set(visual_sources) | {q['id'] for q in quoted}
                        provided_ids.update(i for issue in required_issues for i in issue['source_ids'])
                        validation_sources = {i:s for i,s in sources.items() if not scoped or i in provided_ids}
                        try:
                            structured, answer = reading_workflow.validate(answer, workflow, mode, validation_sources, gaps, required_issues)
                        except HTTPException as error:
                            messages.extend([response, {'role':'user','content':error.detail+'。仅修复一次，请按工作流 JSON 重写；不要请求工具。'}])
                            repaired = await invoke(service, request.agent, messages, [])
                            if repaired.get('tool_calls'):
                                raise HTTPException(502, '工作流修复仍返回工具请求，本轮未完成')
                            structured, answer = reading_workflow.validate(repaired['content'], workflow, mode, validation_sources, gaps, required_issues)
                    break
                if tool_count + len(calls) > MAX_TOOLS:
                    limited = True; break
                tool_count += len(calls)
                messages.append({**response, 'tool_calls': calls})
                image_messages = []
                for call in calls:
                    name = call['function']['name']; png = None
                    args = json.loads(call['function']['arguments'])
                    trace_args = {k: v for k, v in args.items() if k in ('path', 'page', 'unit_id', 'block_id', 'cursor', 'bbox')}
                    try:
                        value, png = run_tool(name, args)
                        trace.append(dict(name=name, status='ok', arguments=trace_args))
                    except HTTPException as error:
                        value = {'error': error.detail}
                        trace.append(dict(name=name, status='error', error=error.detail))
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(value, ensure_ascii=False)})
                    if png:
                        image_messages.append({'role': 'user', 'content': [{'type': 'text', 'text': f"工具图像来源 [[{value['id']}]]"},
                            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(png).decode()}}]})
                    checkpoint('已执行 ' + name)
                messages.extend(image_messages)
                if tool_count == MAX_TOOLS:
                    limited = True; break
            if limited:
                answer = '本轮已达到工具调用上限，已取得的材料已保存，讲解尚未完成。请点击继续或缩小阅读范围。'
            elif request.action != 'test' and not sources:
                raise HTTPException(502, '模型未取得任何原文或图像材料，请测试工具调用能力后重试')
            if request.action == 'summary' and not limited and not pending_notes:
                raise HTTPException(502, '模型未调用小结保存工具，本轮没有生成小结文件，可重试')
            refs = validate_answer(answer, sources)
            if request.action == 'test' and not limited:
                names = {t['name'] for t in trace if t['status'] == 'ok'}
                if not {'get_structure', 'render_page'} <= names:
                    raise HTTPException(502, '模型未完成工具／图片测试，不能确认能力；未将文字回答视为通过')
                answer = '工具与图片请求已执行；请人工核对以下描述是否符合原图（仅此不保证视觉理解正确）。\n\n' + answer
            with lock:
                data = live()
                data['sources'].update(sources); data['coverage'].update(coverage)
                data['last_context'] = context
                turn = dict(id=uuid.uuid4().hex, created=now(), question=instruction, answer=answer, refs=refs,
                            format_version=presentation['version'],
                            mode_id=mode, structured=structured, citations=quoted,
                            context_sources=context_sources, context_map=context_map, visual_sources=visual_sources,
                            workflow_version=workflow['version'] if workflow else None,
                            skill_resources=sorted(resources | {'SKILL.md', 'adapter.md', 'references/domains/_index.md'}),
                            images=list(dict.fromkeys(images)), context=context, skill_version=initial['skill']['version'],
                            config_key=digest(request.agent.model_dump(exclude={'api_key', 'timeout'})), trace=trace)
                if fulltext and not limited:
                    turn['analysis_scope'] = dict(total_pages=len(doc['pages']),
                        text_pages=[p['number'] for p in doc['pages'] if any(b['text'].strip() for b in p['blocks'])],
                        image_pages=list(dict.fromkeys(sources[sid]['page'] for sid in visual_sources if sources[sid]['bbox']==[0,0,1,1])),
                        mode='full_document_multimodal', note='完整原文与全部页面图已共同提供给模型，具体疑点见待核实事项。')
                turn['source_snapshot'] = {sid:copy.deepcopy(sources[sid]) for sid in reading_notes.source_ids(turn) if sid in sources}
                data['messages'].append(turn)
                data['mode_id'] = mode
                data['memory'] = conversation_memory(data['messages'])
                for note in pending_notes:
                    folder = service.doc_dir(doc_id) / 'reading-notes'; folder.mkdir(exist_ok=True)
                    (folder / (note['id'] + '.md')).write_text(note['markdown'], encoding='utf-8')
                    data['summaries'].append(note)
                data['jobs'][job_id].update(status='limited' if limited else 'completed', stage='本轮完成', trace=trace,
                                           message_id=turn['id'])
                if scoped and not limited:
                    analysis_progress.update(phase='completed', percent=100)
                    data['jobs'][job_id]['progress'] = dict(analysis_progress)
                data['revision'] += 1; write(doc_id, data)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            with lock:
                try:
                    data = live()
                    data['jobs'][job_id].update(status='failed', error=error.detail if isinstance(error, HTTPException) else '陪读执行失败，可重试；旧记录已保留', trace=trace)
                    data['revision'] += 1; write(doc_id, data)
                except asyncio.CancelledError:
                    pass
        finally:
            running.pop(task_key(doc_id, job_id), None)

    @app.post('/api/documents/{doc_id}/reading/messages')
    async def start_message(doc_id: str, request: MessageRequest):
        with lock:
            data = current(doc_id); check_revision(data, request.revision)
            if request.action == 'message' and not request.question.strip():
                raise HTTPException(422, '请输入问题')
            doc = service.load_doc(doc_id)
            if request.block_id:
                service.find_block(doc, request.block_id)
            if request.mode_id and request.mode_id != 'citation' and not request.citations and not data.get('workflow'):
                raise HTTPException(409, '此会话使用旧版 Skill，请归档后新建会话启用规范阅读方式')
            if (data.get('workflow') or request.citations) and request.action in ('message', 'visual'):
                request.mode_id = reading_workflow.mode_for(request.action, request.mode_id, request.question, bool(request.citations))
            if request.mode_id == 'citation' and not request.citations:
                raise HTTPException(422, '引用问答需要至少一条有效原文或译文引用')
            quoted = [resolve_citation(service, doc_id, citation) for citation in request.citations]
            if request.page is not None:
                region(doc, request.page, request.bbox)
            elif request.bbox is not None:
                raise HTTPException(422, '区域必须提供页码')
            if any(j['status'] in ('queued', 'running') for j in data['jobs'].values()):
                raise HTTPException(409, '上一轮陪读仍在运行，请等待或停止')
            existing_analysis = service.analysis_service['running']
            count = len(running) + len(existing_analysis)
            if count >= 12:
                raise HTTPException(429, '理解任务繁忙，请稍后重试')
            if sum(1 for key in existing_analysis if key[0] == str(service.doc_dir(doc_id).resolve())) >= 3:
                raise HTTPException(429, '当前论文已有三个理解任务，请稍后重试')
            job_id = uuid.uuid4().hex
            job = dict(id=job_id, status='queued', action=request.action, question=request.question,
                       block_id=request.block_id, page=request.page, bbox=request.bbox, created=now(), trace=[])
            job['progress'] = dict(phase='preparing', percent=0)
            job.update(mode_id=request.mode_id, citations=[c.model_dump() for c in request.citations])
            data['jobs'][job_id] = job; data['revision'] += 1; write(doc_id, data)
            running[task_key(doc_id, job_id)] = asyncio.create_task(
                execute(doc_id, job_id, request, copy.deepcopy(data), doc, quoted))
            return job

    async def shutdown():
        tasks = list(running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    app.router.on_shutdown.insert(0, shutdown)
    return {'running': running}
