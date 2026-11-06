"""Optional real-browser smoke test: pip install playwright; uses installed Edge.
Runs an isolated localhost server with a deterministic model, never real credentials.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import uvicorn
from playwright.sync_api import sync_playwright
import app as service
import paper_reading as reading
import re
from test_paper_analysis import sample


def tool(name, args):
    return dict(role='assistant', content='', tool_calls=[dict(id='smoke',type='function',function=dict(name=name,arguments=json.dumps(args)))])


async def fake_model(module, config, messages, tools):
    await asyncio.sleep(.5)
    if tools and '本轮为统一上下文阅读' in messages[0]['content']:
        return dict(role='assistant',content='材料已查看。',tool_calls=[])
    recent=[]
    for m in reversed(messages):
        if m['role']=='user' and isinstance(m['content'],str) and '"instruction"' in m['content']:
            instruction=json.loads(m['content'])['instruction'];break
        recent.append(m)
    names=[c['function']['name'] for m in recent for c in m.get('tool_calls',[])]
    def answer(text):
        system=messages[0]['content']
        if 'WORKFLOW_RESPONSE_JSON' not in system:return dict(role='assistant',content=text,tool_calls=[])
        fields=re.search('sections 按顺序且仅包含以下标题：([^。]+)',system).group(1).split('、')
        sections=[dict(section=f,text=text if i==0 else '见原文依据。',kind='claim',source_ids=['p1-b1']) for i,f in enumerate(fields)]
        return dict(role='assistant',content=json.dumps(dict(status='incomplete',sections=sections,missing=[])),tool_calls=[])
    if '小结' in instruction:
        resources=[json.loads(m['content']).get('path') for m in recent if m['role']=='tool']
        for resource in ('references/summary-template.md','references/question-checklist.md'):
            if resource not in resources:return tool('read_skill',{'path':resource})
        if 'save_summary' not in names:return tool('save_summary',{'markdown':'# 阅读小结\n方法解释 [[p1-b1]]'})
        return answer('小结已保存。')
    if '测试' in instruction:
        if 'get_structure' not in names:return tool('get_structure',{})
        if 'render_page' not in names:return tool('render_page',{'page':1})
    if '排版验收样例' in instruction:
        text=('这是一段连续文章排版验收示例。阅读时先明确研究对象、已有方法的不足，以及作者准备解决的具体问题。\n\n'
              '分析应结合原文中的方法机制、实验条件和证据边界。段落围绕一个主题展开，主题变化时另起一段，便于在窄侧栏中阅读。\n\n'
              '#### 阅读提示\n\n'
              '先对照问题定义，再查看输入、核心机制与输出之间的关系。评价条件和适用范围需要结合原文核实。\n\n'
              '- 输入与适用条件\n- 核心机制与输出\n\n'
              '比较方法 A 和方法 B，分别考察默认参数与优化参数在两类任务中的表现。\n\n'
              r'误差为 $\delta_p$，比例为 $\frac{a}{b}$，集合为 $\{x\mid x>0\}$。' + '\n\n'
              r'$\text{误差}=e_p$，组合数量为 $2\times 3$。' + '\n\n'
              r'历史格式兼容样例 $\{$A, B$\}\times\{$默认, 优化$\}$。' + '\n\n'
              '$$\n' + r'\begin{aligned}x &= 1 \\ y &= 2\end{aligned}' + '\n$$\n\n'
              '$$' + '+'.join(f'x_{{{i}}}^2' for i in range(35)) + '=1$$\n\n'
              '| 项目 | 条件 | 结果 | 来源 |\n|---|---|---|---|\n'
              '| 方法 | ' + 'long-condition-' * 15 + ' | 见原文 | 第1页 |\n\n'
              '1. 阅读问题定义。\n2. 对照方法与实验。\n3. 核对局限和边界。')
        return answer(text)
    if tools and 'read_material' not in names:return tool('read_material',{'page':1})
    return answer('机制 $x^2$。\n\n| 项目 | 值 |\n|---|---|\n| 方法 | Attention |\n\n<script>alert(1)</script>')


def selection_checks(page):
    def select(locator, end=5):
        locator.scroll_into_view_if_needed()
        locator.evaluate("""(e,end)=>{const r=document.createRange();r.setStart(e.firstChild,0);
            r.setEnd(e.firstChild,Math.min(end,e.firstChild.textContent.length));
            const s=getSelection();s.removeAllRanges();s.addRange(r);} """, end)
        page.locator('#quote-toolbar').wait_for(state='visible')

    def click_pdf_highlight(pane):
        page.locator(pane+' .saved-text-highlight').first.click(force=True)
        page.locator('[data-quote=remove-highlight]').wait_for(state='visible')

    def click_paragraph_highlight(color):
        name='reader-saved' if color=='yellow' else 'reader-saved-'+color
        box=page.evaluate("name=>{const r=[...CSS.highlights.get(name)][0].getClientRects()[0];return {x:r.x,y:r.y,width:r.width,height:r.height}}",name)
        page.mouse.click(box['x']+box['width']/2,box['y']+box['height']/2)
        try:
            page.locator('[data-quote=remove-highlight]').wait_for(state='visible',timeout=5000)
        except Exception:
            page.screenshot(path='tmp/highlight-click-failure.png',full_page=True)
            print('Highlight click:',box,ascii(page.evaluate("({x,y,width,height})=>({selected:getSelection()?.toString(),target:document.elementFromPoint(x+width/2,y+height/2)?.outerHTML,toolbar:document.querySelector('#quote-toolbar').outerHTML})",box)))
            raise

    page.wait_for_function('state.layout && !state.layoutBusy')
    page.locator('#translation-pane .pdf-char[data-run]').first.wait_for()
    assert not page.locator('#translation-edit').is_checked()
    assert page.locator('#quote-toolbar [data-quote]').all_text_contents()==['解释','引用','高亮','取消高亮']
    assert not page.locator('[data-quote=remove-highlight]').is_visible()
    translated=page.locator('#translation-pane .pdf-char[data-run]').filter(has_text='译').first
    translated.dblclick()
    assert not page.locator('#edit-dialog').is_visible()
    page.evaluate('getSelection().removeAllRanges()')
    # Exercise an actual mouse drag, including when the PDF is horizontally pannable.
    page.evaluate('setReaderZoom(2)')
    run=page.locator('#source-pane .pdf-char[data-run]').first
    run.scroll_into_view_if_needed()
    box=run.bounding_box()
    page.mouse.move(box['x']+2,box['y']+box['height']/2);page.mouse.down()
    page.mouse.move(box['x']+65,box['y']+box['height']/2,steps=10);page.mouse.up()
    page.locator('#quote-toolbar').wait_for(state='visible')
    assert page.evaluate('getSelection().toString().length')>0
    assert not page.evaluate('readerViewport.pan')
    select(run)
    page.locator('[data-highlight-color=blue]').click()
    page.locator('[data-quote=highlight]').click()
    page.wait_for_function("document.querySelector('#source-pane .saved-text-highlight')?.dataset.color==='blue'")
    page.reload();page.locator('.library-read').click()
    page.wait_for_function("document.querySelector('#source-pane .saved-text-highlight')?.dataset.color==='blue'")
    # Existing browser data without a color continues to render as yellow.
    page.evaluate("()=>{const key='parallel-highlights-'+state.doc.id;const rows=JSON.parse(localStorage.getItem(key));delete rows[0].color;localStorage.setItem(key,JSON.stringify(rows));}")
    page.reload();page.locator('.library-read').click()
    page.wait_for_function("document.querySelector('#source-pane .saved-text-highlight')?.dataset.color==='yellow'")
    click_pdf_highlight('#source-pane')
    for color in ('green','blue','pink','purple','yellow'):
        page.locator(f'[data-highlight-color={color}]').click()
        assert page.locator('#source-pane .saved-text-highlight').first.get_attribute('data-color')==color
    Path('tmp').mkdir(exist_ok=True)
    page.screenshot(path='tmp/highlight-colors.png',full_page=True)
    page.locator('[data-quote=remove-highlight]').click()
    page.wait_for_function("!document.querySelector('#source-pane .saved-text-highlight')")
    page.wait_for_function('state.layout && !state.layoutBusy')
    translated=page.locator('#translation-pane .pdf-char[data-run]').filter(has_text='译').first
    select(translated)
    page.locator('[data-highlight-color=green]').click()
    page.locator('[data-quote=highlight]').click()
    page.wait_for_function("document.querySelector('#translation-pane .saved-text-highlight')?.dataset.color==='green'")
    click_pdf_highlight('#translation-pane')
    page.locator('[data-highlight-color=pink]').click()
    assert page.locator('#translation-pane .saved-text-highlight').first.get_attribute('data-color')=='pink'
    page.locator('[data-quote=remove-highlight]').click()
    page.wait_for_function("!document.querySelector('#translation-pane .saved-text-highlight')")
    select(translated)
    page.locator('[data-quote=add]').click()
    page.locator('[data-remove-quote]').wait_for()
    assert '引用译文' in page.locator('#guide-selection').inner_text()
    page.locator('[data-preview-quote]').click()
    assert page.locator('#translation-pane .guide-highlight').count()>0
    page.locator('[data-remove-quote]').click()
    page.locator('[data-guide-action=close]').click()
    page.locator('#reader-menu').evaluate('(e)=>e.open=true')
    page.locator('#translation-edit').check()
    page.locator('#reader-menu').evaluate('(e)=>e.open=false')
    page.locator('#translation-pane .block-overlay').nth(1).dblclick()
    page.locator('#edit-dialog').wait_for(state='visible')
    page.locator('#edit-text').fill('译文😀片段可以引用。\n第二行也可高亮。')
    page.locator('#edit-form button[type=submit]').click()
    page.locator('#edit-dialog').wait_for(state='hidden')
    page.locator('#reader-menu').evaluate('(e)=>e.open=true')
    page.locator('#translation-edit').uncheck()
    page.locator('#translation-view').select_option('paragraph')
    page.locator('#reader-menu').evaluate('(e)=>e.open=false')
    text=page.locator('.translation-text').first
    select(text,8)
    page.locator('[data-highlight-color=yellow]').click()
    page.locator('[data-quote=highlight]').click()
    page.wait_for_function("CSS.highlights.get('reader-saved')?.size>0")
    click_paragraph_highlight('yellow')
    page.locator('[data-highlight-color=blue]').click()
    assert page.evaluate("CSS.highlights.get('reader-saved-blue').size")==1
    select(text,8)
    page.locator('[data-quote=add]').click()
    page.locator('[data-remove-quote]').wait_for()
    assert '译文😀片段' in page.locator('#guide-selection').inner_text()
    page.locator('[data-preview-quote]').click()
    assert page.evaluate("CSS.highlights.get('reader-preview').size")==1
    page.reload();page.locator('.library-read').click()
    assert not page.locator('#translation-edit').is_checked()
    page.locator('#reader-menu').evaluate('(e)=>e.open=true')
    page.locator('#translation-view').select_option('paragraph')
    page.locator('#reader-menu').evaluate('(e)=>e.open=false')
    page.wait_for_function("CSS.highlights.get('reader-saved-blue')?.size>0")
    page.locator('.translation-text').first.scroll_into_view_if_needed()
    click_paragraph_highlight('blue')
    page.locator('[data-quote=remove-highlight]').click()
    assert page.evaluate("CSS.highlights.get('reader-saved-blue').size")==0
    # Return to the initial UI for the existing conversation and notes checks.
    page.reload();page.locator('.library-read').click()


def highlight_geometry_checks(page,url):
    for columns,rotation,width in ((1,0,612),(2,90,720),(2,180,840),(1,270,500)):
        response=page.request.post(url+'/api/documents',headers={'X-Local-Token':page.evaluate('state.token')},
            multipart={'file':{'name':'geometry.pdf','mimeType':'application/pdf','buffer':sample(columns=columns,rotation=rotation,width=width)}})
        assert response.status==200,response.text()
        page.evaluate('id=>openDocument(id,true)',response.json()['id'])
        page.locator('[data-reader-view=source]').click()
        run=page.locator('#source-pane .pdf-char[data-run]').first
        run.wait_for();run.scroll_into_view_if_needed()
        run.evaluate("e=>{const r=document.createRange();r.selectNodeContents(e);const s=getSelection();s.removeAllRanges();s.addRange(r);}")
        page.locator('[data-highlight-color=green]').click()
        page.locator('[data-quote=highlight]').click()
        highlight=page.locator('#source-pane .saved-text-highlight').first
        highlight.wait_for(state='attached')
        assert highlight.get_attribute('data-color')=='green'
        highlight.click(force=True)
        page.locator('[data-quote=remove-highlight]').click()
        page.wait_for_function("!document.querySelector('#source-pane .saved-text-highlight')")


def main():
    with tempfile.TemporaryDirectory() as directory:
        service.EXPORT_DIR=Path(directory)/'exports'
        service.DATA=Path(directory);reading.invoke=fake_model
        config=uvicorn.Config(service.app,host='127.0.0.1',port=18769,log_level='error')
        server=uvicorn.Server(config);thread=threading.Thread(target=server.run,daemon=True);thread.start()
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        url='http://127.0.0.1:18769'
        try:
            with httpx.Client(base_url=url) as client:
                token=client.get('/api/session').json()['token']
                doc=client.post('/api/documents',headers={'X-Local-Token':token},files={'file':('smoke.pdf',sample(columns=2),'application/pdf')}).json()
                doc['pages'][0]['blocks'][0]['text'] += ' Long original material for full-document analysis.' * 700
                doc['pages'][0]['blocks'][1].update(translation='译文段落可以解释、引用和高亮。',target='简体中文')
                service.save_doc(service.doc_dir(doc['id']),doc)
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport={'width':1500,'height':1000})
                page.add_init_script("localStorage.setItem('parallel-agent',JSON.stringify({mode:'openai',model:'mock',base_url:'https://example.test/v1'}))")
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url);page.locator('.library-read').click();selection_checks(page);page.locator('#assistant-toggle').click()
                page.locator('#guide-question').fill('分析整篇论文，生成文章速读并给出建议阅读路线。')
                page.locator('#guide-form button').click()
                page.locator('.guide-working progress').wait_for(timeout=15000)
                assert page.locator('.guide-working progress').get_attribute('aria-label')=='文章分析进度'
                page.wait_for_function("document.querySelector('.guide-working')?.textContent.includes('页面图已准备')")
                assert '页面图已准备' in page.locator('.guide-working').inner_text()
                Path('tmp').mkdir(exist_ok=True)
                page.screenshot(path='tmp/fulltext-progress.png',full_page=True)
                page.reload();page.locator('.library-read').click();page.locator('#assistant-toggle').click()
                page.locator('.guide-turn').wait_for(timeout=15000)
                assert '全文共 2 页' in page.locator('.guide-turn').inner_text()
                assert page.locator('.guide-followups').count()==0
                assert page.locator('[data-guide-action=detail],[data-guide-action=next],[data-guide-action=back],[data-guide-action=skip]').count()==0
                assert page.locator('.guide-markdown .katex').count()>0
                assert page.locator('.guide-markdown table').count()==1
                assert page.locator('.guide-markdown script').count()==0
                assert page.locator('#translation-pane').is_visible()
                page.locator('[data-reader-view=source]').click()
                page.wait_for_function("document.querySelector('#source-pane .pdf-page').getBoundingClientRect().width>600")
                page.locator('.guide-markdown a').first.click()
                assert page.locator('#source-pane .selected').count()>0
                page.locator('#guide-question').fill('这一步为什么？')
                page.locator('#guide-form button').click()
                page.wait_for_function("document.querySelectorAll('.guide-turn').length===2")
                page.locator('.guide-modes [data-guide-action=continue]').click()
                page.wait_for_function("document.querySelectorAll('.guide-turn').length===3")
                page.wait_for_function("!document.querySelector('.guide-working')")
                page.locator('.guide-more').evaluate('(e)=>e.open=true')
                page.locator('[data-guide-action=visual]').click()
                page.locator('.guide-turn > .guide-image img').wait_for(state='attached',timeout=15000)
                page.locator('.guide-turn > .guide-image img').scroll_into_view_if_needed()
                page.locator('.guide-turn > .guide-image img').wait_for(timeout=15000)
                page.wait_for_function("!document.querySelector('.guide-working')")
                page.locator('.guide-more').evaluate('(e)=>e.open=true')
                page.locator('[data-guide-action=crop]').click()
                box=page.locator('#source-pane').bounding_box()
                page.mouse.move(box['x']+100,box['y']+60);page.mouse.down()
                page.mouse.move(box['x']+350,box['y']+180,steps=5);page.mouse.up()
                assert '框选区域' in page.locator('#guide-selection').inner_text()
                page.locator('#guide-question').fill('解释选中区域')
                page.locator('#guide-form button').click()
                page.wait_for_function("document.querySelectorAll('.guide-turn').length===5")
                assert page.locator('.guide-image').count()>=2
                page.wait_for_function("!document.querySelector('.guide-working')")
                page.locator('#source-pane .pdf-char[data-run]').first.scroll_into_view_if_needed()
                page.locator('#source-pane .pdf-char[data-run]').first.evaluate("e=>{const r=document.createRange();r.setStart(e.firstChild,0);r.setEnd(e.firstChild,Math.min(5,e.textContent.length));const s=getSelection();s.removeAllRanges();s.addRange(r);}")
                page.locator('#quote-toolbar [data-quote=add]').click()
                page.wait_for_function("document.querySelectorAll('[data-remove-quote]').length===1")
                page.locator('.guide-more').evaluate('(e)=>e.open=true')
                page.locator('[data-guide-action=summary]').click()
                page.wait_for_function("document.querySelectorAll('.guide-turn').length===6")
                page.wait_for_function("!document.querySelector('.guide-working')")
                page.locator('[data-guide-action=notes]').click()
                page.locator('.guide-note-entry').wait_for(timeout=15000)
                page.locator('.guide-note-entry').first.click()
                page.locator('.guide-note-actions a').wait_for()
                with page.expect_download() as download:
                    page.locator('.guide-note-actions a').click()
                assert download.value.suggested_filename.endswith('.md')
                page.locator('[data-guide-action=conversation]').click()
                page.locator('[data-remove-quote]').click()
                page.locator('[data-reader-view=both]').click();assert page.locator('#translation-pane').is_visible()
                page.locator('[data-reader-view=source]').click()
                page.wait_for_function("document.querySelector('#source-pane .pdf-page').getBoundingClientRect().width>600")
                page.locator('.guide-conversation').evaluate('(e)=>e.scrollTop=0')
                # Real browser Range selection -> canonical server quote -> multi-quote message.
                page.locator('.guide-more').evaluate('(e)=>e.open=false')
                page.locator('#source-pane').evaluate('(e)=>e.scrollTop=0')
                page.locator('.pdf-char[data-run]').first.wait_for()
                for index in (0,1):
                    page.locator('#source-pane .pdf-char[data-run]').nth(index).scroll_into_view_if_needed()
                    page.locator('#source-pane .pdf-char[data-run]').nth(index).evaluate("e=>{const r=document.createRange();r.setStart(e.firstChild,0);r.setEnd(e.firstChild,Math.min(5,e.textContent.length));const s=getSelection();s.removeAllRanges();s.addRange(r);}")
                    page.locator('#quote-toolbar [data-quote=add]').click()
                    page.wait_for_function(f"document.querySelectorAll('[data-remove-quote]').length==={index+1}")
                page.locator('[data-remove-quote="0"]').click()
                assert page.locator('[data-remove-quote]').count()==1
                page.locator('#source-pane .pdf-char[data-run]').first.scroll_into_view_if_needed()
                page.locator('#source-pane .pdf-char[data-run]').first.evaluate("e=>{const r=document.createRange();r.selectNodeContents(e);const s=getSelection();s.removeAllRanges();s.addRange(r);}")
                page.locator('#quote-toolbar [data-quote=add]').click()
                page.wait_for_function("document.querySelectorAll('[data-remove-quote]').length===2")
                page.locator('#guide-question').fill('比较这两段的作用')
                page.locator('#guide-form button').click()
                page.wait_for_function("document.querySelectorAll('.guide-turn').length===7")
                assert page.locator('.message-citation').count()==2
                assert page.locator('[data-remove-quote]').count()==0
                page.locator('.message-citation').first.click()
                assert page.locator('.guide-highlight').count()>0
                page.wait_for_function("!document.querySelector('.guide-working')")
                # Favorite full answers and verify the same article renderer in notes.
                for index in (0,3,6):
                    saved=page.locator('[data-favorite]').nth(index)
                    saved.click()
                    page.wait_for_function(f"document.querySelectorAll('[data-favorite]')[{index}].textContent.includes('已收藏')")
                page.locator('[data-favorite]').first.click()
                page.locator('.guide-note-heading').wait_for()
                assert page.locator('.guide-markdown section').count()==4
                assert page.locator('.guide-markdown .katex').count()>0
                assert page.locator('.guide-markdown table').count()==1
                assert page.locator('.guide-sources li').count()>=3
                assert not page.locator('.guide-composer').is_visible()
                page.once('dialog',lambda dialog:dialog.accept())
                page.locator('[data-delete-note]').click()
                page.wait_for_function("document.querySelectorAll('.guide-note-entry').length===3")
                page.locator('[data-guide-action=conversation]').click()
                assert '已收藏' not in page.locator('[data-favorite]').first.inner_text()
                page.locator('[data-favorite]').first.click()
                page.wait_for_function("document.querySelector('[data-favorite]').textContent.includes('已收藏')")
                page.locator('[data-favorite]').nth(3).click()
                page.locator('.guide-note-heading').wait_for()
                visual_note_id=page.locator('[data-delete-note]').get_attribute('data-delete-note')
                page.locator('[data-guide-action=conversation]').click()
                page.once('dialog',lambda dialog:dialog.accept())
                page.locator('[data-delete-turn]').last.click()
                page.wait_for_function("document.querySelectorAll('[data-delete-turn]').length===6")
                page.locator('#guide-question').fill('分析整篇论文并生成文章速读，使用排版验收样例。')
                page.locator('#guide-form button').click()
                page.wait_for_function("document.querySelectorAll('[data-delete-turn]').length===7")
                page.wait_for_function("!document.querySelector('.guide-working')")
                assert page.locator('.guide-turn').last.locator('.katex-display').count()>0
                assert page.locator('.guide-turn').last.locator('table').count()==1
                assert page.locator('.guide-turn').last.locator('.katex-error').count()==0
                formulas=[text.strip() for text in page.locator('.guide-turn').last.locator('annotation[encoding="application/x-tex"]').all_text_contents()]
                assert r'\{x\mid x>0\}' in formulas
                assert r'\begin{aligned}x &= 1 \\ y &= 2\end{aligned}' in formulas
                assert r'\text{误差}=e_p' in formulas
                assert r'\frac{a}{b}' in formulas
                assert r'\}\times\{' in formulas
                for width in (1920,1440,1280,1024,700):
                    page.set_viewport_size({'width':width,'height':1000})
                    page.wait_for_timeout(250)
                    panel=page.locator('#guided-reading').bounding_box()
                    composer=page.locator('.guide-composer').bounding_box()
                    assert abs(composer['y']+composer['height']-(panel['y']+panel['height']))<2
                    assert page.locator('#guide-form button').is_visible()
                    assert page.locator('.guide-markdown').last.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
                    if width>=1040:
                        reader=page.locator('.reading-workspace>.panes').bounding_box()
                        assert reader['x']+reader['width']<=panel['x']+1
                    else:
                        assert not page.locator('.reading-workspace>.panes').is_visible()
                page.set_viewport_size({'width':1920,'height':1080})
                page.locator('[data-reader-view=both]').click()
                page.locator('#directory-toggle').click()
                page.locator('.guide-conversation').evaluate('(e)=>e.scrollTop=0')
                Path('tmp').mkdir(exist_ok=True)
                page.screenshot(path='tmp/reading-desktop.png',full_page=True)
                page.locator('.guide-turn').last.evaluate("e=>{const p=e.closest('.guide-conversation');p.scrollTop+=e.getBoundingClientRect().top-p.getBoundingClientRect().top;}")
                page.screenshot(path='tmp/reading-article-layout.png',full_page=True)
                page.reload();page.locator('.library-read').click();page.locator('#assistant-toggle').click()
                page.wait_for_function("document.querySelectorAll('.guide-turn').length>=5")
                page.wait_for_function("document.querySelector('[data-favorite]').textContent.includes('已收藏')")
                page.set_viewport_size({'width':700,'height':900})
                assert page.locator('#guided-reading').bounding_box()['width']>500
                page.screenshot(path='tmp/reading-mobile.png',full_page=True)
                # Clearing the conversation keeps independent notes and their images.
                page.locator('.guide-more').evaluate('(e)=>e.open=true')
                page.once('dialog',lambda dialog:dialog.accept())
                page.locator('[data-guide-action=clear]').click()
                page.wait_for_function("document.querySelectorAll('[data-delete-turn]').length===0")
                page.locator('[data-guide-action=notes]').click()
                page.wait_for_function("document.querySelectorAll('.guide-note-entry').length===4")
                page.locator(f'[data-open-note="{visual_note_id}"]').click()
                page.locator('.guide-image img').wait_for()
                image_url=page.locator('.guide-image img').get_attribute('src')
                assert page.request.get(url+image_url).status==200
                with page.expect_download() as note_download:
                    page.locator('.guide-note-actions a').click()
                exported=Path(note_download.value.path()).read_text(encoding='utf-8')
                assert '图像区域' in exported
                page.screenshot(path='tmp/reading-note-mobile.png',full_page=True)
                page.set_viewport_size({'width':1500,'height':1000})
                page.screenshot(path='tmp/reading-note-desktop.png',full_page=True)
                page.locator('.guide-markdown a').first.click()
                assert page.locator('#source-pane .selected').count()>0
                page.once('dialog',lambda dialog:dialog.accept())
                page.locator('[data-delete-note]').click()
                page.wait_for_function("document.querySelectorAll('.guide-note-entry').length===3")
                assert page.request.get(url+image_url).status==404
                page.locator('[data-guide-action=close]').click();assert not page.locator('#guided-reading').is_visible()
                page.locator('#assistant-toggle').click();page.locator('.guide-more summary').click();page.locator('[data-guide-action=history]').click()
                assert page.locator('#reading-assistant').is_visible()
                page.locator('#assistant-close').click()
                page.locator('#reader-menu').evaluate('(e)=>e.open=true')
                page.locator('#translation-view').select_option('paragraph')
                page.locator('#reader-menu').evaluate('(e)=>e.open=false')
                text=page.locator('.translation-text').first
                text.scroll_into_view_if_needed()
                text.evaluate("e=>{const r=document.createRange();r.setStart(e.firstChild,0);r.setEnd(e.firstChild,8);const s=getSelection();s.removeAllRanges();s.addRange(r);}")
                page.locator('[data-quote=explain]').click()
                try:
                    page.locator('.message-citation').wait_for(timeout=15000)
                except Exception:
                    print('Explain state:',ascii(page.locator('#toast').inner_text()),ascii(page.locator('#guided-reading').inner_text()))
                    raise
                page.wait_for_function("!document.querySelector('.guide-working')")
                assert '引用译文' in page.locator('.message-citation').inner_text()
                assert '译文😀片段' in page.locator('.message-citation').inner_text()
                page.locator('.message-citation').click()
                assert page.evaluate("CSS.highlights.get('reader-preview').size")==1
                page.screenshot(path='tmp/reading-selection.png',full_page=True)
                highlight_geometry_checks(page,url)
                assert not errors,errors
                print('Browser smoke passed: conversation, math, table, sanitization, citations, progress, vision, notes, restore, narrow layout, legacy. Console errors:',len(errors))
                browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=10)


if __name__=='__main__':main()
