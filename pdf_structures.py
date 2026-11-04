"""Keep independently anchored labels and author rows out of paragraph flow."""
import html
import re
import unicodedata
from collections import Counter

import pymupdf


def normalized(text):
    return ''.join(unicodedata.normalize('NFKC', text).split())


def line_text(line):
    return ''.join(s['text'] for s in line['spans']).strip()


def grid_members(native):
    """Find repeated author cards from their rows, without page/name assumptions."""
    institution = re.compile(r'universit|institut|department|laborator|大学|学院|研究所', re.I)
    cards = [b for b in native if 2 <= len(b['lines']) <= 6
             and any(institution.search(line_text(l)) for l in b['lines'])
             and all(len(line_text(l)) < 100 for l in b['lines'])]
    selected = []
    for b in cards:
        box = pymupdf.Rect(b['bbox'])
        centers = [(l['bbox'][0] + l['bbox'][2]) / 2 for l in b['lines']]
        if max(centers) - min(centers) > 3:
            continue
        if any(other is not b and abs(other['bbox'][1] - box.y0) < 3
               and (other['bbox'][2] < box.x0 or other['bbox'][0] > box.x1)
               for other in cards):
            selected.append(b)
    result = {id(b) for b in selected}
    for b in native:
        if len(b['lines']) != 1 or '@' not in line_text(b['lines'][0]):
            continue
        box = pymupdf.Rect(b['bbox'])
        if any(abs((box.x0 + box.x1 - c['bbox'][0] - c['bbox'][2]) / 2) < 3
               and 0 <= box.y0 - c['bbox'][3] < 24 for c in selected):
            result.add(id(b))
    return result


def anchored_lines(native, rect, graphics, grid, prose, caption):
    candidates = [b for b in native if (pymupdf.Rect(b['bbox']) & rect).get_area() > .5]
    if not candidates:
        return None
    best = max(candidates, key=lambda b: (pymupdf.Rect(b['bbox']) & rect).get_area())
    lines = [l for l in best['lines'] if rect.contains(pymupdf.Rect(l['bbox']).tl
             + (pymupdf.Rect(l['bbox']).br - pymupdf.Rect(l['bbox']).tl) * .5)]
    if not lines or any(abs(l.get('dir', (1, 0))[0] - 1) > .01
                        or abs(l.get('dir', (1, 0))[1]) > .01 for l in lines):
        return None
    if id(best) in grid:
        return lines
    if any(re.search(r'math|symbol|cmmi|cmsy|cmex|msam|msbm|stmary|rsfs', s['font'], re.I)
           for line in lines for s in line['spans']):
        return None  # Mathematical regions keep the formula-specific path.
    text = '\n'.join(line_text(l) for l in lines)
    if prose(text) or caption(text) or any(len(line_text(l)) > 85 for l in lines):
        return None
    size = max(s['size'] for l in lines for s in l['spans'])
    # Separate anchors on one baseline are not paragraph wrapping. Requiring
    # nearby artwork prevents ordinary numbered headings from entering here.
    separated = any(abs(a['bbox'][1] - b['bbox'][1]) < size * .3
                    and b['bbox'][0] - a['bbox'][2] > max(4, size * .8)
                    for a in lines for b in lines)
    near = any((pymupdf.Rect(g) & (rect + (-size * 3, -size * 3, size * 3, size * 3))).get_area() > 0
               for g in graphics)
    sizes = Counter()
    for b in native:
        for l in b['lines']:
            for s in l['spans']:
                sizes[round(s['size'], 1)] += len(s['text'].strip())
    body_size = sizes.most_common(1)[0][0] if sizes else size
    return lines if near and (separated or size < body_size * .9) else None


