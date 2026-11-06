"""Independent local snapshots of favorited reading answers and legacy summaries."""
import copy
from datetime import datetime, timezone
import json
import re

from fastapi import HTTPException
from paper_analysis import digest, now


def source_ids(turn):
    ids = list(turn.get('refs', [])) + list(turn.get('images', []))
    ids += list(turn.get('visual_sources', [])) + list(turn.get('context_sources', []))
    ids += [q['id'] for q in turn.get('citations', [])]
    for section in (turn.get('structured') or {}).get('sections', []):
        ids.extend(section.get('source_ids', []))
    for issue in (turn.get('structured') or {}).get('issues', []):
        ids.extend(issue['source_ids'])
    ids += re.findall(r'\[\[([^\[\]\n]+)\]\]', turn.get('answer', ''))
    return list(dict.fromkeys(ids))


def load(service, doc_id):
    directory = service.doc_dir(doc_id)
    path = directory / 'reading-notes.json'
    data = dict(version=1, notes=[], deleted_legacy=[])
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if (data['version'] != 1 or not isinstance(data['notes'], list)
                    or not isinstance(data['deleted_legacy'], list)
                    or any(not isinstance(i, str) or not re.fullmatch(r'[0-9a-f]{32}', i)
                           for i in data['deleted_legacy'])):
                raise ValueError()
            for note in data['notes']:
                if (not isinstance(note['id'], str) or not re.fullmatch(r'[0-9a-f]{32}', note['id'])
                        or not isinstance(note['created'], str) or not isinstance(note['title'], str)
                        or not isinstance(note['turn']['answer'], str) or not isinstance(note['sources'], dict)):
                    raise ValueError()
        except (ValueError, KeyError, TypeError, OSError):
            raise HTTPException(409, '本地笔记无法读取或版本不兼容，原文件已保留')
    known = {n['id'] for n in data['notes']} | set(data['deleted_legacy'])
    legacy_directory = directory / 'reading-notes'
    if legacy_directory.is_symlink():
        return data
    for path in sorted(legacy_directory.glob('*.md')):
        if path.is_symlink() or not re.fullmatch(r'[0-9a-f]{32}', path.stem) or path.stem in known:
            continue
        try:
            markdown = path.read_text(encoding='utf-8')
            created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except (OSError, UnicodeError, ValueError, OverflowError):
            raise HTTPException(409, f'旧小结 {path.name} 无法读取，原文件已保留')
        title = next((line.lstrip('# ').strip() for line in markdown.splitlines() if line.strip()), '阅读小结')
        data['notes'].append(dict(id=path.stem, title=title[:80], created=created,
            origin_session_id=None, origin_message_id=None, legacy=True, sources={},
            turn=dict(answer=markdown, question='阅读小结', created=created, mode_id='summary')))
    return data


def write(service, doc_id, data):
    path = service.doc_dir(doc_id) / 'reading-notes.json'
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    except OSError:
        raise HTTPException(503, '本地笔记写入失败，已有笔记已保留，请重试')


def find(data, note_id):
    note = next((n for n in data['notes'] if n['id'] == note_id), None)
    if note is None:
        raise HTTPException(404, '笔记不存在')
    return note


def snapshot(doc, session, turn):
    title = re.sub(r'\s+', ' ', turn.get('question', '')).strip() or 'AI 助读笔记'
    available = turn.get('source_snapshot', session['sources'])
    sources = {sid: copy.deepcopy(available[sid]) for sid in source_ids(turn) if sid in available}
    if 'source_snapshot' not in turn:
        for source in sources.values():
            source.pop('provided', None)
    for quote in turn.get('citations', []):
        sources[quote['id']] = copy.deepcopy(quote['source'])
    return dict(id=digest([session['id'], turn['id']])[:32], title=title[:80], created=now(),
        doc_id=doc['id'], doc_name=doc['name'], document_digest=session['document_digest'],
        origin_session_id=session['id'], origin_message_id=turn['id'],
        turn=copy.deepcopy(turn), sources=sources)


def metadata(note):
    text = re.sub(r'\[\[([^\[\]\n]+)\]\]', '', note['turn']['answer'])
    text = re.sub(r'(?m)^#{1,6}\s+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return {**{k: note.get(k) for k in ('id', 'title', 'created', 'origin_session_id', 'origin_message_id')},
            'preview': text[:140]}


def markdown(note):
    turn, sources = note['turn'], note['sources']
    ids = source_ids(turn)
    def reference(match):
        sid = match.group(1)
        return f'[{ids.index(sid) + 1}]' if sid in sources else '（来源不可用）'
    answer = re.sub(r'\[\[([^\[\]\n]+)\]\]', reference, turn['answer'])
    parts = ['# ' + note['title'], '收藏时间 ' + note['created']]
    if note.get('doc_name'):
        parts.append('论文 ' + note['doc_name'])
    if turn.get('question'):
        parts.append('## 提问\n\n' + turn['question'])
    parts.append('## AI 助读内容\n\n' + answer)
    for quote in turn.get('citations', []):
        label = '译文' if quote['source'].get('view') == 'translation' or quote['source'].get('translated') else '原文'
        parts.append('## 引用' + label + '\n\n' + quote['text'])
        if quote.get('context_ambiguous'):
            parts.append('引用定位需核对，分析时已补充页面上下文和对应页面图。')
    if ids:
        lines = []
        for index, sid in enumerate(ids):
            source = sources.get(sid)
            if not source:
                lines.append(f'{index + 1}. 来源不可用（{sid}）')
                continue
            label = '译文' if source.get('view') == 'translation' or source.get('translated') else '原文'
            location = f"{label}第 {source['page']} 页"
            if source.get('block_id'):
                location += '，段落 ' + source['block_id']
            if source.get('bbox'):
                location += '，图像区域 ' + str(source['bbox'])
            if source.get('provided') is False:
                location += '，本次分析获取该图像失败'
            if source.get('rects'):
                location += '，引用区域 ' + str(source['rects'])
            lines.append(f'{index + 1}. {location}')
        parts.append('## 原文来源\n\n' + '\n'.join(lines))
    return '\n\n'.join(parts) + '\n'
