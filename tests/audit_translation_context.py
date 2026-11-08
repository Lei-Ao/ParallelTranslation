"""Read-only corpus audit of quality-context preparation; no model calls.

All imports and extraction run in a temporary data directory. Source artifacts
and saved translations are hashed before/after. Explicit input paths are optional.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as service
import pymupdf
from translation_quality import context_material


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(paths):
    original_data = service.DATA
    documents = list(original_data.glob('*/document.json'))
    watched = {p: file_hash(p) for p in paths + documents}
    results=[]
    with tempfile.TemporaryDirectory(prefix='quality-context-audit-') as temporary:
        service.DATA=Path(temporary)
        try:
            for path in paths:
                raw=path.read_bytes()
                _,doc=service.extract_document(raw,path.name)
                material=context_material(service,doc)
                with pymupdf.open(stream=raw,filetype='pdf') as pdf:
                    pages=[dict(width=p.rect.width,height=p.rect.height,rotation=p.rotation,
                                images=len(p.get_images())) for p in pdf]
                results.append(dict(name=path.name,pages=len(doc['pages']),blocks=doc['total_blocks'],
                                    formula_spans=sum(len(x) for x in material['formulas'].values()),
                                    sections=len(set(material['sections'].values())),gaps=material['gaps'],geometry=pages))
        finally:
            service.DATA=original_data
    unchanged=all(file_hash(p)==value for p,value in watched.items())
    report=dict(model_calls=0,inputs_unchanged=unchanged,documents=len(results),
                pages=sum(r['pages'] for r in results),results=results)
    output=service.ROOT/'tmp'/'quality-validation';output.mkdir(parents=True,exist_ok=True)
    (output/'context-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='results'}))
    return 0 if unchanged else 1


if __name__=='__main__':
    paths=[Path(p) for p in sys.argv[1:]] or list(service.DATA.glob('*/source.pdf'))
    raise SystemExit(audit(paths))
