"""Isolated browser fixture exercising multimodal API layout + deterministic translation."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import uvicorn

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app as service
import pdf_model
from vision_fixture import client as vision_client
pdf_model._CLIENT = vision_client()
from test_document_structure import fixture


async def model(config, prompt, text='', target='简体中文', **kwargs):
    await asyncio.sleep(.1)
    def translated(source):
        return source.replace('Method.', '方法。').replace('The state is ', '状态为 ').replace(' and remains stable.', ' 并保持稳定。').replace('The next paragraph describes our results.', '下一段描述实验结果。')
    if '参考资料与本次待翻译段落：\n' in prompt:
        content=json.loads(prompt.split('参考资料与本次待翻译段落：\n')[1])
        return json.dumps({'blocks':[{'id':b['id'],'translation':translated(b['source'])} for b in content['current_blocks']]})
    return translated(text)


with tempfile.TemporaryDirectory() as temporary:
    service.DATA=Path(temporary)/'data'; service.DATA.mkdir()
    service.EXPORT_DIR=Path(temporary)/'exports'
    service.invoke_agent=model
    identifier, document=service.extract_document(fixture(columns=2),'结构浏览器验收.pdf')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    print('FIXTURE '+json.dumps({'url':f'http://127.0.0.1:{port}','id':identifier}),flush=True)
    uvicorn.run(service.app,host='127.0.0.1',port=port,log_level='error')
