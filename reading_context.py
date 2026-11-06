"""Original-material selection for quotation questions, independent of reader position."""
import re
import pymupdf

from paper_analysis import tokens, retrieve


def rows(doc):
    return [dict(id=b['id'], page=p['number'], text=b['text']) for p in doc['pages'] for b in p['blocks']]


def surrounding(doc, structure, quotes, question='', service=None):
    material = rows(doc)
    positions = {r['id']: i for i, r in enumerate(material)}
    selected, contexts, visual = set(), [], []
    for quote in quotes:
        source = quote['source']
        own = quote.get('block_ids') or ([source['block_id']] if source.get('block_id') else [])
        own = [bid for bid in own if bid in positions]
        ambiguous = bool(quote.get('context_ambiguous')) or not own
        if ambiguous:
            own = [r['id'] for r in material if r['page'] == source['page']]
        indexes = [positions[bid] for bid in own]
        included = set(range(min(indexes), max(indexes)+1)) if indexes else set()
        if indexes:
            included.update(i for i in (min(indexes)-1, max(indexes)+1) if 0 <= i < len(material))
        if service and own:
            # Stored extraction order can interleave columns. Include the nearest
            # paragraphs in the quote's own native column, also on rotated pages.
            page_data = doc['pages'][source['page']-1]
            with service.PDF_LOCK, pymupdf.open(service.doc_dir(doc['id'])/'source.pdf') as pdf:
                page = pdf[source['page']-1]
                native = {b['id']:pymupdf.Rect(b['bbox'][0]*page.rect.width,b['bbox'][1]*page.rect.height,
                    b['bbox'][2]*page.rect.width,b['bbox'][3]*page.rect.height)*page.derotation_matrix for b in page_data['blocks']}
            for bid in own:
                box = native.get(bid)
                if box is None:
                    continue
                neighbors = [(other,rect) for other,rect in native.items() if other not in own
                             and min(box.x1,rect.x1)-max(box.x0,rect.x0) >= min(box.width,rect.width)*.5]
                above = [(other,rect) for other,rect in neighbors if rect.y1 <= box.y0+.5]
                below = [(other,rect) for other,rect in neighbors if rect.y0 >= box.y1-.5]
                if above:
                    included.add(positions[max(above,key=lambda item:item[1].y1)[0]])
                if below:
                    included.add(positions[min(below,key=lambda item:item[1].y0)[0]])
        selected.update(included)
        chapters = list(dict.fromkeys(u['title'] for u in structure['units'] if set(own) & set(u['block_ids'])))
        contexts.append(dict(quote_id=quote['id'], block_ids=own, chapters=chapters,
                             mapping='page_context' if ambiguous else 'paragraph_context'))
        if ambiguous:
            contexts[-1]['note'] = '部分选中文字与原文段落的对应关系需核对，结合页面上下文和原文及对应译文页面理解。'
        nearby = '\n'.join(material[i]['text'] for i in sorted(included))
        mathematical = re.search(r'\b(?:fig(?:ure)?\.?|table|eq(?:uation)?\.?|theorem)\s*[\d(]|[=∑∫√≤≥α-ωΑ-Ω]|公式|图\s*\d|表\s*\d',
                                 question + '\n' + quote['text'] + '\n' + nearby, re.I)
        if mathematical or ambiguous:
            visual.append(dict(page=source['page']))
            if source.get('view') == 'translation':
                visual.append(dict(page=source['page'], view='translation', revision=source['revision']))
    return [r for i, r in enumerate(material) if i in selected], contexts, visual


def search(doc, query):
    material = rows(doc)
    terms = set(tokens(query))
    reference = re.search(r'(fig(?:ure)?|table|eq(?:uation)?|公式|式|图|表)[\s.(]*(\d+(?:\.\d+)*)', query, re.I)
    reference_pattern = None
    if reference:
        kind, number = reference.groups()
        prefix = r'(?:fig(?:ure)?|图)\.?\s*' if kind.lower().startswith('fig') or kind=='图' else r'(?:table|表)\s*'
        reference_pattern = (r'\(\s*' + re.escape(number) + r'\s*\)' if kind.lower().startswith('eq') or kind in ('公式','式')
                             else prefix + re.escape(number) + r'(?!\d)')
    def score(row):
        return len(terms & set(tokens(row['text']))) + (20 if reference_pattern and re.search(reference_pattern,row['text'],re.I) else 0)
    ranked = sorted(range(len(material)), key=lambda i: score(material[i]), reverse=True)
    hits = [i for i in ranked if score(material[i])][:4]
    selected = {j for i in hits for j in range(max(0, i-1), min(len(material), i+2))}
    retrieved_ids = {r['id'] for r in retrieve(material, query, [])}
    selected.update(i for i,r in enumerate(material) if r['id'] in retrieved_ids)
    return [r for i, r in enumerate(material) if i in selected]
