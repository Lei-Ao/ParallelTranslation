"""Versioned Skill workflow selection, material requirements and result validation."""
import json
import re
import copy
from fastapi import HTTPException
from paper_analysis import digest


def snapshot(root):
    folder = root / 'skills' / 'reading-workflows'
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    files = {mode['file']: (folder / mode['file']).read_text(encoding='utf-8') for mode in manifest['modes'].values()}
    return dict(version=digest([manifest, files]), manifest=manifest, files=files)


def presentation(root):
    text = (root / 'skills' / 'reading-workflows' / 'formatting.md').read_text(encoding='utf-8')
    return dict(version=digest(text), text=text)


def effective(workflow, root, mode):
    if mode not in ('quickread', 'citation'):
        return workflow
    current = snapshot(root)
    result = copy.deepcopy(workflow or current)
    spec = current['manifest']['modes'][mode]
    result['manifest']['modes'][mode] = spec
    result['files'][spec['file']] = current['files'][spec['file']]
    result['version'] = digest([result['manifest'], result['files']])
    return result


def mode_for(action, explicit=None, question='', citations=False):
    # The composer labels ordinary messages as followup. Honor an explicit
    # whole-paper request in that message without routing local questions by topic.
    if action in ('message', 'visual') and explicit in (None, 'followup', 'citation'):
        text = re.sub(r'\s+', '', question).lower()
        text = re.sub(r'^(?:(?:请|帮我|麻烦你|麻烦|先|重新|再|直接))+', '', text)
        scope = r'(?:整篇(?:论文|文章|文献)?|全文|整份(?:论文|文章|文献|文档))'
        operation = r'(?:分析|阅读|通读|解读|总结|概括|概述|梳理|速读|速览)'
        patterns = [
            r'^' + operation + r'(?:一下|这篇|这份|这|该)?' + scope,
            r'^(?:对|把|将)?' + scope + r'(?:进行|做|作)?(?:整体|全面|系统|详细)?' + operation,
            r'^(?:(?:生成|进行|做|开始|给出)(?:一[份次个])?)?(?:文章|论文)(?:速读|速览|概览)(?:[，。！？,!?.]|$)',
            r'^(?:please)?(?:analy[sz]e|summari[sz]e|read|review)(?:the)?(?:entire|whole|full)(?:paper|article|document)',
        ]
        if any(re.search(pattern, text) for pattern in patterns):
            return 'quickread'
    if citations and action in ('message', 'visual') and explicit in (None, 'followup', 'citation'):
        return 'citation'
    return explicit or {'start': 'quickread', 'continue': 'guided'}.get(action, 'followup')


def domain_references(doc):
    """Conservative domain hints; unmatched disciplines use the generic adapter."""
    text=' '.join(b['text'] for p in doc['pages'][:2] for b in p['blocks']).lower()
    names=[]
    for name,pattern in [('control-dl', r'\breinforcement learning\b|\bmodel predictive control\b|强化学习|系统辨识'),
                         ('audio-visual', r'\baudio.visual\b|\bvisual sound\b|视听'),
                         ('acoustic-imaging', r'\bbeamforming\b|\bmicrophone array\b|波束形成|麦克风阵列')]:
        if re.search(pattern,text):names.append(name)
    if not names and re.search(r'\bdeep learning\b|\bneural network\b|深度学习|神经网络',text):names.append('general-dl')
    return ['references/domains/'+name+'.md' for name in names]


