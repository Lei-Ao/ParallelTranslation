"""Isolated, deduplicated source-structure / layout comparison. No model API.

python tests/audit_document_structure.py --baseline tmp/babeldoc-upgrade-baseline \
    --references D:/AIwork/copy --out tmp/structure-audit
Reference PDFs use identity translations to test reconstruction, not accuracy.
"""
import argparse
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pymupdf
import app as service
import pdf_document
import pdf_layout
import pdf_translation
import pdf_model
import pdf_visual_check
from vision_fixture import client as vision_client
from performance import TRACE
from test_document_structure import fixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--mock-vision-api', action='store_true', help='Use deterministic HTTP image responses; does not measure model quality')
    args = parser.parse_args()
    if args.mock_vision_api:
        pdf_model._CLIENT = vision_client()
        pdf_model._CONFIG.set({'mode':'openai','base_url':'https://example.com/v1','model':'fixture'})
    args.out.mkdir(parents=True, exist_ok=False)
    service.DATA = args.out.resolve()/'inputs'
    service.DATA.mkdir()
    samples, seen = [], set()
    for source in sorted((ROOT/'.data').glob('*/source.pdf')):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        document = json.loads(source.with_name('document.json').read_text(encoding='utf-8'))
        samples.append(('saved-paper', source, document, digest))
    for source in sorted(args.references.rglob('*.pdf')):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        identifier, document = service.extract_document(source.read_bytes(), source.name)
        for page in document['pages']:
            for block in page['blocks']:
                block.update(translation=block['text'], target='English')
        samples.append(('reference-identity', service.DATA/identifier/'source.pdf', document, digest))
    for rotation in (0, 90, 180, 270):
        for columns in (1, 2):
            raw = fixture(rotation, columns, crop=True)
            identifier, document = service.extract_document(raw, f'synthetic-{rotation}-{columns}.pdf')
            directory = service.DATA/identifier
            pages = pdf_document.ensure_pages(directory, document)
            records = {}
            for page in document['pages']:
                for block in page['blocks']:
                    packet = pdf_translation.build_input(block, pdf_document.paragraph_for(pages, block))
                    translated = packet.text.replace('Method.', '方法。').replace('The state is ', '状态为 ').replace(' and remains stable.', ' 并保持稳定。').replace('The next paragraph describes our results.', '下一段描述实验结果。')
                    text, record = pdf_translation.restore_output(packet, translated, '简体中文')
                    block.update(translation=text, target='简体中文')
                    records[block['id']] = record
            pdf_translation.save_records(directory, records)
            samples.append(('synthetic-translated', directory/'source.pdf', document, hashlib.sha256(raw).hexdigest()))
    results = []
    baseline_code = """import json,sys;from pathlib import Path
sys.path.insert(0,sys.argv[1]);import pdf_layout
p=Path(sys.argv[2]);d=json.loads((p/'document.json').read_text(encoding='utf-8'))
r=pdf_layout.build_layout(p,d,sys.argv[3]);(p/'report.json').write_text(json.dumps(r,ensure_ascii=False),encoding='utf-8')
import shutil;shutil.copy2(p/'layout'/(r['revision']+'.pdf'),p/'result.pdf')
"""
    for index, (kind, source, document, digest) in enumerate(samples):
        directory = args.out.resolve()/f'case-{index:02d}'
        directory.mkdir()
        target = next((b['target'] for p in document['pages'] for b in p['blocks'] if b.get('target')), 'English')
        for mode in ('before','after'):
            folder = directory/mode; folder.mkdir()
            shutil.copy2(source, folder/'source.pdf')
            (folder/'document.json').write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8')
        sidecar = source.parent/'structure'/'translations.json'
        if sidecar.is_file():
            (directory/'after'/'structure').mkdir()
            shutil.copy2(sidecar,directory/'after'/'structure'/'translations.json')
        subprocess.run([sys.executable,'-c',baseline_code,str(args.baseline.resolve()),str(directory/'before'),target],check=True,stdout=subprocess.DEVNULL)
        trace={}; token=TRACE.set(trace)
        started=time.perf_counter()
        original=copy.deepcopy(document)
        report=pdf_layout.build_layout(directory/'after',document,target)
        report=pdf_visual_check.validate(directory/'after',document,report)
        elapsed=time.perf_counter()-started
        inferences=trace.get('vision_requests',0)
        cached_report=pdf_layout.build_layout(directory/'after',document,target)
        pdf_visual_check.validate(directory/'after',document,cached_report)
        assert trace.get('vision_requests',0)==inferences,'warm build repeated vision requests'
        TRACE.reset(token)
        assert document==original,'layout modified document'
        after=directory/'after';shutil.copy2(after/'layout'/(report['revision']+'.pdf'),after/'result.pdf')
        (after/'report.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
        before_report=json.loads((directory/'before'/'report.json').read_text(encoding='utf-8'))
        info={'kind':kind,'source':str(source),'sha256':digest,'pages':len(document['pages']),
              'before':{k:before_report[k] for k in ('replaced','preserved','untranslated')},
              'after':{k:report[k] for k in ('replaced','preserved','untranslated')},
              'issues':dict(Counter(item['message'] for item in report['issues'])),
              'vision_fixture':args.mock_vision_api,'visual_check':report['visual_check']['status'],'structure':report['structure'],'seconds':elapsed,'timings':trace,'pages_changed':[],'missing_translation_characters':[]}
        with pymupdf.open(directory/'before'/'result.pdf') as before,pymupdf.open(after/'result.pdf') as new:
            for page_index in range(len(new)):
                old_pix=before[page_index].get_pixmap();new_pix=new[page_index].get_pixmap()
                if old_pix.samples!=new_pix.samples:info['pages_changed'].append(page_index+1)
                for mode,pdf in (('before',before),('after',new)):
                    pdf[page_index].get_pixmap(matrix=pymupdf.Matrix(1.2,1.2)).save(directory/f'{mode}-{page_index+1}.png')
                visible=Counter(c for c in new[page_index].get_text() if '\u3400'<=c<='\u9fff')
                expected=Counter()
                preserved={i['block_id'] for i in report['issues'] if i.get('block_id') and i['kind']=='preserved'}
                for block in document['pages'][page_index]['blocks']:
                    if block['id'] in report['placements'] and block['id'] not in preserved:
                        expected.update(c for c in block.get('translation','') if '\u3400'<=c<='\u9fff')
                if expected-visible:info['missing_translation_characters'].append({'page':page_index+1,'characters':dict(expected-visible)})
        results.append(info)
        print(json.dumps({'case':index,'kind':kind,'pages':info['pages'],'before':info['before'],'after':info['after'],'missing':info['missing_translation_characters']},ensure_ascii=True),flush=True)
        (args.out/'report.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Validated {len(results)} unique sources / {sum(r["pages"] for r in results)} pages')
    return any(r['missing_translation_characters'] for r in results)


if __name__=='__main__':
    raise SystemExit(main())