def anchored_overlay(overlays, page, block, lines, native, graphics, typography):
    """Translate individual anchors; untouched text retains its original glyphs.

    Positional mappings require one translation line per extracted source line.
    Ambiguity or any unsafe glyph footprint rejects the entire group atomically.
    """
    source = [line_text(l) for l in lines]
    translated = [x.strip() for x in block['translation'].splitlines() if x.strip()]
    failure = '图表标签或作者行无法可靠逐项对应或在原位容纳，完整保留该文字组原文'
    if normalized(''.join(source)) != normalized(block['text']):
        return None, None, failure
    if normalized(block['text']) == normalized(block['translation']):
        return None, None, ''
    if len(source) != len(translated):
        return None, None, failure
    changed = [i for i, (a, b) in enumerate(zip(source, translated)) if normalized(a) != normalized(b)]
    owned = {id(s) for i in changed for s in lines[i]['spans']}
    foreign = []
    for b in native:
        for line in b['lines']:
            spans = [s for s in line['spans'] if s['text'].strip()]
            remaining = [s for s in spans if id(s) not in owned]
            if remaining and len(remaining) == len(spans):
                # One PDF text operator can paint several spans on a line.
                # Measure that line together instead of mistaking its padded
                # font frames for visible ink when each span has no own operator.
                foreign.append(pymupdf.Rect(line['bbox']))
            else:
                foreign.extend(pymupdf.Rect(s['bbox']) for s in remaining)
    operations = [pymupdf.Rect(coords) for kind, coords, *_ in page.get_bboxlog()
                  if kind == 'fill-text']
    fixed_ink = []
    for box in foreign:
        measured = [ink for ink in operations if (box + (-2, -2, 2, 2)).contains(ink)
                    and (box & ink).get_area() > ink.get_area() * .5]
        fixed_ink.extend(ink + (.5, .5, -.5, -.5) for ink in measured)
        if not measured:
            fixed_ink.append(box)
    composite = overlays.new_page(width=page.rect.width, height=page.rect.height)
    composite_index = len(overlays) - 1
    region = pymupdf.Rect(lines[0]['bbox'])
    for l in lines[1:]:
        region |= pymupdf.Rect(l['bbox'])
    occupied = []
    for i in changed:
        line = lines[i]
        box = pymupdf.Rect(line['bbox'])
        spans = [s for s in line['spans'] if s['text'].strip()]
        size = max(s['size'] for s in spans)
        baseline = max(s['origin'][1] for s in spans)
        bold = sum(len(s['text']) for s in spans if s['flags'] & 16) > sum(len(s['text']) for s in spans) * .75
        family = 'monospace' if '@' in source[i] else typography['family']
        css = typography['css'] + (f'body{{margin:0;padding:0;font-family:{family};font-size:{size}pt;'
              f'line-height:1.0;font-weight:{600 if bold else 400};color:#{spans[0]["color"]:06x};text-align:center}}')
        with pymupdf.open() as scratch:
            p = scratch.new_page(width=page.rect.width, height=page.rect.height)
            # Modest expansion is allowed only after glyph-level collision
            # checks, preserving the original label's center and baseline.
            fit = pymupdf.Rect(max(0, box.x0 - size), 10,
                               min(page.rect.width, box.x1 + size), 10 + size * 4)
            spare, scale = p.insert_htmlbox(fit, html.escape(translated[i]), css=css,
                                           archive=typography['archive'], scale_low=1)
            output = [l for b in p.get_text('dict')['blocks'] if b['type'] == 0 for l in b['lines']]
            if spare < 0 or len(output) != 1 or normalized(p.get_text()) != normalized(translated[i]):
                overlays.delete_page(composite_index)
                return None, None, failure + '（译文无法保持单行）'
            actual = pymupdf.Rect(output[0]['bbox'])
            shift = baseline - output[0]['spans'][0]['origin'][1]
            dest = actual + (0, shift, 0, shift)
            # Use painted bounds, not overlapping font ascent/descent frames.
            ink = [pymupdf.Rect(coords) for kind, coords, *_ in p.get_bboxlog() if kind == 'fill-text']
            painted = ink[0] if ink else actual
            for item in ink[1:]:
                painted |= item
            painted += (.5, .5, -.5, -.5)
            painted += (0, shift, 0, shift)
            if not page.rect.contains(dest) or any((painted & other).get_area() > .1 for other in fixed_ink + occupied):
                overlays.delete_page(composite_index)
                return None, None, failure + '（相邻文字空间不足）'
            # Background panels enclose their labels, but individual strokes or
            # images crossing a new glyph footprint must never be overwritten.
            if any((painted & pymupdf.Rect(g)).get_area() > .1
                   and not pymupdf.Rect(g).contains(box)
                   and (box & pymupdf.Rect(g)).get_area() < .1 for g in graphics):
                overlays.delete_page(composite_index)
                return None, None, failure
            clip = actual + (-.2, -.2, .2, .2)
            dest = clip + (0, shift, 0, shift)
            composite.show_pdf_page(dest, scratch, 0, clip=clip)
            region |= dest
            occupied.append(painted)
    style = {'spans': [s for i in changed for s in lines[i]['spans']], 'lines': lines,
             'size': max(s['size'] for l in lines for s in l['spans']), 'bold': False}
    result = {'overlay': composite_index, 'rect': list(region), 'clip': list(region), 'scale': 1,
              'anchored': True}
    return result, style, ''
