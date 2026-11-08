"""Offline, isolated serial/prewarmed comparison. Never calls a model or edits .data.

python tests/benchmark_pipeline.py --doc .data/<id> --out tmp/speed-current
Use --engine tmp/speed-baseline to compare the original code on the same inputs.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--doc', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--engine', type=Path, default=Path(__file__).resolve().parents[1])
args = parser.parse_args()
sys.path.insert(0, str(args.engine.resolve()))
import pdf_layout
import pdf_typography

args.out.mkdir(parents=True, exist_ok=False)
document = json.loads((args.doc/'document.json').read_text(encoding='utf-8'))
target = next((b['target'] for p in document['pages'] for b in p['blocks'] if b.get('translation') and b.get('target')), '简体中文')
pdf_typography._font_cache_root = lambda: args.out.resolve()/'font-cache'
measurements = {'pages': len(document['pages']), 'engine': pdf_layout.ENGINE_VERSION}
for mode in ('serial','pipeline') if hasattr(pdf_layout,'prewarm_layout') else ('serial',):
    directory = args.out/mode
    directory.mkdir()
    shutil.copy2(args.doc/'source.pdf',directory/'source.pdf')
    prewarm_ms=0
    if mode=='pipeline':
        start=time.perf_counter()
        warmed=pdf_layout.prewarm_layout(directory,document,target,[p['number'] for p in document['pages']])
        prewarm_ms=(time.perf_counter()-start)*1000
        measurements['warmed_pages']=warmed['pages']
    start=time.perf_counter()
    report=pdf_layout.build_layout(directory,document,target)
    cold_ms=(time.perf_counter()-start)*1000
    start=time.perf_counter()
    pdf_layout.build_layout(directory,document,target)
    warm_ms=(time.perf_counter()-start)*1000
    (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
    shutil.copy2(directory/'layout'/(report['revision']+'.pdf'),directory/'result.pdf')
    measurements[mode]={'prewarm_ms':prewarm_ms,'final_ms':cold_ms,'warm_revision_ms':warm_ms,
                        'replaced':report['replaced'],'preserved':report['preserved'],'untranslated':report['untranslated']}
(args.out/'timings.json').write_text(json.dumps(measurements,indent=2),encoding='utf-8')
print(json.dumps(measurements,indent=2))
