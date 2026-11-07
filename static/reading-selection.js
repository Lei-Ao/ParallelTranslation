'use strict';
if(typeof window!=='undefined'){
  let documentId=null,sequence=0,selectionRequests=[],highlights=[],activeHighlights=[],paintedHighlights=[];
  const colors={yellow:{label:'黄色',value:'#f2cf4c'},green:{label:'绿色',value:'#70cf96'},blue:{label:'蓝色',value:'#7fb5f5'},pink:{label:'粉色',value:'#f397b8'},purple:{label:'紫色',value:'#b39aef'}};
  let selectedColor=stored('parallel-highlight-color','yellow');if(!Object.hasOwn(colors,selectedColor))selectedColor='yellow';
  const panes=[document.querySelector('#source-pane'),document.querySelector('#translation-pane')];
  const float=document.createElement('div');float.id='quote-toolbar';float.className='hidden';
  float.setAttribute('role','toolbar');float.setAttribute('aria-label','选中文字操作');
  float.innerHTML='<div class="quote-actions"><button data-quote="translate">翻译</button><button data-quote="explain">解释</button><button data-quote="add">引用</button><button data-quote="highlight">高亮</button><button data-quote="remove-highlight" class="hidden">取消高亮</button></div>'+
    '<div class="highlight-colors" role="group" aria-label="高亮颜色">'+Object.entries(colors).map(([key,color])=>`<button data-highlight-color="${key}" style="--highlight-color:${color.value}" aria-label="${color.label}高亮" title="${color.label}高亮" aria-pressed="${key===selectedColor}"></button>`).join('')+'</div>';
  document.body.append(float);
  function refresh(){
    if(!state.doc)return;
    if(documentId!==state.doc.id){
      documentId=state.doc.id;sequence++;hide();
      highlights=stored('parallel-highlights-'+documentId,[]);
      if(!Array.isArray(highlights))highlights=[];
    }
    paintHighlights();
  }
  function hide(){float.classList.add('hidden');selectionRequests=[];activeHighlights=[];float.querySelector('[data-quote=remove-highlight]').classList.add('hidden');}
  function positionToolbar(rect){
    float.classList.remove('hidden');float.style.left=Math.max(8,Math.min(innerWidth-float.offsetWidth-8,rect.left))+'px';float.style.top=Math.max(8,Math.min(innerHeight-float.offsetHeight-8,rect.top-float.offsetHeight-5))+'px';
  }
  function markColor(color){selectedColor=color;float.querySelectorAll('[data-highlight-color]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.highlightColor===color)));}
  function saveHighlights(next){if(documentId!==state.doc?.id || !persist('parallel-highlights-'+documentId,next))return false;highlights=next;paintHighlights();document.dispatchEvent(new CustomEvent('parallel:highlights-changed',{detail:{docId:documentId}}));return true;}
  function highlightAt(event){
    for(const painted of paintedHighlights.slice().reverse()){
      if(painted.pane.contains(event.target) && [...painted.region.getClientRects()].some(rect=>event.clientX>=rect.left && event.clientX<=rect.right && event.clientY>=rect.top && event.clientY<=rect.bottom))return painted;
    }
    return null;
  }
  function positionActiveHighlight(){
    for(const painted of paintedHighlights){
      if(!activeHighlights.includes(painted.quote.id))continue;
      const bounds=painted.pane.getBoundingClientRect();
      for(const rect of painted.region.getClientRects()){
        if(rect.bottom>=bounds.top && rect.top<=bounds.bottom && rect.right>=bounds.left && rect.left<=bounds.right){positionToolbar(rect);return;}
      }
    }
    hide();
  }
  document.addEventListener('pointerdown',event=>{if(!float.contains(event.target))hide();},true);
  let timer;function schedule(){clearTimeout(timer);timer=setTimeout(refresh,120);}
  new MutationObserver(schedule).observe(panes[1],{childList:true});
  for(const pane of panes){
    new ResizeObserver(schedule).observe(pane);pane.addEventListener('scroll',()=>{schedule();requestAnimationFrame(()=>{if(activeHighlights.length)positionActiveHighlight();else updateSelection();});},{passive:true});
    pane.addEventListener('click',e=>{
      if((pane===panes[1]&&state.editTranslation) || document.querySelector('#reader').classList.contains('quote-cropping'))return;
      if(getSelection()?.toString() && e.target.closest('.pdf-char,.translation-text,.source-preview')){e.stopImmediatePropagation();e.preventDefault();return;}
      const painted=highlightAt(e);if(!painted)return;
      e.stopImmediatePropagation();e.preventDefault();hide();activeHighlights=[painted.quote.id];selectionRequests=[structuredClone(painted.quote.request)];
      markColor(Object.hasOwn(colors,painted.quote.color)?painted.quote.color:'yellow');float.querySelector('[data-quote=remove-highlight]').classList.remove('hidden');positionToolbar(painted.region.getBoundingClientRect());
    },true);
  }
  document.addEventListener('keydown',e=>{if(e.key==='Escape')hide();});
  // Map the display's softened whitespace back to the stored paragraph text.
  function characterMap(content,raw){
    const display=Array.from(content.textContent),original=Array.from(raw),map=[];let cursor=0;
    for(const char of display){
      while(cursor<original.length && original[cursor]!==char && /\s/.test(original[cursor]))cursor++;
      if(/\s/.test(char) && !/\s/.test(original[cursor]||'')){map.push(cursor);continue;}
      if(original[cursor]!==char)return null;
      map.push(cursor++);
    }
    return map;
  }
  function paragraphPart(content,range){
    const part=range.cloneRange();if(!content.contains(range.startContainer))part.setStart(content,0);if(!content.contains(range.endContainer))part.setEnd(content,content.childNodes.length);
    const before=document.createRange();before.selectNodeContents(content);before.setEnd(part.startContainer,part.startOffset);
    const block=blocks().find(b=>b.id===content.closest('[data-block]').dataset.block);
    const translated=content.classList.contains('translation-text') && Boolean(block.translation);
    const raw=translated ? block.translation : block.text,map=characterMap(content,raw);
    const start=Array.from(before.toString()).length,end=start+Array.from(part.toString()).length;
    if(!map || end<=start)return null;
    return {page:Number(content.closest('.reader-page').dataset.pageIndex)+1,view:'paragraph',block_id:block.id,translated,raw,
      ranges:[{run_id:block.id,start:map[start],end:map[end-1]+1}]};
  }
  function selectedGlyph(glyph,range){
    const node=glyph.firstChild;if(!node)return null;
    const start=range.startContainer===node?Array.from(node.textContent.slice(0,range.startOffset)).length:0;
    const end=range.endContainer===node?Array.from(node.textContent.slice(0,range.endOffset)).length:Array.from(node.textContent).length;
    return end>start?{node,start,end}:null;
  }
  function clearSelectionVisual(){document.querySelectorAll('.pdf-selection-rect').forEach(el=>el.remove());}
  // The text layer is one transparent span per glyph. Native ::selection paints
  // one translucent box per glyph, so overlapping glyph boxes show seams and
  // darker patches. Draw one merged band per visual line instead, which keeps
  // per-glyph citation ranges and copy behavior but looks like a run text layer.
  function paintSelectionVisual(pane,range){
    const bounds=pane.getBoundingClientRect();
    for(const page of pane.querySelectorAll('.pdf-page')){
      const pageBox=page.getBoundingClientRect();
      if(pageBox.bottom<bounds.top || pageBox.top>bounds.bottom)continue;
      const layer=page.querySelector('.pdf-text-layer');
      if(!layer || !range.intersectsNode(layer))continue;
      const layerBox=layer.getBoundingClientRect();
      if(!(layerBox.width>0 && layerBox.height>0))continue;
      for(const line of layer.querySelectorAll('.pdf-text-line')){
        if(!range.intersectsNode(line))continue;
        let left=Infinity,top=Infinity,right=-Infinity,bottom=-Infinity;
        for(const glyph of line.querySelectorAll('.pdf-char')){
          if(!range.intersectsNode(glyph) || !selectedGlyph(glyph,range))continue;
          const box=glyph.getBoundingClientRect();
          if(!(box.width>0 && box.height>0))continue;
          left=Math.min(left,box.left);top=Math.min(top,box.top);
          right=Math.max(right,box.right);bottom=Math.max(bottom,box.bottom);
        }
        if(!(right>left && bottom>top))continue;
        const el=document.createElement('div');el.className='pdf-selection-rect';
        Object.assign(el.style,{left:(left-layerBox.left)/layerBox.width*100+'%',
          top:(top-layerBox.top)/layerBox.height*100+'%',
          width:(right-left)/layerBox.width*100+'%',
          height:(bottom-top)/layerBox.height*100+'%'});
        layer.append(el);
      }
    }
  }
  function updateSelection(){
    clearSelectionVisual();
    const selection=getSelection();if(activeHighlights.length && (!selection || selection.isCollapsed))return;
    const pane=panes.find(p=>p.contains(selection?.anchorNode)&&p.contains(selection?.focusNode));hide();
    if(!selection?.rangeCount||selection.isCollapsed||!pane)return;
    const range=selection.getRangeAt(0);
    // Draw the merged selection even when this pane cannot produce a citation,
    // so copying a page without a usable text mapping still has feedback.
    paintSelectionVisual(pane,range);
    if(pane===panes[1]&&state.editTranslation)return;
    let mappable=true;
    for(const layer of pane.querySelectorAll('.pdf-text-layer')){
      if(!range.intersectsNode(layer))continue;
      const ranges=[];
      for(const line of layer.querySelectorAll('.pdf-text-line')){
        if(!range.intersectsNode(line))continue;
        for(const glyph of line.querySelectorAll('.pdf-char')){
          if(!range.intersectsNode(glyph))continue;
          const part=selectedGlyph(glyph,range);if(!part)continue;
          // Never cite a different or partially recoverable string in place of the selection.
          if(!glyph.dataset.run){
            if(part.node.textContent.trim()){mappable=false;break;}
            continue;
          }
          const offset=Number(glyph.dataset.start),last=ranges.at(-1);
          if(last?.run_id===glyph.dataset.run){
            if(last.end!==offset+part.start){mappable=false;break;}
            last.end=offset+part.end;
          }else ranges.push({run_id:glyph.dataset.run,start:offset+part.start,end:offset+part.end});
        }
        if(!mappable)break;
      }
      if(!mappable)break;
      if(ranges.length)selectionRequests.push({page:Number(layer.dataset.page),text_version:layer.dataset.version,
        view:layer.dataset.view,revision:layer.dataset.revision || null,ranges});
    }
    if(!mappable){hide();return;}
    for(const content of pane.querySelectorAll('.translation-text,.source-preview')){if(range.intersectsNode(content)){const part=paragraphPart(content,range);if(part)selectionRequests.push(part);}}
    if(!selectionRequests.length)return;
    const rect=range.getBoundingClientRect(),bounds=pane.getBoundingClientRect();if(rect.bottom<bounds.top || rect.top>bounds.bottom)return;
    positionToolbar(rect);
  }
  document.addEventListener('selectionchange',updateSelection);
  float.addEventListener('pointerdown',e=>e.preventDefault());
  float.addEventListener('click',async e=>{
    const color=e.target.closest('[data-highlight-color]')?.dataset.highlightColor;
    if(color){
      if(activeHighlights.length && !saveHighlights(highlights.map(h=>activeHighlights.includes(h.id)?{...h,color}:h)))return;
      markColor(color);persist('parallel-highlight-color',color);return;
    }
    const action=e.target.dataset.quote;if(!action)return;
    if(action==='remove-highlight'){if(saveHighlights(highlights.filter(h=>!activeHighlights.includes(h.id)))){hide();getSelection()?.removeAllRanges();toast('已取消高亮');}return;}
    const id=state.doc?.id,epoch=sequence,highlightColor=selectedColor,requests=selectionRequests.map(r=>structuredClone(r));hide();if(!requests.length)return;
    try{const quotes=[];for(const request of requests){
        if(request.view==='paragraph' && typeof request.raw==='string'){const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(request.raw)));request.text_version=Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('');delete request.raw;}
        quotes.push(await api(`/api/documents/${id}/reading/citations`,{method:'POST',body:JSON.stringify(request)}));}
      if(epoch!==sequence||state.doc?.id!==id)return;
      if(action==='highlight'){
        const next=[...highlights.filter(h=>!quotes.some(q=>q.id===h.id)),...quotes.map(q=>({...q,color:highlightColor}))];
        if(saveHighlights(next))toast('已高亮，点击高亮文字可调整颜色或取消');
      }else globalThis.PaperAssistant?.addCitations(quotes,action);getSelection()?.removeAllRanges();
    }catch(error){toast(error.message);}
  });
  function textRange(content,start,end){
    const walker=document.createTreeWalker(content,NodeFilter.SHOW_TEXT),range=document.createRange();let offset=0,node,started=false;
    while(node=walker.nextNode()){
      const length=Array.from(node.textContent).length;
      if(!started && start<offset+length){range.setStart(node,Array.from(node.textContent).slice(0,start-offset).join('').length);started=true;}
      if(started && end<=offset+length){range.setEnd(node,Array.from(node.textContent).slice(0,end-offset).join('').length);return range;}
      offset+=length;
    }
    return null;
  }
  function paragraphRange(source){
    const block=blocks().find(b=>b.id===source.block_id);if(!block)return null;
    const translated=source.translated !== false;
    const content=document.getElementById('card-'+block.id)?.querySelector(translated?'.translation-text':'.source-preview');if(!content)return null;
    const raw=translated ? (block.translation || block.text) : block.text,map=characterMap(content,raw),item=source.ranges[0];if(!map || !item)return null;
    if(Array.from(raw).slice(item.start,item.end).join('')!==source.text)return null;
    const start=map.findIndex(i=>i>=item.start),last=map.findLastIndex(i=>i<item.end);if(start<0 || last<start)return null;
    return textRange(content,start,last+1);
  }
  function paintHighlights(){
    document.querySelectorAll('.saved-text-highlight').forEach(el=>el.remove());
    paintedHighlights=[];
    const paragraphRanges=Object.fromEntries(Object.keys(colors).map(key=>[key,[]]));
    for(const quote of highlights){
      const source=quote.source;if(!source || source.doc_id!==state.doc?.id)continue;
      const color=Object.hasOwn(colors,quote.color)?quote.color:'yellow';
      if(source.view==='paragraph'){const range=paragraphRange(source);if(range){paragraphRanges[color].push(range);paintedHighlights.push({quote,pane:panes[1],region:range});}continue;}
      for(const pane of panes){
        const revision=pane===panes[1]?state.layout?.revision:null;
        if((source.view==='translation') ? pane!==panes[1] || source.revision!==revision : Boolean(revision))continue;
        const page=pane.querySelector(`[data-page-index="${source.page-1}"]`),layer=page?.querySelector('.pdf-text-layer');
        if(!layer || layer.dataset.version!==source.text_version)continue;
        for(const [x,y,r,b] of source.rects || []){const el=document.createElement('div');el.className='saved-text-highlight';el.dataset.highlight=quote.id;el.dataset.color=color;Object.assign(el.style,{left:x*100+'%',top:y*100+'%',width:(r-x)*100+'%',height:(b-y)*100+'%',background:colors[color].value+'66'});page.append(el);paintedHighlights.push({quote,pane,region:el});}
      }
    }
    if(globalThis.CSS?.highlights && globalThis.Highlight){for(const [color,ranges] of Object.entries(paragraphRanges))CSS.highlights.set(color==='yellow'?'reader-saved':'reader-saved-'+color,new Highlight(...ranges));}
    if(activeHighlights.length)positionActiveHighlight();
  }
  function currentHighlights(){
    if(documentId!==state.doc?.id){
      documentId=state.doc?.id;
      highlights=stored('parallel-highlights-'+documentId,[]);
      if(!Array.isArray(highlights))highlights=[];
    }
    return highlights.map(highlight=>({...highlight}));
  }
  globalThis.OriginalTextSelection={refresh:schedule,paragraphRange,
    highlights:currentHighlights,
    removeHighlight:id=>{currentHighlights();return saveHighlights(highlights.filter(highlight=>highlight.id!==id));}};
  schedule();
}
