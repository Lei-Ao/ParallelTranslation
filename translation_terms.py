"""Manual glossary matching and CSV storage."""
import csv
import hashlib
import io
import json
import re
import unicodedata


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalized(text):
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'(?<=\w)-\s*\n\s*(?=\w)', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def term_key(source):
    value = normalized(source)
    return value if value.isupper() else value.casefold()


def contains(text, term):
    term = normalized(term)
    if not term:
        return False
    # Acronyms are case-sensitive; Latin words must not match inside other words.
    left = r'(?<![\w])' if re.match(r'[A-Za-z0-9]', term) else ''
    right = r'(?![\w])' if re.search(r'[A-Za-z0-9]$', term) else ''
    return bool(re.search(left + re.escape(term) + right, normalized(text),
                          0 if term.isupper() else re.I))


def manual_entries(text, target):
    entries = []
    for line in text.splitlines():
        pair = re.split(r'\s*(?:=>|=|→)\s*', line.strip(), maxsplit=1)
        if len(pair) == 2 and all(pair) and len(pair[0]) <= 200 and len(pair[1]) <= 500:
            entries.append(dict(source=pair[0], target=pair[1], tgt_lng=target,
                                origin='manual', enabled=True, source_ids=[], alternatives=[]))
    return entries


def language_key(value):
    key = (value or '').strip().lower().replace('-', '_')
    aliases = {'简体中文':'zh_cn', 'simplified chinese':'zh_cn', 'zh_hans':'zh_cn',
               '繁體中文':'zh_tw', '繁体中文':'zh_tw', 'traditional chinese':'zh_tw', 'zh_hant':'zh_tw',
               'english':'en', '日本語':'ja', '한국어':'ko', 'français':'fr', 'deutsch':'de'}
    return aliases.get(key, key)


def active_entries(entries, target, text=None):
    candidates = [e for e in entries if e.get('enabled', True)
                  and (not e.get('tgt_lng') or language_key(e['tgt_lng']) == language_key(target))]
    return sorted([e for e in candidates if text is None or contains(text, e['source'])],
                  key=lambda e: (-len(e['source']), e['source']))


def merge_entries(defaults, document, automatic=()):
    # Last explicit document entry wins, including disabled entries (tombstones).
    chosen = {}
    for entry in [*automatic, *defaults, *document]:
        key = (term_key(entry['source']), language_key(entry.get('tgt_lng')))
        chosen[key] = dict(entry)
    return list(chosen.values())


def csv_text(entries):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=['source', 'target', 'tgt_lng'])
    writer.writeheader()
    writer.writerows({k: e.get(k, '') for k in writer.fieldnames} for e in entries if e.get('enabled', True))
    return out.getvalue()


def read_csv(text, target):
    if len(text) > 2_000_000:
        raise ValueError('术语文件超过 2 MB')
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not {'source', 'target'} <= set(reader.fieldnames or []):
        raise ValueError('CSV 必须包含 source,target 列')
    entries = []
    for row in reader:
        source, translated = (row.get('source') or '').strip(), (row.get('target') or '').strip()
        if not 1 <= len(source) <= 200 or not 1 <= len(translated) <= 500:
            raise ValueError('术语原词或译法为空或过长')
        entries.append(dict(source=source, target=translated, tgt_lng=row.get('tgt_lng') or target,
                            origin='manual', enabled=True, source_ids=[], alternatives=[]))
        if len(entries) > 5000:
            raise ValueError('术语文件最多 5000 条')
    return entries