def material_plan(doc, structure, mode, unit_id, block_id=None):
    rows = [(p['number'], b) for p in doc['pages'] for b in p['blocks']]
    selected, missing = set(), []
    if mode == 'guided':
        unit = next(u for u in structure['units'] if u['id'] == unit_id)
        selected.update(unit['block_ids'])
        if not selected:
            missing.append('当前单元没有可提取文字，需要查看页面图像')
    elif mode == 'quickread':
        selected.update(b['id'] for _, b in rows)
    elif mode == 'detail':
        if rows:
            selected.add(rows[0][1]['id'])
        else:
            missing.append('没有可提取的标题与正文，需要逐页视觉阅读')
        patterns = [('摘要', r'\babstract\b|摘要'), ('引言与贡献', r'\bintroduction\b|\bcontributions?\b|引言|贡献'),
                    ('结论', r'\bconclusions?\b|结论|总结')]
        patterns += [('方法', r'\bmethods?\b|\bapproach\b|方法'), ('证据', r'\bexperiments?\b|\bresults?\b|实验|结果|证明'),
                     ('局限', r'\blimitations?\b|\bdiscussion\b|局限|讨论')]
        for label, pattern in patterns:
            matches = [u for u in structure['units'] if re.search(pattern, u['title'], re.I)]
            if matches:
                selected.update(b for u in matches for b in u['block_ids'])
            else:
                indexes = [i for i, (_, b) in enumerate(rows) if re.search(pattern, b['text'][:300], re.I)]
                if indexes:
                    for i in indexes:
                        selected.update(b['id'] for _, b in rows[i:i+3])
                else:
                    missing.append(f'未可靠识别{label}材料')
        selected.update(c['block_id'] for c in structure['captions'])
    elif block_id:
        selected.add(block_id)
    else:
        unit = next(u for u in structure['units'] if u['id'] == unit_id)
        selected.update(unit['block_ids'])
    return [b['id'] for _, b in rows if b['id'] in selected], missing


def instructions(workflow, mode):
    spec = workflow['manifest']['modes'][mode]
    text = workflow['files'][spec['file']]
    if mode == 'quickread':
        # Apply the current product contract to older pinned workflow snapshots too.
        text += ('\n\n文章速读运行规则更新：结合整篇论文原文和全部页面图进行整体分析。'
                 '正文取消150字及其他总字数限制，'
                 '按论文内容充分展开研究问题、主要做法、核心结论、建议阅读路线。'
                 '此规则优先于上述旧版第一遍选材与字数约定。结合已取得的全部材料判断具体疑点。')
    if mode in ('quickread', 'citation'):
        text += ('\n最终待核实事项写入 issues 数组，每项为 '
                 '{"category":"source_unavailable|visual_ambiguity|paper_not_specified",'
                 '"page":1,"source_ids":["实际提供的来源ID"],"description":"具体疑点","impact":"影响"}。'
                 'source_unavailable 仅复述后端已经报告的取材失败；visual_ambiguity 必须对应实际提供的图像；'
                 'paper_not_specified 需结合本轮已取得的全部相关上下文判定。没有疑点时 issues=[]、missing=[]。'
                 '将局部未提及某事、内部执行进度写成全文缺口会校验失败。')
    gap_instruction = ('有具体待核实事项时记录 issues 并使用 status=incomplete，missing 保持空数组。'
                       if mode in ('quickread', 'citation') else
                       '资料尚未读完、没有提取出的必读部分必须保留在 missing，status=incomplete。')
    return text + '\n\nWORKFLOW_RESPONSE_JSON\n' + (
        '最终回答必须为 JSON，不是 Markdown 字符串。结构：'
        '{"status":"complete|incomplete","sections":[{"section":"规定标题","text":"正文（可含Markdown公式）",'
        '"kind":"claim|explanation|missing","source_ids":["实际取得的来源ID"]}],"missing":["具体缺口"]}。'
        'sections 按顺序且仅包含以下标题：' + '、'.join(spec['fields']) + '。'
        'claim 必须有来源；辅助解释必须标记 explanation，不冒充原文。没有依据使用 missing。'
        + gap_instruction +
        '不允许通过普通自由文本绕过工作流。引用写 source_ids，不在正文伪造链接。')


