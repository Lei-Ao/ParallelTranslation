"""Isolated browser fixture. No credentials or existing document data are used."""
import asyncio
import json
from pathlib import Path
import re
import socket
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as service
import pdf_model
from vision_fixture import client as vision_client
pdf_model._CLIENT = vision_client()
import uvicorn
from test_app import sample_pdf


async def model(config, prompt, text='', target='', system_prompt=''):
    await asyncio.sleep(.15)
    value=json.loads(prompt[prompt.index('{'):])
    blocks=value.get('current_blocks')
    if blocks is not None:
        return json.dumps({'blocks':[dict(id=b['id'],translation='智能体使用上下文理解论文。') for b in blocks]})
    return '智能体使用上下文理解论文。'



with tempfile.TemporaryDirectory(prefix='parallel-quality-browser-') as temp:
    service.DATA=Path(temp)/'data';service.DATA.mkdir()
    service.EXPORT_DIR=Path(temp)/'exports'
    service.invoke_agent=model
    doc_id,_=service.extract_document(sample_pdf(), 'Quality browser fixture.pdf')
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    print('FIXTURE '+json.dumps(dict(url=f'http://127.0.0.1:{port}',doc_id=doc_id)),flush=True)
    uvicorn.Server(uvicorn.Config(service.app,log_level='error')).run(sockets=[sock])
