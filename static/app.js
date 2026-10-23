'use strict';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const DEFAULT_AGENT = {mode:'openai', base_url:'https://api.apigoto.com/v1', api_key:'', model:'', template:'{\n  "input": "{{prompt}}"\n}', response_path:'output', timeout:120};
function stored(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } }
function persist(key, data) { try { localStorage.setItem(key, JSON.stringify(data)); return true; } catch { toast('浏览器存储不可用，设置仅在当前页面有效'); return false; } }
const state = {token:'', doc:null, library:[], page:0, selected:null, running:false, stop:false, uploading:false, loading:false, activeBlocks:new Set(), concurrency:[1,2,3,4,6,8,12].includes(stored('parallel-concurrency',3))?stored('parallel-concurrency',3):3, errors:{}, view:'pdf', layout:null, layoutKey:'', layoutBusy:false, layoutError:'',
  agent:{...DEFAULT_AGENT,...stored('parallel-agent',{})}, context:stored('parallel-context',{glossary:'',instructions:''}), target:stored('parallel-target','简体中文')};
state.batchSize=[1,3,5].includes(stored('parallel-batch-size',3))?stored('parallel-batch-size',3):3;
state.batchSupported=false;
state.pendingOpen=false;
state.preparing=false;
state.openMessage='';
let toastTimer, editBlock;
const pendingPaneScrolls=new WeakMap();
const readingInteraction={side:'source',lane:.25,hovered:null,anchor:null,selection:new Set()};
const readerViewport={zoom:1,fit:true,documentId:null,pan:null,suppressClick:null,imageTimer:null};
const MIN_READER_ZOOM=.1, MAX_READER_ZOOM=6, PDF_CSS_SCALE=96/72;
let layoutRequest = 0;
const layoutUpdates=TranslationQueue.createCoalescedTask(generateLayout);
function layoutKey(){return JSON.stringify([state.doc?.id,state.target,blocks().map(b=>[b.id,b.translation,b.target])]);}
function invalidateLayout(preserve=false){if(!preserve){layoutRequest++;state.layout=null;state.layoutKey='';state.layoutBusy=false;}state.layoutError='';updateLayoutControls();}
function updateLayoutControls(){
  const valid=Boolean(state.layout),fresh=valid && state.layoutKey===layoutKey();
  $('#refresh-layout').disabled=state.layoutBusy || state.running || !state.doc;
  $('#download-pdf').classList.toggle('hidden',!valid);
  if(valid)$('#download-pdf').href=`/api/documents/${state.doc.id}/layout/${state.layout.revision}/download`;
  else $('#download-pdf').removeAttribute('href');
  $('#download-pdf').textContent=fresh?'下载译文 PDF':'下载已排版 PDF';
  $('#layout-status').textContent=state.layoutBusy?'正在生成译文 PDF…':state.layoutError || (valid?`已排版 ${state.layout.replaced} 段 · ${state.layout.preserved} 段保留原文 · ${state.layout.untranslated} 段未译${fresh?'':' · 新译文将在本页完成后更新'}`:'每页译完后自动排版');
  $('#layout-issues').innerHTML=valid?state.layout.issues.map(issue=>`<li>第 ${issue.page} 页${issue.block_id?` · ${escapeHtml(issue.block_id)}`:''}：${escapeHtml(issue.message)}</li>`).join(''):'';
  if(valid && state.layout.saved_pdf)$('#layout-issues').innerHTML+=`<li>已自动保存：${escapeHtml(state.layout.saved_pdf)}。可在本地直接打开；部分区域可能保留原文，请查看排版提示。</li>`;
  if(valid && state.layout.save_error)$('#layout-issues').innerHTML+=`<li>${escapeHtml(state.layout.save_error)}</li>`;
}
function refreshLayout(){return layoutUpdates.request();}
async function generateLayout(){
  if(!state.doc)return;
  const request=layoutRequest, key=layoutKey(), id=state.doc.id, target=state.target;
  state.layoutBusy=true;state.layoutError='';updateLayoutControls();
  if(state.view==='pdf' && !state.pendingOpen)renderTranslations();
  try{
    const report=await api(`/api/documents/${id}/layout`,{method:'POST',body:JSON.stringify({target})});
    if(request!==layoutRequest)return;
    state.layout=report;state.layoutKey=key;
    if(report.save_error)toast(report.save_error);
  }catch(error){if(request!==layoutRequest)return;state.layoutError=error.message;}
  finally{
    if(request===layoutRequest){state.layoutBusy=false;updateLayoutControls();if(state.view==='pdf' && state.doc?.id===id && !state.pendingOpen)renderTranslations();}
  }
}
function showEdit(block){editBlock=block;$('#edit-source').textContent=block.text;$('#edit-text').value=block.translation;$('#edit-dialog').showModal();}
const escapeHtml = (str) => String(str).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function toast(message) { $('#toast').textContent=message; $('#toast').classList.remove('hidden'); clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('#toast').classList.add('hidden'),5500); }
async function api(path, options={}) {
  const headers = {'X-Local-Token':state.token,...options.headers};
  if(options.body && !(options.body instanceof FormData)) headers['Content-Type']='application/json';
  const response = await fetch(path,{...options,headers});
  if(!response.ok) { let message; try { const data=await response.json(); message=typeof data.detail==='string'?data.detail:'请求参数无效，请检查设置'; } catch { message=`请求失败（HTTP ${response.status}）`; } throw Object.assign(new Error(message),{status:response.status,retryable:response.headers.get('X-Translation-Retryable')==='true',fallback:response.headers.get('X-Translation-Fallback')}); }
  return response.json();
}
function currentPage(){return state.doc?.pages[state.page];}
function blocks(){return state.doc?.pages.flatMap(page=>page.blocks) ?? [];}
function isTranslated(block){return Boolean(block.translation) && block.target===state.target;}
function status(message,error=false){$('#job-status').textContent=message;$('#job-status').classList.toggle('error',error);}
function updateControls(){
  const count=blocks().filter(isTranslated).length,total=blocks().length;
  updateOpenProgress(count,total);
  $('#progress-bar').style.width=`${total?count/total*100:0}%`;
  $('#translation-count').textContent=`${count} / ${total} 段已译 · ${state.target}`;
  $('#translate-all').textContent=state.running?(state.stop?'正在暂停…':'Ⅱ 暂停翻译'):(count && count<total?'✦ 继续翻译':'✦ 翻译全文');
  $('#translate-all').disabled=state.uploading || state.loading || !total || (state.running && state.stop);
  $('#translate-page').disabled=state.running || !currentPage()?.blocks.length;
  $('#target-language').disabled=state.running;
  $('#translation-concurrency').disabled=state.running;
  $('#translation-batch-size').disabled=state.running || !state.batchSupported || state.agent.mode!=='openai';
  $('#batch-help').textContent=!state.batchSupported?'重启本机服务后可启用合并翻译。':state.agent.mode!=='openai'?'自定义接口保持单段请求。':'合并同页段落，减少请求和重复上下文；格式不兼容时可改回 1 段。';
  $('#settings-button').disabled=state.running || state.preparing;
  $('#reader-settings').disabled=state.running;
  $('#back-library').disabled=state.running;
  document.body.classList.toggle('translating',state.running);
  $('#reading-activity').classList.toggle('hidden',!state.running);
  $('#reading-activity').textContent=state.stop?'正在暂停…':`翻译中 · ${state.activeBlocks.size} 段`;
  $('#context-button').disabled=state.running;
  $('#upload-side').disabled=state.running || state.uploading || state.loading || state.preparing;
  $$('.library-item').forEach(button=>button.disabled=state.running || state.uploading || state.loading || state.preparing);
  $('#previous-page').disabled=state.page<=0;
  $('#next-page').disabled=!state.doc || state.page>=state.doc.pages.length-1;
  $$('.card-actions button').forEach(button=>button.disabled=state.running);
  updateLayoutControls();
}
function renderLibrary(){
  $('#library-count').textContent=state.library.length;
  $('#library').innerHTML=state.library.length?state.library.map(doc=>`<button class="library-item ${state.doc?.id===doc.id?'active':''}" data-doc="${doc.id}" title="${escapeHtml(doc.name)}" aria-label="打开 ${escapeHtml(doc.name)}"><span class="doc-symbol">▤</span><div><strong>${escapeHtml(doc.name)}</strong><small>${doc.pages} 页 · ${doc.translated} 段已有译文</small></div></button>`).join(''):'<p class="library-empty">导入第一份文档，<br>开始跨越语言的阅读。</p>';
  updateControls();
}
async function refreshLibrary(){state.library=await api('/api/documents');renderLibrary();}
function renderDocument(ready=false){
  document.body.classList.add('reading');
  $('#reader-menu').open=false;
  $('#welcome').classList.add('hidden');$('#reader').classList.remove('hidden');
  $('#document-name').textContent=state.doc.name;$('#breadcrumb-title').textContent=state.doc.name;
  const empty=state.doc.pages.filter(page=>page.needs_ocr).length;
  $('#document-meta').textContent=`${state.doc.pages.length} 页 · ${state.doc.total_blocks} 个文本块 · 自动保存${empty?` · ${empty} 页无可提取文字`:''}`;
  $('#page-select').innerHTML=state.doc.pages.map((page,i)=>`<option value="${i}">${page.number}</option>`).join('');
  $('#page-total').textContent=`/ ${state.doc.pages.length}`;
  $('#export').href=`/api/documents/${state.doc.id}/export`;
  if(!ready)invalidateLayout();renderPage();renderLibrary();status('准备就绪 · 单击同步选中，双击译文编辑 · Shift 连选');if(!ready)refreshLayout();
}
function renderCard(block,index){
  const error=state.errors[block.id],working=state.activeBlocks.has(block.id);
  const text=block.translation?`<div class="translation-text">${escapeHtml(block.translation)}</div>`:`<div class="source-preview">${escapeHtml(block.text)}</div>`;
  let label=block.translation?(block.edited?'已修订':'已翻译'):'待翻译';
  if(block.translation && block.target!==state.target) label=`已有${block.target}译文`;
  if(error) label='翻译失败'; if(working) label='<span class="busy-indicator"></span>翻译中';
  const needsStatus=working || error || !block.translation || block.target!==state.target;
  return `<article class="translation-card ${isBlockSelected(block.id)?'selected':''}" data-block="${block.id}" id="card-${block.id}" tabindex="0" aria-label="第 ${index+1} 段"><div class="card-meta ${needsStatus?'':'hidden'}"><span class="state ${error?'error':''}">${label}</span></div>${text}${error?`<div class="block-error">${escapeHtml(error)}</div>`:''}<div class="card-actions"><button data-action="translate" data-id="${block.id}">${block.translation?'↻ 重新翻译':'✦ 翻译此段'}</button><button data-action="edit" data-id="${block.id}">编辑译文</button>${block.translation?`<button data-action="copy" data-id="${block.id}">复制</button>`:''}</div></article>`;
}
function isBlockSelected(id){return state.selected===id || (state.selected!==null && readingInteraction.selection.has(id));}
function pageElements(pane){return pane && pane.querySelectorAll?[...pane.querySelectorAll('.reader-page')]:[];}
function setPaneScroll(pane,top,left=pane.scrollLeft){
  pane.scrollTop=Math.max(0,top);pane.scrollLeft=left;
  pendingPaneScrolls.set(pane,{top:pane.scrollTop,left:pane.scrollLeft});
}
function elementTopInPane(pane,element){return element.getBoundingClientRect().top-pane.getBoundingClientRect().top+pane.scrollTop;}
function panePosition(pane){
  const pages=pageElements(pane);if(!pages.length)return null;
  let index=0;
  for(let i=0;i<pages.length;i++){if(elementTopInPane(pane,pages[i])<=pane.scrollTop+45)index=i;else break;}
  const top=elementTopInPane(pane,pages[index]);
  const height=pages[index].getBoundingClientRect().height || pages[index].offsetHeight || 1;
  return {index,top,height,fraction:Math.max(0,Math.min(1,(pane.scrollTop-top)/height))};
}
function restorePanePosition(pane,position,fallbackTop){
  if(!position){setPaneScroll(pane,fallbackTop || 0);return;}
  const pages=pageElements(pane), element=pages[Math.min(position.index,pages.length-1)];
  if(!element){setPaneScroll(pane,fallbackTop || 0);return;}
  const height=element.getBoundingClientRect().height || element.offsetHeight || 0;
  setPaneScroll(pane,elementTopInPane(pane,element)+position.fraction*height);
}
function pdfPanes(){return [$('#source-pane'),$('#translation-pane')];}
function paneContentWidth(pane){
  const style=getComputedStyle(pane);
  return pane.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight);
}
function fitReaderZoom(){
  const widest=Math.max(...state.doc.pages.map(page=>page.width));
  const available=pdfPanes().map(paneContentWidth);
  return Math.max(MIN_READER_ZOOM,Math.min(MAX_READER_ZOOM,Math.min(...available)/(widest*PDF_CSS_SCALE)));
}
function updateZoomControls(){
  const enabled=Boolean(state.doc) && state.view==='pdf';
  $('#zoom-reset').textContent=`${Math.round(readerViewport.zoom*100)}%`;
  $('#zoom-out').disabled=!enabled || readerViewport.zoom<=MIN_READER_ZOOM;
  $('#zoom-in').disabled=!enabled || readerViewport.zoom>=MAX_READER_ZOOM;
  $('#zoom-reset').disabled=!enabled;$('#zoom-fit').disabled=!enabled;
  $('#zoom-fit').setAttribute('aria-pressed',String(readerViewport.fit));
  $('#zoom-hint').textContent=enabled?'同步缩放 · Ctrl＋滚轮':'段落视图 · 切回 PDF 可缩放';
}
function applyViewportGeometry(){
  if(!state.doc || !$('#source-pane').clientWidth)return;
  if(readerViewport.documentId!==state.doc.id){
    readerViewport.documentId=state.doc.id;readerViewport.fit=true;
    for(const pane of pdfPanes())pane.scrollLeft=0;
  }
  if(readerViewport.fit)readerViewport.zoom=fitReaderZoom();
  for(const pane of pdfPanes()){
    const available=paneContentWidth(pane);
    for(const page of pageElements(pane)){
      if(!page.classList.contains('pdf-page'))continue;
      const source=state.doc.pages[Number(page.dataset.pageIndex)];
      const width=source.width*PDF_CSS_SCALE*readerViewport.zoom;
      page.style.width=`${width}px`;
      page.style.marginLeft=`${Math.max(0,(available-width)/2)}px`;page.style.marginRight='0';
    }
    // The source has an extra stack wrapper. Size it explicitly so its right
    // padding contributes the same scroll range as the translated PDF pane.
    const stack=pane.querySelector('#pdf-page');
    if(stack)stack.style.width=`${Math.max(available,...state.doc.pages.map(page=>page.width*PDF_CSS_SCALE*readerViewport.zoom))}px`;
    pane.classList.toggle('can-pan',pane.scrollWidth>pane.clientWidth+1);
  }
  updateZoomControls();scheduleReaderImages();
}
function captureViewportAnchor(pane,clientX,clientY){
  const pages=pageElements(pane);if(!pages.length)return null;
  const bounds=pane.getBoundingClientRect();
  const x=clientX ?? bounds.left+pane.clientWidth/2;
  const y=clientY ?? bounds.top+pane.clientHeight/2;
  let index=0;
  for(let i=0;i<pages.length;i++){if(pages[i].getBoundingClientRect().top<=y)index=i;else break;}
  const page=pages[index].getBoundingClientRect();
  return {index,x:(x-page.left)/(page.width || 1),y:(y-page.top)/(page.height || 1),
    offsetX:x-bounds.left,offsetY:y-bounds.top};
}
function restoreViewportAnchor(pane,anchor){
  if(!anchor)return;
  const page=pageElements(pane)[anchor.index];if(!page)return;
  const box=page.getBoundingClientRect(),bounds=pane.getBoundingClientRect();
  setPaneScroll(pane,pane.scrollTop+box.top-bounds.top+anchor.y*box.height-anchor.offsetY,
    pane.scrollLeft+box.left-bounds.left+anchor.x*box.width-anchor.offsetX);
}
function setReaderZoom(value,{side='translation',clientX,clientY,fit=false}={}){
  if(!state.doc || state.view!=='pdf')return;
  const zoom=Number(value);if(!fit && !Number.isFinite(zoom))return;
  const panes=pdfPanes(),origin=$(`#${side}-pane`),bounds=origin.getBoundingClientRect();
  const x=clientX ?? bounds.left+origin.clientWidth/2,y=clientY ?? bounds.top+origin.clientHeight/2;
  // Each side keeps its own corresponding reading position. Scroll events
  // caused by resizing must not replace the cursor anchor with a top-of-pane anchor.
  const anchors=panes.map(pane=>{const box=pane.getBoundingClientRect();return captureViewportAnchor(pane,
    box.left+(x-bounds.left)/origin.clientWidth*pane.clientWidth,
    box.top+(y-bounds.top)/origin.clientHeight*pane.clientHeight);});
  readerViewport.fit=fit;readerViewport.zoom=Math.max(MIN_READER_ZOOM,Math.min(MAX_READER_ZOOM,zoom));
  applyViewportGeometry();
  panes.forEach((pane,i)=>restoreViewportAnchor(pane,anchors[i]));
}
function refreshReaderViewport(){
  if(!state.doc || $('#reader').classList.contains('hidden'))return;
  const panes=pdfPanes(),anchors=panes.map(pane=>captureViewportAnchor(pane));
  applyViewportGeometry();panes.forEach((pane,i)=>restoreViewportAnchor(pane,anchors[i]));
}
function readerWheel(event,side){
  if(!event.ctrlKey || !state.doc || state.view!=='pdf')return;
  event.preventDefault();
  const unit=event.deltaMode===1?16:event.deltaMode===2?200:1;
  const delta=Math.max(-100,Math.min(100,event.deltaY*unit));
  if(delta)setReaderZoom(readerViewport.zoom*Math.exp(-delta*.002),{side,clientX:event.clientX,clientY:event.clientY});
}
function startReaderPan(event,side){
  readerViewport.suppressClick=null;
  const pane=$(`#${side}-pane`);
  if(event.button!==0 || event.pointerType==='touch' || event.shiftKey || event.ctrlKey || event.metaKey
      || !event.target.closest('.pdf-page') || pane.scrollWidth<=pane.clientWidth+1)return;
  readerViewport.pan={side,pane,pointerId:event.pointerId,x:event.clientX,y:event.clientY,
    left:pane.scrollLeft,top:pane.scrollTop,moved:false};
}
function moveReaderPan(event){
  const pan=readerViewport.pan;if(!pan || event.pointerId!==pan.pointerId)return false;
  const dx=event.clientX-pan.x,dy=event.clientY-pan.y;
  if(!pan.moved && Math.hypot(dx,dy)<6)return false;
  if(!pan.moved){pan.moved=true;pan.pane.setPointerCapture(event.pointerId);pan.pane.classList.add('reader-panning');hoverBlock(null);}
  event.preventDefault();
  setPaneScroll(pan.pane,pan.top-dy,pan.left-dx);
  if($('#sync-scroll').checked){
    if(Math.abs(dy)>1){pendingPaneScrolls.delete(pan.pane);synchronize(pan.side);}
    else{
      const other=$(`#${pan.side==='source'?'translation':'source'}-pane`);
      const range=pan.pane.scrollWidth-pan.pane.clientWidth;
      setPaneScroll(other,other.scrollTop,range>0?pan.pane.scrollLeft/range*Math.max(0,other.scrollWidth-other.clientWidth):0);
    }
  }
  pendingPaneScrolls.set(pan.pane,{top:pan.pane.scrollTop,left:pan.pane.scrollLeft});
  readingInteraction.side=pan.side;return true;
}
function endReaderPan(event){
  const pan=readerViewport.pan;if(!pan || event.pointerId!==pan.pointerId)return;
  readerViewport.pan=null;pan.pane.classList.remove('reader-panning');
  if(pan.moved)readerViewport.suppressClick=pan.side;
  if(pan.pane.hasPointerCapture(event.pointerId))pan.pane.releasePointerCapture(event.pointerId);
}
function suppressPanClick(event,side){
  if(readerViewport.suppressClick===side && event.detail!==0){event.preventDefault();event.stopImmediatePropagation();}
}
function scheduleReaderImages(){
  clearTimeout(readerViewport.imageTimer);
  readerViewport.imageTimer=setTimeout(()=>{
    const quality=Math.min(6,Math.max(1.7,Math.ceil(readerViewport.zoom*PDF_CSS_SCALE*(window.devicePixelRatio || 1)*2)/2));
    for(const pane of pdfPanes()){
      const bounds=pane.getBoundingClientRect();
      for(const img of pane.querySelectorAll('.pdf-page img')){
        const box=img.getBoundingClientRect();
        if(box.bottom<bounds.top || box.top>bounds.bottom)continue;
        if(quality<=Number(img.dataset.requestedScale || img.dataset.renderScale || 1.7))continue;
        const base=img.dataset.baseSrc || img.getAttribute('src').split('?')[0];
        img.dataset.baseSrc=base;img.dataset.requestedScale=quality;
        // Keep the current preview until the sharper image has loaded. A slow
        // or failed request must never blank a page or replace a newer revision.
        const sharper=new Image();sharper.decoding='async';
        sharper.onload=()=>{if(img.isConnected && quality>Number(img.dataset.renderScale || 1.7)){
          img.dataset.renderScale=quality;img.src=sharper.src;
        }};
        sharper.src=`${base}?scale=${quality}`;
      }
    }
  },180);
}
function alignReaderAfterRender(){
  const source=$('#source-pane');
  if($('#sync-scroll').checked && pageElements(source).length){pendingPaneScrolls.delete(source);synchronize('source');}
}
function setCurrentPage(index){
  if(!state.doc)return;
  const next=Math.max(0,Math.min(state.doc.pages.length-1,Number(index)||0));
  $('#page-select').value=next;
  if(next!==state.page){state.page=next;updateControls();}
}
function scrollPaneToPage(pane,index,fraction=0){
  const pages=pageElements(pane), element=pages[index];if(!element)return;
  const height=element.getBoundingClientRect().height || element.offsetHeight || 0;
  setPaneScroll(pane,elementTopInPane(pane,element)+fraction*height-14);
}
function scrollToPage(index){
  if(!state.doc)return;
  state.page=Math.max(0,Math.min(state.doc.pages.length-1,Number(index)||0));
  $('#page-select').value=state.page;
  scrollPaneToPage($('#source-pane'),state.page);
  scrollPaneToPage($('#translation-pane'),state.page);
  updateControls();
}
function renderTranslations(){
  const pane=$('#translation-pane');
  if(!state.doc){pane.innerHTML='';return;}
  const top=pane.scrollTop, left=pane.scrollLeft, position=panePosition(pane);
  pane.classList.toggle('pdf-mode',state.view==='pdf');
  pane.classList.toggle('pages-stack',state.view==='pdf');
  if(state.view==='pdf'){
    const valid=Boolean(state.layout);
    pane.innerHTML=state.doc.pages.map((page,pageIndex)=>{
      const image=valid?`/api/documents/${state.doc.id}/layout/${state.layout.revision}/pages/${page.number}.png`:`/api/documents/${state.doc.id}/pages/${page.number}.png`;
      return `<div class="pdf-page reader-page" data-page-index="${pageIndex}" style="aspect-ratio:${page.width}/${page.height}"><img ${pageIndex===state.page?'':'loading="lazy" '}decoding="async" src="${image}" alt="第 ${page.number} 页${valid?'译文':'原文占位'}" draggable="false">${page.blocks.map((block,i)=>{const [x0,y0,x1,y1]=valid?(state.layout.placements[block.id] || block.bbox):block.bbox;return `<button class="block-overlay ${isBlockSelected(block.id)?'selected':''}" data-block="${block.id}" aria-label="定位第 ${page.number} 页第 ${i+1} 段译文，双击编辑" title="单击同步选中，双击编辑译文" style="left:${x0*100}%;top:${y0*100}%;width:${(x1-x0)*100}%;height:${(y1-y0)*100}%"></button>`;}).join('')}</div>`;
    }).join('');
    if(!valid){const notice=document.createElement('div');notice.className='layout-notice';notice.textContent=state.layoutError || (state.layoutBusy?'正在排版，暂显示原文…':state.running?'翻译中，每页译完后自动更新版式':'暂显示原文，请点击菜单中的重新排版');pane.prepend(notice);}
    $$('#translation-pane .pdf-page img').forEach(img=>img.addEventListener('error',()=>toast('译文页面加载失败，请点击重新排版或切换段落视图')));
    applyViewportGeometry();restorePanePosition(pane,position,top);setPaneScroll(pane,pane.scrollTop,left);updateControls();alignReaderAfterRender();return;
  }
  pane.innerHTML=state.doc.pages.map((page,pageIndex)=>`<section class="reader-page paragraph-page" data-page-index="${pageIndex}" aria-label="第 ${page.number} 页译文"><div class="page-divider">第 ${page.number} 页</div>${page.blocks.length?page.blocks.map(renderCard).join(''):'<div class="ocr-notice"><strong>这一页没有可提取的文字</strong><br>可能是扫描件、图片页或空白页。第一版暂不包含 OCR，请先用 OCR 工具生成可搜索 PDF 后再导入。其他含文字的页面仍可正常翻译。</div>'}</section>`).join('');
  applyViewportGeometry();restorePanePosition(pane,position,top);updateControls();alignReaderAfterRender();
}
function renderPage(){
  if(!state.doc)return;
  $('#page-select').value=state.page;
  const stack=$('#pdf-page');
  stack.classList.add('pages-stack');
  stack.style.aspectRatio='';
  stack.innerHTML=state.doc.pages.map((page,pageIndex)=>`<div class="pdf-page reader-page" data-page-index="${pageIndex}" style="aspect-ratio:${page.width}/${page.height}"><img ${pageIndex===state.page?'':'loading="lazy" '}decoding="async" src="/api/documents/${state.doc.id}/pages/${page.number}.png" alt="${escapeHtml(state.doc.name)} 第 ${page.number} 页" draggable="false">${page.blocks.map((block,i)=>{const [x0,y0,x1,y1]=block.bbox;return `<button class="block-overlay ${isBlockSelected(block.id)?'selected':''}" data-block="${block.id}" aria-label="定位第 ${page.number} 页第 ${i+1} 段译文" title="第 ${page.number} 页第 ${i+1} 段" style="left:${x0*100}%;top:${y0*100}%;width:${(x1-x0)*100}%;height:${(y1-y0)*100}%"></button>`;}).join('')}</div>`).join('');
  $$('#pdf-page img').forEach(img=>img.addEventListener('error',()=>toast('PDF 页面渲染失败，请重新打开文档')));
  renderTranslations();
  scrollToPage(state.page);
}
function showLibrary(){
  document.body.classList.remove('reading','reading-focus');$('#reader').classList.add('hidden');$('#welcome').classList.remove('hidden');$('#breadcrumb-title').textContent='对照翻译';$('#reader-menu').open=false;
}
function updateOpenProgress(count,total){
  const visible=state.pendingOpen || state.uploading || state.loading;
  $('#open-progress').classList.toggle('hidden',!visible);
  $('#drop-zone').classList.toggle('hidden',visible);
  $('#home-target').disabled=state.running || state.preparing || state.uploading || state.loading;
  $('#home-context').disabled=state.running || state.preparing;
  if(!visible)return;
  const percent=total && !state.uploading && !state.loading?Math.floor(count/total*100):0;
  $('#open-name').textContent=state.uploading?'正在导入 PDF…':state.loading?'正在读取 PDF…':state.doc?.name || '';
  $('#open-percent').textContent=`${percent}%`;
  $('#open-meter').value=percent;
  $('#open-status').textContent=state.uploading || state.loading?'正在解析文档，请稍候…':state.openMessage || `${state.stop?'正在暂停':'正在翻译'} · ${count} / ${total} 段 · ${state.target}`;
  $('#open-action').textContent=state.running?(state.stop?'正在暂停…':'暂停翻译'):count===total && total?'重新排版并打开':'继续翻译';
  $('#open-action').disabled=state.uploading || state.loading || (state.preparing && !state.running) || (state.running && state.stop) || !total;
}
async function prepareDocument(){
  if(!state.pendingOpen || state.preparing || state.running || state.loading || state.uploading)return;
  const pending=blocks().filter(block=>!isTranslated(block));
  if(!blocks().length){state.openMessage='没有可提取的文字，请先对扫描件进行 OCR，再重新导入。';updateControls();return;}
  if(pending.length && !validAgent()){state.openMessage='请先配置 Agent，保存后点击继续翻译。';updateControls();return;}
  state.preparing=true;state.openMessage='';updateControls();
  try{
    if(pending.length)await runTranslation(pending);
    if(blocks().some(block=>!isTranslated(block))){const error=Object.values(state.errors)[0];state.openMessage=error?`翻译失败：${error}。已保留完成的译文，可点击继续翻译重试。`:'已暂停，点击继续翻译可从当前进度恢复。';return;}
    state.openMessage='翻译 100% · 正在排版，完成后自动打开…';updateControls();
    await refreshLayout();
    if(state.layoutError || !state.layout || state.layoutKey!==layoutKey())throw new Error(state.layoutError || '排版未完成，请重试');
    state.pendingOpen=false;renderDocument(true);
  }catch(error){state.openMessage=`处理失败：${error.message}。可重试，已完成的译文会保留。`;toast(error.message);}
  finally{state.preparing=false;updateControls();}
}
async function openDocument(id){
  if(state.running || state.uploading || state.loading || state.preparing)return;
  state.pendingOpen=false;state.loading=true;showLibrary();updateControls();
  try{state.doc=await api(`/api/documents/${id}`);state.page=0;state.selected=null;state.errors={};state.pendingOpen=true;invalidateLayout();}
  catch(error){toast(error.message);}
  finally{state.loading=false;updateControls();}
  await prepareDocument();
}
async function uploadFile(file){
  if(!file || state.running || state.uploading || state.loading || state.preparing)return;
  if(file.size>50*1024*1024){toast('PDF 不能超过 50 MB');return;}
  state.pendingOpen=false;state.uploading=true;showLibrary();updateControls();toast('正在解析 PDF，请稍候…');
  try{const body=new FormData();body.append('file',file);state.doc=await api('/api/documents',{method:'POST',body});state.page=0;state.errors={};state.selected=null;state.pendingOpen=true;invalidateLayout();await refreshLibrary();}
  catch(error){toast(error.message);}
  finally{state.uploading=false;$('#file-input').value='';updateControls();}
  await prepareDocument();
}
function choosePage(index){scrollToPage(index);}
function applyBlockSelection(){
  $$('[data-block]').forEach(el=>{
    const selected=isBlockSelected(el.dataset.block);
    el.classList.toggle('selected',selected);
    if(selected)el.setAttribute('aria-current','true');else el.removeAttribute('aria-current');
    if(el.matches('.block-overlay'))el.setAttribute('aria-pressed',String(selected));
  });
}
function blockElement(side,id){
  return (side==='source'?$('#source-pane'):$('#translation-pane')).querySelector(`[data-block="${id}"]`);
}
function rememberBlockLane(id){
  const block=blocks().find(block=>block.id===id);
  if(block){const center=(block.bbox[0]+block.bbox[2])/2;if(block.bbox[2]-block.bbox[0]<.65)readingInteraction.lane=center;}
}
function selectBlock(id,from,extend=false){
  const all=blocks(),index=all.findIndex(block=>block.id===id);if(index<0)return;
  if(!state.selected)readingInteraction.anchor=null;
  if(!extend || !readingInteraction.anchor)readingInteraction.anchor=id;
  const anchor=all.findIndex(block=>block.id===readingInteraction.anchor);
  readingInteraction.selection=new Set(all.slice(Math.min(index,anchor),Math.max(index,anchor)+1).map(block=>block.id));
  state.selected=id;readingInteraction.side=from;rememberBlockLane(id);applyBlockSelection();
  const pageIndex=state.doc.pages.findIndex(page=>page.blocks.some(block=>block.id===id));
  if(pageIndex>=0)setCurrentPage(pageIndex);
  const origin=from==='source'?$('#source-pane'):$('#translation-pane');
  const dest=from==='source'?$('#translation-pane'):$('#source-pane');
  const original=blockElement(from,id),item=blockElement(from==='source'?'translation':'source',id);
  if(!original || !item)return;
  let offset=original.getBoundingClientRect().top-origin.getBoundingClientRect().top;
  if(offset<0 || offset>origin.clientHeight-40){setPaneScroll(origin,elementTopInPane(origin,original)-35);offset=35;}
  setPaneScroll(dest,elementTopInPane(dest,item)-offset);
}
function hoverBlock(id){
  if(readingInteraction.hovered===id)return;
  readingInteraction.hovered=id;
  $$('[data-block]').forEach(el=>el.classList.toggle('linked-hover',Boolean(id) && el.dataset.block===id));
}
function syncAnchors(index){
  const sourcePane=$('#source-pane'),translationPane=$('#translation-pane');
  const sourcePage=pageElements(sourcePane)[index],translationPage=pageElements(translationPane)[index];
  if(!sourcePage || !translationPage)return [];
  const pairs=[{s:elementTopInPane(sourcePane,sourcePage),t:elementTopInPane(translationPane,translationPage)}];
  for(const block of state.doc.pages[index].blocks){
    const [x0,,x1]=block.bbox,center=(x0+x1)/2;
    // A two-column page has two independent reading positions. Follow the
    // column the reader is pointing at or has selected, using source geometry.
    if(x1-x0<.65 && (center<.5)!==(readingInteraction.lane<.5))continue;
    const original=blockElement('source',block.id),translated=blockElement('translation',block.id);
    if(original && translated)pairs.push({s:elementTopInPane(sourcePane,original),t:elementTopInPane(translationPane,translated)});
  }
  pairs.push({s:elementTopInPane(sourcePane,sourcePage)+sourcePage.getBoundingClientRect().height,
              t:elementTopInPane(translationPane,translationPage)+translationPage.getBoundingClientRect().height});
  pairs.sort((a,b)=>a.s-b.s || a.t-b.t);
  const clean=[];
  for(const pair of pairs){const last=clean.at(-1);if(!last || (pair.s>last.s+.5 && pair.t>last.t+.5))clean.push(pair);}
  return clean;
}
function interpolateReadingPosition(pairs,value,from){
  const a=from==='source'?'s':'t',b=from==='source'?'t':'s';
  if(pairs.length<2)return null;
  if(value<=pairs[0][a])return pairs[0][b]+value-pairs[0][a];
  for(let i=1;i<pairs.length;i++)if(value<=pairs[i][a]){
    const before=pairs[i-1],after=pairs[i];
    return before[b]+(after[b]-before[b])*(value-before[a])/(after[a]-before[a]);
  }
  return pairs.at(-1)[b]+value-pairs.at(-1)[a];
}
function synchronize(from){
  if(!state.doc)return;
  const source=from==='source'?$('#source-pane'):$('#translation-pane');
  const dest=from==='source'?$('#translation-pane'):$('#source-pane');
  const pending=pendingPaneScrolls.get(source);pendingPaneScrolls.delete(source);
  if(pending && Math.abs(source.scrollTop-pending.top)<1 && Math.abs(source.scrollLeft-pending.left)<1)return;
  readingInteraction.side=from;
  const position=panePosition(source);
  if(position)setCurrentPage(position.index);
  if(!$('#sync-scroll').checked)return;
  const focus=Math.min(80,source.clientHeight*.2);
  const focalPosition=panePositionAt(source,source.scrollTop+focus);
  const mapped=focalPosition?interpolateReadingPosition(syncAnchors(focalPosition.index),source.scrollTop+focus,from):null;
  const horizontalMax=source.scrollWidth-source.clientWidth,destHorizontalMax=dest.scrollWidth-dest.clientWidth;
  const left=horizontalMax>0?source.scrollLeft/horizontalMax*Math.max(0,destHorizontalMax):0;
  if(mapped!==null)setPaneScroll(dest,mapped-focus,left);
  else if(position){scrollPaneToPage(dest,position.index,position.fraction);setPaneScroll(dest,dest.scrollTop,left);}
}
function panePositionAt(pane,top){
  const pages=pageElements(pane);if(!pages.length)return null;
  let index=0;
  for(let i=0;i<pages.length;i++){if(elementTopInPane(pane,pages[i])<=top)index=i;else break;}
  return {index};
}
function readerKeydown(event,side){
  if(event.target.closest('input,textarea,select,[contenteditable="true"]'))return;
  if((event.ctrlKey || event.metaKey) && ['+','=','-','0'].includes(event.key) && state.view==='pdf'){
    event.preventDefault();
    setReaderZoom(event.key==='0'?1:readerViewport.zoom*(event.key==='-'?1/1.2:1.2),{side});return;
  }
  if(event.key==='Escape'){
    state.selected=null;readingInteraction.anchor=null;readingInteraction.selection.clear();applyBlockSelection();return;
  }
  const item=event.target.closest('[data-block]');
  if(item && ['ArrowUp','ArrowDown'].includes(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey){
    const all=blocks(),index=all.findIndex(block=>block.id===(state.selected || item.dataset.block));
    const next=all[Math.max(0,Math.min(all.length-1,index+(event.key==='ArrowDown'?1:-1)))];
    if(next){event.preventDefault();selectBlock(next.id,side,event.shiftKey);blockElement(side,next.id)?.focus({preventScroll:true});}
  }else if(event.key==='Enter' && event.target.matches('.translation-card')){
    event.preventDefault();selectBlock(item.dataset.block,side,event.shiftKey);
  }
}
function validAgent(){
  if(!state.agent.base_url || (state.agent.mode==='openai' && !state.agent.model.trim())){showSettings();toast('请先填写接口地址和模型名称');return false;}
  return true;
}
async function runTranslation(selectedBlocks){
  if(state.running || !selectedBlocks.length)return;
  if(!validAgent())return;
  $('#reader-menu').open=false;
  state.running=true;state.stop=false;updateControls();
  let done=0,failure='';
  const request={agent:{...state.agent},target:state.target,...state.context};
  const docId=state.doc.id;
  const groups=TranslationQueue.groupByPage(state.doc.pages,selectedBlocks,state.batchSupported && state.agent.mode==='openai'?state.batchSize:1);
  const pageTracker=TranslationQueue.createPageTracker(state.doc.pages,selectedBlocks,()=>{if(!state.pendingOpen)refreshLayout();});
  let allowBatch=true;
  const send=(path,body)=>TranslationQueue.withRetry(()=>api(path,{method:'POST',body:JSON.stringify(body)}),{
    shouldStop:()=>state.stop,
    onRetry:(error,attempt,total)=>{state.openMessage=`${error.message} · 正在重试（${attempt}/${total}）…`;status(state.openMessage);updateControls();}
  });
  function progress(){
    if(failure)status(`已暂停：${failure}${state.activeBlocks.size?` · 等待 ${state.activeBlocks.size} 段返回`:''}`,true);
    else status(`${state.stop?'正在暂停':'正在翻译'} · 已完成 ${done} / ${selectedBlocks.length} 段 · ${state.activeBlocks.size} 段处理中`);
    if(state.view==='paragraph' && !state.pendingOpen)renderTranslations();else updateControls();
  }
  try{
    await TranslationQueue.run({items:groups,concurrency:state.concurrency,shouldStop:()=>state.stop,
      translate:group=>TranslationQueue.translateGroup({blocks:group.blocks,canBatch:()=>allowBatch,shouldStop:()=>state.stop,
        translateBatch:items=>send(`/api/documents/${docId}/blocks/translate-batch`,{...request,block_ids:items.map(b=>b.id)}),
        translateOne:block=>send(`/api/documents/${docId}/blocks/${block.id}/translate`,request),
        onFallback:()=>{allowBatch=false;state.batchSize=1;persist('parallel-batch-size',1);$('#translation-batch-size').value=1;state.openMessage='合并翻译未返回完整格式，已自动切换为单段翻译…';updateControls();},
        onBlock:result=>{const block=group.blocks.find(b=>b.id===result.id);Object.assign(block,result);done++;state.openMessage='';state.activeBlocks.delete(block.id);delete state.errors[block.id];invalidateLayout(true);pageTracker.mark(block.id);progress();}
      }),
      onStart:group=>{for(const block of group.blocks){state.activeBlocks.add(block.id);delete state.errors[block.id];}progress();},
      onError:(group,error)=>{if(error.cancelled)return;for(const block of group.blocks)if(state.activeBlocks.has(block.id))state.errors[block.id]=error.message;if(!failure){failure=error.message;state.stop=true;toast(error.message);}},
      onSettled:group=>{for(const block of group.blocks)state.activeBlocks.delete(block.id);progress();}
    });
    if(!failure)status(state.stop?`已暂停 · 本次完成 ${done} 段，可继续翻译`:`翻译完成 · 本次完成 ${done} 段`);
  }finally{
    state.running=false;state.stop=false;state.activeBlocks.clear();updateControls();
    if(!state.pendingOpen){if(done)refreshLayout();else renderTranslations();}
    try{await refreshLibrary();}catch(error){toast(error.message);}
  }
}
function formAgent(){const data=Object.fromEntries(new FormData($('#settings-form')));data.timeout=Number(data.timeout);return data;}
function modeVisibility(){const custom=$('#agent-mode').value==='custom';$('#custom-fields').classList.toggle('hidden',!custom);$('#url-help').textContent=custom?'填写完整 POST 接口地址；目前支持非流式 JSON 响应。':'自动追加 /chat/completions，也可填写完整端点。';$('#settings-form [name=model]').required=!custom;}
function showSettings(){
  for(const [key,value]of Object.entries(state.agent)){const input=$(`#settings-form [name="${key}"]`);if(input)input.value=value;}
  setKeyVisible(false);
  modeVisibility();$('#connection-status').textContent='';$('#settings-dialog').showModal();
}
function updateAgentDot(){$('#agent-dot').classList.toggle('configured',Boolean(state.agent.base_url && (state.agent.mode==='custom'||state.agent.model)));}
$('#settings-button').onclick=showSettings;
$('#zoom-in').onclick=()=>setReaderZoom(readerViewport.zoom*1.2);
$('#zoom-out').onclick=()=>setReaderZoom(readerViewport.zoom/1.2);
$('#zoom-reset').onclick=()=>setReaderZoom(1);
$('#zoom-fit').onclick=()=>setReaderZoom(1,{fit:true});
$('#reader-settings').onclick=()=>{$('#reader-menu').open=false;showSettings();};
$('#back-library').onclick=showLibrary;
$('#open-action').onclick=()=>{if(state.running){state.stop=true;updateControls();}else prepareDocument();};
$('#focus-reading').onclick=()=>{$('#reader-menu').open=false;document.body.classList.add('reading-focus');$('#exit-focus').focus();};
function exitFocus(){document.body.classList.remove('reading-focus');$('#reader-menu summary').focus();}
$('#exit-focus').onclick=exitFocus;
document.addEventListener('keydown',event=>{if(event.key==='Escape' && !document.querySelector('dialog[open]')){if(document.body.classList.contains('reading-focus'))exitFocus();$('#reader-menu').open=false;}});
document.addEventListener('click',event=>{if(!event.target.closest('#reader-menu'))$('#reader-menu').open=false;});
function setKeyVisible(visible){const input=$('#api-key');input.type=visible?'text':'password';$('#toggle-api-key').textContent=visible?'隐藏':'显示';$('#toggle-api-key').setAttribute('aria-label',visible?'隐藏 API Key':'显示 API Key');$('#toggle-api-key').setAttribute('aria-pressed',String(visible));}
$('#toggle-api-key').onclick=()=>setKeyVisible($('#api-key').type==='password');
$('#settings-dialog').addEventListener('close',()=>setKeyVisible(false));
$('#clear-api-key').onclick=()=>{state.agent.api_key='';$('#api-key').value='';setKeyVisible(false);if(persist('parallel-agent',state.agent))toast('已清除保存的 API Key');};
$('#reset-agent').onclick=()=>{state.agent={...DEFAULT_AGENT};const saved=persist('parallel-agent',state.agent);updateAgentDot();$('#settings-dialog').close();if(saved)toast('已恢复默认接口，并清除保存的密钥');};
$('#agent-mode').onchange=modeVisibility;
$('#settings-form').onsubmit=event=>{
  event.preventDefault();const config=formAgent();
  if(config.mode==='custom'){try{const value=JSON.parse(config.template);if(!value || Array.isArray(value)||typeof value!=='object')throw new Error();}catch{toast('请求模板必须是有效 JSON 对象');return;}}
  state.agent=config;const saved=persist('parallel-agent',config);updateAgentDot();$('#settings-dialog').close();if(saved)toast('Agent 配置和密钥已保存，下次自动读取');
};
$('#test-connection').onclick=async()=>{
  if(!$('#settings-form').reportValidity())return;
  const button=$('#test-connection'),result=$('#connection-status');button.disabled=true;result.classList.remove('error');result.textContent='正在发送一条简短测试请求…';
  try{const data=await api('/api/agent/test',{method:'POST',body:JSON.stringify(formAgent())});result.textContent=`连接成功 · ${data.message.slice(0,300)}`;}
  catch(error){result.classList.add('error');result.textContent=error.message;}
  finally{button.disabled=false;}
};
$$('.close-dialog').forEach(button=>button.onclick=()=>button.closest('dialog').close());
$('#context-button').onclick=()=>{$('#glossary').value=state.context.glossary;$('#instructions').value=state.context.instructions;$('#context-dialog').showModal();};
$('#home-context').onclick=$('#context-button').onclick;
$('#context-form').onsubmit=event=>{event.preventDefault();state.context={glossary:$('#glossary').value.slice(0,12000),instructions:$('#instructions').value.slice(0,8000)};persist('parallel-context',state.context);$('#context-dialog').close();toast('术语与翻译要求已保存');};
$('#edit-form').onsubmit=async event=>{
  event.preventDefault();const button=$('#edit-form button[type=submit]');button.disabled=true;
  try{const result=await api(`/api/documents/${state.doc.id}/blocks/${editBlock.id}`,{method:'PATCH',body:JSON.stringify({translation:$('#edit-text').value,target:state.target})});Object.assign(editBlock,result);delete state.errors[editBlock.id];invalidateLayout();renderTranslations();$('#edit-dialog').close();refreshLayout();await refreshLibrary();toast('译文已保存');}
  catch(error){toast(error.message);}finally{button.disabled=false;}
};
$('#upload-side').onclick=()=>$('#file-input').click();
$('#drop-zone').onclick=()=>$('#file-input').click();
$('#drop-zone').onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('#file-input').click();}};
$('#file-input').onchange=()=>uploadFile($('#file-input').files[0]);
for(const eventName of ['dragenter','dragover'])$('#drop-zone').addEventListener(eventName,event=>{event.preventDefault();$('#drop-zone').classList.add('dragging');});
for(const eventName of ['dragleave','drop'])$('#drop-zone').addEventListener(eventName,event=>{event.preventDefault();$('#drop-zone').classList.remove('dragging');if(eventName==='drop')uploadFile(event.dataTransfer.files[0]);});
$('#library').onclick=event=>{const button=event.target.closest('[data-doc]');if(button)openDocument(button.dataset.doc);};
$('#previous-page').onclick=()=>choosePage(state.page-1);$('#next-page').onclick=()=>choosePage(state.page+1);
$('#page-select').onchange=event=>choosePage(event.target.value);
$('#target-language').value=state.target;
if(!$('#target-language').value){state.target='简体中文';$('#target-language').value=state.target;}
$('#home-target').innerHTML=$('#target-language').innerHTML;
$('#home-target').value=state.target;
$('#home-target').onchange=event=>{state.target=event.target.value;$('#target-language').value=state.target;persist('parallel-target',state.target);state.openMessage='';invalidateLayout();updateControls();};
$('#target-language').onchange=event=>{state.target=event.target.value;$('#home-target').value=state.target;persist('parallel-target',state.target);invalidateLayout();renderTranslations();refreshLayout();status('目标语言已更新，开始翻译将处理缺少该语言译文的段落');};
$('#translation-view').onchange=event=>{state.view=event.target.value;renderTranslations();pendingPaneScrolls.delete($('#source-pane'));synchronize('source');if(state.view==='pdf' && !state.layoutBusy && state.layoutKey!==layoutKey())refreshLayout();};
$('#translation-concurrency').value=state.concurrency;
$('#translation-concurrency').onchange=event=>{state.concurrency=Number(event.target.value);persist('parallel-concurrency',state.concurrency);};
$('#translation-batch-size').value=state.batchSize;
$('#translation-batch-size').onchange=event=>{state.batchSize=Number(event.target.value);persist('parallel-batch-size',state.batchSize);};
$('#refresh-layout').onclick=()=>refreshLayout();
$('#translate-all').onclick=()=>{if(state.running){state.stop=true;updateControls();status(`将在已发送的 ${state.activeBlocks.size} 段完成后暂停…`);return;}const pending=blocks().filter(block=>!isTranslated(block));if(!pending.length){toast('全文已翻译完成。如需调整，可对单段重新翻译。');return;}runTranslation(pending);};
$('#translate-page').onclick=()=>{const pending=currentPage().blocks.filter(block=>!isTranslated(block));if(!pending.length){toast('本页已翻译完成');return;}runTranslation(pending);};
$('#source-pane').onclick=event=>{const block=event.target.closest('[data-block]');if(block)selectBlock(block.dataset.block,'source',event.shiftKey);};
$('#translation-pane').onclick=async event=>{
  const button=event.target.closest('[data-action]'),card=event.target.closest('[data-block]');
  if(card)selectBlock(card.dataset.block,'translation',event.shiftKey);
  if(!button)return;
  const block=blocks().find(block=>block.id===button.dataset.id);
  if(button.dataset.action==='translate')runTranslation([block]);
  if(button.dataset.action==='edit')showEdit(block);
  if(button.dataset.action==='copy'){try{await navigator.clipboard.writeText(block.translation);toast('已复制译文');}catch{toast('无法访问剪贴板，请手动选择译文复制');}}
};
$('#translation-pane').ondblclick=event=>{
  const item=event.target.closest('.block-overlay');
  if(item && state.view==='pdf' && !state.running)showEdit(blocks().find(block=>block.id===item.dataset.block));
};
for(const side of ['source','translation']){
  const pane=$(`#${side}-pane`);
  pane.addEventListener('wheel',event=>readerWheel(event,side),{passive:false});
  pane.addEventListener('pointerdown',event=>startReaderPan(event,side));
  pane.addEventListener('pointerup',endReaderPan);
  pane.addEventListener('pointercancel',endReaderPan);
  pane.addEventListener('lostpointercapture',endReaderPan);
  pane.addEventListener('click',event=>suppressPanClick(event,side),true);
  pane.addEventListener('dblclick',event=>suppressPanClick(event,side),true);
  pane.addEventListener('scroll',scheduleReaderImages,{passive:true});
  pane.addEventListener('keydown',event=>readerKeydown(event,side));
  pane.addEventListener('pointerover',event=>{
    if(readerViewport.pan?.moved)return;
    const item=event.target.closest('[data-block]');
    if(item){rememberBlockLane(item.dataset.block);hoverBlock(item.dataset.block);}
  });
  pane.addEventListener('pointermove',event=>{
    if(moveReaderPan(event))return;
    readingInteraction.side=side;
    const page=event.target.closest('.pdf-page');
    if(page){const box=page.getBoundingClientRect();readingInteraction.lane=(event.clientX-box.left)/(box.width || 1);}
  });
  pane.addEventListener('pointerout',event=>{if(!event.relatedTarget || !pane.contains(event.relatedTarget))hoverBlock(null);else if(!event.relatedTarget.closest('[data-block]'))hoverBlock(null);});
}
$('#sync-scroll').onchange=()=>{if($('#sync-scroll').checked){pendingPaneScrolls.delete($(`#${readingInteraction.side}-pane`));synchronize(readingInteraction.side);}};
$('#source-pane').addEventListener('scroll',()=>synchronize('source'),{passive:true});
$('#translation-pane').addEventListener('scroll',()=>synchronize('translation'),{passive:true});
let resizeFrame;
window.addEventListener('resize',()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{
  refreshReaderViewport();
});});
window.addEventListener('pointerup',endReaderPan);
window.addEventListener('blur',()=>{if(readerViewport.pan)endReaderPan({pointerId:readerViewport.pan.pointerId});});
window.addEventListener('beforeunload',event=>{if(state.running){event.preventDefault();event.returnValue='';}});
async function init(){try{const session=await api('/api/session');state.token=session.token;state.batchSupported=session.batch_translation===true;await refreshLibrary();updateAgentDot();}catch(error){toast(`初始化失败：${error.message}`);}}
init();