def validate(raw, workflow, mode, sources, required_missing, required_issues=None):
    try:
        value = json.loads(raw.strip().removeprefix('```json').removesuffix('```').strip())
        sections = value['sections']; missing = value['missing']
        spec = workflow['manifest']['modes'][mode]
        if not isinstance(sections, list) or [s['section'] for s in sections] != spec['fields']:
            raise ValueError('回答栏目不符合 Skill 工作流')
        if value['status'] not in ('complete', 'incomplete') or not isinstance(missing, list) or any(not isinstance(m, str) or len(m)>1000 for m in missing):
            raise ValueError('完成状态格式无效')
        for item in sections:
            if (not isinstance(item['text'], str) or not item['text'].strip()
                    or (mode != 'quickread' and len(item['text']) > 12000)):
                raise ValueError('正文为空或过长')
            if item['kind'] not in ('claim', 'explanation', 'missing') or not isinstance(item['source_ids'], list):
                raise ValueError('证据类型无效')
            if any(not isinstance(i, str) or i not in sources for i in item['source_ids']):
                raise ValueError('引用未提供的来源')
            if item['kind'] == 'claim' and not item['source_ids']:
                raise ValueError('论文陈述缺少来源')
        if mode != 'quickread' and spec.get('max_chars') and sum(len(re.sub(r'\s', '', s['text'])) for s in sections) > spec['max_chars']:
            raise ValueError('正文超过工作流长度上限')
        issues = value.get('issues', [])
        if not isinstance(issues, list):
            raise ValueError('待核实事项格式无效')
        for issue in issues:
            if (issue['category'] not in ('source_unavailable', 'visual_ambiguity', 'paper_not_specified')
                    or type(issue['page']) is not int or not isinstance(issue['source_ids'], list)
                    or not issue['source_ids'] or any(not isinstance(i, str) or i not in sources for i in issue['source_ids'])
                    or not any(sources[i]['page'] == issue['page'] for i in issue['source_ids'])
                    or any(not isinstance(issue[k], str) or not issue[k].strip() or len(issue[k])>1200 for k in ('description', 'impact'))):
                raise ValueError('待核实事项必须有实际来源、页码、具体疑点和影响')
            if issue['category'] == 'visual_ambiguity' and not any('bbox' in sources[i] and sources[i].get('provided') is not False for i in issue['source_ids']):
                raise ValueError('视觉疑点缺少实际提供的图像')
            if issue['category'] == 'source_unavailable' and not any(
                    i['page']==issue['page'] and set(i['source_ids'])==set(issue['source_ids']) for i in (required_issues or [])):
                raise ValueError('取材失败应依据后端记录')
        if mode in ('quickread', 'citation') and missing:
            raise ValueError('请将具体待核实事项写入 issues，missing 保持空数组')
        if mode in ('quickread', 'citation') and any(s['kind']=='missing' for s in sections) and not issues and not required_issues:
            raise ValueError('缺少依据的栏目需要提供有具体位置的 issues')
        merged = {(i['category'],i['page'],tuple(sorted(i['source_ids']))):i for i in issues + (required_issues or [])}
        value['issues'] = list(merged.values())
        issue_gaps = [f"第 {i['page']} 页｜{i['description']}。{i['impact']}" for i in value['issues']]
        gaps = list(dict.fromkeys(required_missing + missing + issue_gaps + [s['section']+'缺少依据' for s in sections if s['kind']=='missing']))
        if gaps and value['status'] != 'incomplete':
            raise ValueError('有取材缺口，不能标记完整完成')
        value['missing'] = gaps
        markdown = '\n\n'.join('### '+s['section']+'\n\n'+('辅助解释：' if s['kind']=='explanation' else '')+s['text']+' '+
                                ' '.join('[['+i+']]' for i in s['source_ids']) for s in sections)
        if gaps:
            markdown += '\n\n**尚未完成／依据缺口**\n\n'+'\n'.join('- '+m for m in gaps)
        return value, markdown
    except (ValueError, TypeError, KeyError) as error:
        raise HTTPException(502, 'Skill 工作流校验失败：' + str(error))
