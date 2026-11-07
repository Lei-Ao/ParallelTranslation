'use strict';

const GuidedReadingModel = {
  active(data) { return Object.values(data?.jobs || {}).find(j=>['queued','running'].includes(j.status)); },
  progress(job) {
    const p=job?.progress;
    if(!p || !Number.isFinite(p.percent))return null;
    return {...p,percent:Math.max(0,Math.min(100,Math.floor(p.percent)))};
  },
  sourceIds(turn) {
    const ids=[...(turn.refs||[]),...(turn.images||[]),...(turn.visual_sources||[]),...(turn.context_sources||[]),...(turn.citations||[]).map(q=>q.id)];
    for(const section of turn.structured?.sections||[])ids.push(...section.source_ids);
    for(const issue of turn.structured?.issues||[])ids.push(...issue.source_ids);
    for(const match of (turn.answer||'').matchAll(/\[\[([^\[\]\n]+)\]\]/g))ids.push(match[1]);
    return [...new Set(ids)];
  },
  annotationCount(data, citations = [], highlights = []) {
    const ids = new Set();
    for (const item of [...(citations || []), ...(highlights || [])]) {
      if (item?.id) ids.add(item.id);
    }
    for (const turn of data?.messages || []) {
      for (const citation of turn.citations || []) if (citation?.id) ids.add(citation.id);
    }
    return ids.size;
  },
  routeUnits(data) {
    const units=data?.structure?.units || [];
    const pattern=data?.route==='method'?/method|approach|model|framework|方法|模型|理论/i:data?.route==='experiments'?/experiment|result|evaluation|实验|结果|评估/i:null;
    const matches=pattern?units.filter(u=>pattern.test(u.title)):units;
    return matches.length?matches:units;
  },
  selected(doc, blockId) {
    for(const page of doc?.pages || []) {const block=page.blocks.find(b=>b.id===blockId);if(block)return {page:page.number,block_id:block.id,text:block.text};}
    return null;
  },
  rectangle(start,end) {return [Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0]),Math.max(start[1],end[1])].map(v=>Math.max(0,Math.min(1,v)));},
  stale(turn, current) {return turn.config_key && current && turn.config_key!==current;}
};
if(typeof module!=='undefined')module.exports=GuidedReadingModel;

if(typeof window!=='undefined') {
  const legacy=globalThis.PaperAssistant, legacyToggle=$('#assistant-toggle').onclick;
  const panel=document.createElement('aside');panel.id='guided-reading';panel.className='guided-reading hidden';panel.setAttribute('aria-label','Skill 论文陪读');
  $('.reading-workspace').append(panel);
  const nav=document.createElement('aside');nav.id='reading-directory';$('.reading-workspace').prepend(nav);
  const controls=document.createElement('div');controls.id='reader-view-switch';controls.innerHTML='<button data-reader-view="source">仅原文</button><button data-reader-view="both" class="active">原文译文</button><button data-reader-view="translation">仅译文</button>';
  $('.reader-toolbar').insertBefore(controls,$('#assistant-toggle'));
  const directoryButton=document.createElement('button');directoryButton.className='button subtle';directoryButton.textContent='目录';directoryButton.id='directory-toggle';$('.reader-toolbar').insertBefore(directoryButton,$('#document-name'));
  controls.onclick=e=>{const view=e.target.closest('[data-reader-view]')?.dataset.readerView;if(view){g.view=view;layout();}};
  directoryButton.onclick=()=>{g.directory=!g.directory;layout();};
  nav.onclick=async e=>{if(e.target.closest('[data-guide-action=directory]')){g.directory=false;layout();return;}const item=e.target.closest('[data-outline]');if(!item)return;const entry=(g.data?.structure?.outline?.items||g.outline.items)[Number(item.dataset.outline)];showSource({doc_id:state.doc.id,...entry});if(g.data){const match=g.data.structure.units.find(u=>u.page===entry.page&&(u.title===entry.title||u.source.block_id===entry.block_id));if(match)await mutate(()=>update({unit_id:match.id}));}};
  async function loadOutline(){if(!state.doc)return;const id=state.doc.id,epoch=g.epoch;try{const result=await api(`/api/documents/${id}/outline`);if(epoch===g.epoch){g.outline=result;renderDirectory();}}catch(e){if(epoch===g.epoch)nav.textContent=e.message;}}
  const g={open:false,id:null,epoch:0,data:null,busy:false,draft:'',timer:null,selection:null,crop:false,
    view:'both',tab:'conversation',notes:[],note:null,citations:[],outline:null,directory:innerWidth>=1440,width:Math.max(320,Math.min(640,stored('parallel-guide-width',400))), signature:'',restored:false,scroll:0};
  let browseSave=Promise.resolve();
  const escape=escapeHtml;
  const readingMarkdown=new marked.Marked({
    renderer:{html({text}){return escape(text);}},
    extensions:[{
      name:'readingMath',level:'inline',
      start(text){return text.indexOf('$');},
      tokenizer(text){
        // Preserve TeX escapes and Markdown punctuation until KaTeX renders them.
        const match=/^(\$\$|\$)(?!\$)((?:\\[\s\S]|[^\\])*?)\1(?!\$)/.exec(text);
        if(match)return {type:'readingMath',raw:match[0]};
      },
      renderer(token){return escape(token.raw);}
    }]
  });
  const settings=()=>({agent:{...state.agent},target:state.target,...state.context});
  const base=()=>`/api/documents/${state.doc.id}/reading`;
  const json=(method,body)=>({method,body:JSON.stringify(body)});
  const button=(action,label,disabled=false)=>`<button class="button secondary" data-guide-action="${action}" ${disabled?'disabled':''}>${label}</button>`;
  const citationLabel=source=>source?.view==='translation'||source?.translated?'译文':'原文';
  const currentSources=()=>g.tab==='notes'&&g.note?g.note.sources:g.data?.sources||{};
  function markdown(text,sources,ids) {
    const clean=text.replace(/\[\[([^\[\]\n]+)\]\]/g,(_,id)=>sources[id]?`[${ids.indexOf(id)+1}](#reading-source-${encodeURIComponent(id)})`:'[来源不可用]');
    // Escape raw HTML tokens. Remove remote images and unsafe URLs after parsing.
    const html=readingMarkdown.parse(clean,{breaks:false});
    return DOMPurify.sanitize(html,{FORBID_TAGS:['img','style','iframe','svg','math'],FORBID_ATTR:['style']});
  }
  function reference(id,sources,ids) {
    const source=sources[id],number=ids.indexOf(id)+1;
    return source?`<a class="guide-reference" href="#reading-source-${encodeURIComponent(id)}" aria-label="来源 ${number}，${citationLabel(source)}第 ${source.page} 页">[${number}]</a>`:`<span class="guide-reference">[${number} 来源不可用]</span>`;
  }
  function answerHTML(turn,sources) {
    const ids=GuidedReadingModel.sourceIds(turn),structured=turn.structured;
    const body=structured?.sections?structured.sections.map(s=>`<section class="guide-section"><h3>${escape(s.section)}</h3>${s.kind==='explanation'?'<p class="guide-explanation">辅助解释</p>':''}${markdown(s.text,sources,ids)}<div class="guide-section-citations">${[...new Set(s.source_ids)].map(id=>reference(id,sources,ids)).join(' ')}</div></section>`).join(''):markdown(turn.answer,sources,ids);
    const issues=structured?.issues||[],missing=issues.length?[]:structured?.missing||[];
    const labels={source_unavailable:'取材失败',visual_ambiguity:'视觉辨认困难',paper_not_specified:'论文交代不足'};
    const issueHTML=issues.map(i=>`<li><strong>${labels[i.category]} · 第 ${i.page} 页</strong><p>${escape(i.description)}</p><p>影响 · ${escape(i.impact)}</p>${i.source_ids.map(id=>reference(id,sources,ids)).join(' ')}</li>`).join('');
    return `<div class="guide-markdown">${body}${issues.length||missing.length?`<aside class="guide-gaps"><h4>待核实事项</h4><ul>${issueHTML}${missing.map(m=>`<li>${escape(m)}</li>`).join('')}</ul></aside>`:''}</div>`+
      (ids.length?`<details class="guide-sources"><summary>原文来源 · ${ids.length}</summary><ol>${ids.map(id=>`<li>${reference(id,sources,ids)} ${sources[id]?`第 ${sources[id].page} 页${sources[id].bbox?' · 图像区域':sources[id].rects||sources[id].ranges?' · 引用'+citationLabel(sources[id]):' · 段落'}`:'来源不可用'}</li>`).join('')}</ol></details>`:'');
  }
  function turnHTML(turn,sources,noteId=null) {
    const disabled=g.busy||Boolean(GuidedReadingModel.active(g.data));
    const saved=g.notes.find(n=>n.origin_session_id===g.data?.id&&n.origin_message_id===turn.id);
    const imageBase=noteId?`${base()}/notes/${noteId}/images`:`${base()}/images`;
    const showQuestion=!noteId||turn.question!==g.note?.title;
    return `<article class="guide-turn" ${noteId?'':`data-turn="${escape(turn.id)}"`}>${showQuestion||turn.citations?.length?`<div class="guide-user">${showQuestion?`<h3>${escape(turn.question||'阅读笔记')}</h3>`:''}${(turn.citations||[]).map(c=>`<button class="message-citation" data-citation-source="${escape(c.id)}"><small>引用${citationLabel(c.source)} · ${c.source.page} 页${c.context_ambiguous?' · 定位需核对':''}</small><p>${escape(c.text.slice(0,180))}</p></button>`).join('')}</div>`:''}
      ${answerHTML(turn,sources)}
      ${turn.analysis_scope?`<p class="guide-evidence">全文共 ${turn.analysis_scope.total_pages} 页，文字来自 ${turn.analysis_scope.text_pages.length} 页${turn.analysis_scope.image_pages?`，已提供 ${new Set(turn.analysis_scope.image_pages).size} 页页面图`:''}。${escape(turn.analysis_scope.note)}</p>`:''}
      ${turn.visual_sources?.length?`<details class="guide-visual-records"><summary>视觉查看记录 · ${turn.visual_sources.length} 个页面或区域</summary>${turn.visual_sources.map(id=>`<button class="guide-image" data-image-source="${escape(id)}"><img loading="lazy" src="${imageBase}/${encodeURIComponent(id)}" alt="${citationLabel(sources[id])}第 ${sources[id]?.page} 页区域"><small>第 ${sources[id]?.page} 页 · 最高 ${sources[id]?.render_scale||2} 倍渲染 · 点击定位</small></button>`).join('')}</details>`:''}
      ${(turn.images||[]).map(id=>`<button class="guide-image" data-image-source="${escape(id)}"><img loading="lazy" src="${imageBase}/${encodeURIComponent(id)}" alt="原文第 ${sources[id]?.page} 页区域"><small>点击定位原文</small></button>`).join('')}
      <details class="guide-evidence"><summary>执行记录</summary><small>Skill ${escape((turn.skill_version||'').slice(0,8))} · 排版 ${escape((turn.format_version||'').slice(0,8))} · ${escape(turn.mode_id||'历史分析')}</small>${(turn.trace||[]).map(x=>`<p>${escape(x.name)} · ${escape(x.status==='ok'?'完成':x.error)}</p>`).join('')}</details>
      ${noteId?'':`<div class="guide-turn-actions"><button data-favorite="${escape(turn.id)}" ${g.busy?'disabled':''}>${saved?'已收藏 · 查看笔记':'☆ 收藏为笔记'}</button><button data-delete-turn="${escape(turn.id)}" ${disabled?'disabled':''}>删除回答</button></div>`}
      </article>`;
  }
  function notesHTML() {
    if(g.note)return `<div class="guide-note-heading">${button('note-list','返回笔记列表')}<h2>${escape(g.note.title)}</h2><small>收藏于 ${escape(new Date(g.note.created).toLocaleString())}</small><div class="guide-note-actions"><a class="button secondary" href="${base()}/notes/${g.note.id}/download" download>导出 Markdown</a><button class="button secondary" data-delete-note="${g.note.id}" ${g.busy?'disabled':''}>删除笔记</button></div></div>${turnHTML(g.note.turn,g.note.sources,g.note.id)}`;
    return '<h2 class="guide-notes-title">本篇论文的笔记</h2>'+(g.notes.length?g.notes.map(n=>`<button class="guide-note-entry" data-open-note="${n.id}"><strong>${escape(n.title)}</strong><small>${escape(new Date(n.created).toLocaleString())}</small><p>${escape(n.preview)}</p></button>`).join(''):'<p class="guide-welcome">点击回答下方的“收藏为笔记”，即可在这里查看和导出。</p>');
  }
  function annotationEntries() {
    const highlights=globalThis.OriginalTextSelection?.highlights?.() || [];
    const staged=g.citations || [];
    const seen=new Set([...highlights,...staged].map(item=>item?.id).filter(Boolean));
    const turns=[];
    for(const turn of g.data?.messages || []) for(const citation of turn.citations || []){
      if(citation?.id && !seen.has(citation.id)){seen.add(citation.id);turns.push(citation);}
    }
    return {highlights,staged,turns};
  }
  function annotationMeta(source,prefix) {
    return `${prefix}${citationLabel(source)}${source?.page?` · 第 ${source.page} 页`:''}${source?.context_ambiguous?' · 定位需核对':''}`;
  }
  function annotationsHTML() {
    const {highlights,staged,turns}=annotationEntries();
    if(!highlights.length && !staged.length && !turns.length)return '<p class="guide-welcome">选中原文后使用「引用」或「高亮」，批注会汇总在这里。</p>';
    const section=(title,body)=>body?`<section class="guide-annotations"><h2 class="guide-notes-title">${title}</h2>${body}</section>`:'';
    const highlightHTML=highlights.map(item=>`<div class="guide-annotation"><button class="guide-annotation-main" data-annotation-highlight="${escape(item.id)}"><small>${escape(annotationMeta(item.source,'高亮 · '))}</small><p>${escape((item.text||'').slice(0,180))}</p></button><button class="guide-annotation-remove" data-remove-annotation="${escape(item.id)}" aria-label="删除高亮">×</button></div>`).join('');
    const stagedHTML=staged.map((item,index)=>`<div class="guide-annotation"><button class="guide-annotation-main" data-preview-quote="${index}"><small>${escape(annotationMeta(item.source,'待发送 · '))}</small><p>${escape((item.text||'').slice(0,180))}</p></button><button class="guide-annotation-remove" data-remove-quote="${index}" aria-label="移除此引用">×</button></div>`).join('');
    const turnHTMLList=turns.map(item=>`<button class="guide-annotation-main" data-citation-source="${escape(item.id)}"><small>${escape(annotationMeta(item.source,'对话 · '))}</small><p>${escape((item.text||'').slice(0,180))}</p></button>`).join('');
    return section('高亮',highlightHTML)+section('待发送引用',stagedHTML)+section('对话引用',turnHTMLList);
  }
  function layout() {
    const reader=$('#reader');reader.classList.add('reading-shell');reader.classList.toggle('guided-open',g.open);reader.classList.toggle('directory-open',g.directory);$('#reading-directory').classList.toggle('hidden',!g.directory);$$('[data-reader-view]').forEach(b=>b.classList.toggle('active',b.dataset.readerView===g.view));
    reader.dataset.guideView=g.view;reader.style.setProperty('--guide-width',g.width+'px');
    $('#assistant-toggle').setAttribute('aria-expanded',String(g.open));
    window.dispatchEvent(new Event('resize'));
  }
  function show(open) {
    if(open && !state.doc)return;
    g.open=open;panel.classList.toggle('hidden',!open);
    if(open){if(!$('#reading-assistant').classList.contains('hidden'))$('#assistant-close').click();$('#reader').classList.remove('assistant-open');}
    else {g.scroll=panel.querySelector('.guide-conversation')?.scrollTop||0;saveBrowse();}
    layout();clearTimeout(g.timer);
    if(open){globalThis.OriginalTextSelection?.refresh?.();render();return browseSave.then(load);}else $('#assistant-toggle').focus();
  }
  function showSource(source) {
    if(!source || source.doc_id!==state.doc?.id)return;
    const translated=source.view==='translation' || source.view==='paragraph';
    if(source.view==='translation' && source.revision!==state.layout?.revision){toast('此引用属于其他译文排版版本，请查看引用中保留的文字。');return;}
    if(source.view==='paragraph' && state.view!=='paragraph'){state.view='paragraph';$('#translation-view').value='paragraph';renderTranslations();}
    if(source.view==='translation' && state.view!=='pdf'){state.view='pdf';$('#translation-view').value='pdf';renderTranslations();}
    if(g.view===(translated?'source':'translation'))g.view='both';layout();
    const index=state.doc.pages.findIndex(p=>p.number===source.page);if(index<0)return;
    const pane=translated?$('#translation-pane'):$('#source-pane');
    scrollPaneToPage(pane,index,source.rects?.[0]?.[1] || source.bbox?.[1] || 0);setCurrentPage(index);
    if(source.rects || source.view==='paragraph')clearBlockSelection();else if(source.block_id)selectBlock(source.block_id,'source');
    $$('.guide-highlight').forEach(el=>el.remove());
    globalThis.CSS?.highlights?.delete('reader-preview');
    for(const box of source.rects || (source.bbox?[source.bbox]:[])){const page=pane.querySelector(`[data-page-index="${index}"]`);if(page){const el=document.createElement('div');el.className='guide-highlight';const [x,y,r,b]=box;Object.assign(el.style,{left:x*100+'%',top:y*100+'%',width:(r-x)*100+'%',height:(b-y)*100+'%'});page.append(el);}}
    if(source.view==='paragraph'){
      const range=globalThis.OriginalTextSelection?.paragraphRange(source);
      if(range && globalThis.CSS?.highlights){CSS.highlights.set('reader-preview',new Highlight(range));range.startContainer.parentElement.scrollIntoView({block:'center'});}
      else toast('段落内容已改变，请查看引用中保留的文字。');
    }
    if(innerWidth<1040)show(false);
  }
  function renderDirectory(){
    const nav=$('#reading-directory');
    const items=g.data?.structure?.outline?.items || g.outline?.items || [];
    const units=g.data?.structure?.units || [];
    let activeIndex=items.findLastIndex(item=>item.page<state.page+1 || (item.page===state.page+1&&(item.fraction||0)<=(g.browseFraction||0)+.01));
    if(activeIndex<0)activeIndex=items.findIndex(item=>item.page===state.page+1);
    nav.innerHTML='<header>目录<button class="icon-button" data-guide-action="directory" aria-label="收起目录">‹</button></header>'+
      (items.length?items.map((item,i)=>`<button class="directory-item ${i===activeIndex?'current':''}" data-outline="${i}" style="padding-left:${16+Math.min(4,item.level-1)*14}px"><span>${escape(item.title)}</span><small>${item.page}</small></button>`).join(''):'<p class="assistant-meta">正在读取目录…</p>')+
      (g.data?`<footer>已完成 ${Object.values(g.data.progress).filter(v=>v==='completed').length} / ${units.length} 个阅读单元</footer>`:'');
  }
  function render() {
    const active=document.activeElement,editing=active?.id==='guide-question'&&!$('.panes').contains(getSelection()?.anchorNode),start=active?.selectionStart,end=active?.selectionEnd;
    const scroller=panel.querySelector('.guide-conversation'),top=scroller?.scrollTop || 0;
    const moreOpen=Boolean(panel.querySelector('.guide-more')?.open);
    const data=g.data,job=GuidedReadingModel.active(data),progress=GuidedReadingModel.progress(job),prepared=job?.progress,disabled=g.busy||Boolean(job),old=data&&!data.workflow_version,composerOld=old&&!g.citations.length;
    const annotationTotal=GuidedReadingModel.annotationCount(data,g.citations,globalThis.OriginalTextSelection?.highlights?.()||[]);
    panel.innerHTML=`<div class="guide-resizer" role="separator" tabindex="0" aria-label="调整助手宽度" aria-orientation="vertical"></div>
      <header class="guide-header"><strong>AI 助读</strong><div><details class="guide-more" ${moreOpen?'open':''}><summary aria-label="更多助读选项">···</summary><div>
      ${button('history','历史分析')}${button('test','测试模型能力',disabled)}${button('visual','看当前页',disabled)}${button('crop','框选图表',disabled)}
      ${data?button('summary','生成阅读小结',disabled)+button('clear','清空对话',g.busy):''}
      ${old?button('archive','归档并新建会话',disabled):''}
      </div></details><button class="icon-button" data-guide-action="close" aria-label="收起助读">×</button></div></header>
      <div class="guide-tabs" role="tablist" aria-label="助读内容"><button role="tab" aria-selected="${g.tab==='conversation'}" data-guide-action="conversation">对话</button><button role="tab" aria-selected="${g.tab==='notes'}" data-guide-action="notes">笔记${g.notes.length?` · ${g.notes.length}`:''}</button><button role="tab" aria-selected="${g.tab==='annotations'}" data-guide-action="annotations">批注${annotationTotal?` · ${annotationTotal}`:''}</button></div>
      ${g.tab==='conversation'?`<div class="guide-modes">${button('start','文章速读',disabled||old)}${button('continue','逐节陪读',disabled||old)}</div>`:''}
      <div id="guide-notice" role="status"></div>
      <div class="guide-conversation" aria-label="助读对话">
      ${g.tab==='notes'?notesHTML():g.tab==='annotations'?annotationsHTML():`${old?'<p class="assistant-warning">此会话保留旧版 Skill。请在更多菜单归档并新建，使用规范阅读方式。</p>':''}
      ${!data?.messages.length?'<div class="guide-welcome"><p>选中原文，解释一句话，或引用多段文字一起讨论。</p><p class="assistant-meta">选择文章速读或逐节陪读，按 Skill 阅读。</p></div>':''}
      ${(data?.messages||[]).map(t=>turnHTML(t,data.sources)).join('')}`}
      </div>
      <div class="guide-composer ${g.tab!=='conversation'&&!job?'hidden':''}">
      ${job?`<div class="guide-working" role="status"><div>${escape(job.stage||'准备中')} ${button('stop','停止')}</div>
      <progress max="100" ${progress&&(prepared?.phase==='preparing'||progress.total_chunks)?`value="${progress.percent}"`:''} aria-label="文章分析进度"></progress>
      ${prepared?.prepared_pages!==undefined?`<small>页面图已准备 ${prepared.prepared_pages}/${prepared.total_pages} 页${prepared.round?` · 补读与核查第 ${prepared.round}/${prepared.max_rounds} 轮`:''}</small>`:prepared?.round?`<small>补读与核查第 ${prepared.round}/${prepared.max_rounds} 轮</small>`:progress?.total_chunks?`<small>已分析 ${progress.completed_chunks}/${progress.total_chunks} 块</small>`:'<small>正在处理，请稍候…</small>'}</div>`:''}
      ${g.tab==='conversation'?`<div class="guide-composer-box"><div id="guide-selection"></div><form id="guide-form"><textarea id="guide-question" rows="2" maxlength="4000" placeholder="问一个问题，或引用选中的文字…" aria-label="助读问题">${escape(g.draft)}</textarea><div class="guide-sendbar"><small>通过 Skill 回答 · ${escape(state.agent.model||'请配置模型')}</small><button class="button primary" aria-label="发送问题" ${disabled||composerOld?'disabled':''}>↑</button></div></form></div>`:''}</div>`;
    const last=Object.values(data?.jobs||{}).at(-1);
    if(last&&['failed','interrupted','cancelled','limited'].includes(last.status))$('#guide-notice').innerHTML=`<p class="assistant-warning">${escape(last.error||'本轮尚未完成')} ${button('retry','重试',disabled)}</p>`;
    selectionPreview();renderDirectory();
    panel.querySelectorAll('.guide-markdown').forEach(el=>globalThis.renderMathInElement?.(el,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false},{left:'\\(',right:'\\)',display:false},{left:'\\[',right:'\\]',display:true}],throwOnError:false,trust:false,strict:'ignore'}));
    if(editing){const field=$('#guide-question');field?.focus({preventScroll:true});field?.setSelectionRange(start,end);}
    panel.querySelector('.guide-conversation').scrollTop=top;
    const resize=panel.querySelector('.guide-resizer');
    resize.onpointerdown=e=>{resize.setPointerCapture(e.pointerId);resize.onpointermove=x=>{g.width=Math.max(320,Math.min(640,innerWidth-x.clientX));layout();};resize.onpointerup=()=>{resize.onpointermove=null;persist('parallel-guide-width',g.width);};};
    resize.onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();g.width=Math.max(320,Math.min(640,g.width+(e.key==='ArrowLeft'?20:-20)));layout();persist('parallel-guide-width',g.width);}};
  }
  function selectionPreview(){
    const target=$('#guide-selection');if(!target)return;
    target.innerHTML=g.citations.map((c,i)=>`<div class="guide-quote-card"><button data-remove-quote="${i}" aria-label="移除此引用">×</button><button data-preview-quote="${i}"><small>引用${citationLabel(c.source)} · ${c.source.page} 页${c.context_ambiguous?' · 定位需核对':''}</small><p>${escape(c.text.slice(0,180))}</p></button></div>`).join('');
    const selection=g.selection || (!g.citations.length&&GuidedReadingModel.selected(state.doc,state.selected));
    if(selection)target.innerHTML+=`<div class="guide-quote-card"><small>第 ${selection.page} 页${selection.bbox?'框选区域':'段落引用'}</small><p>${escape((selection.text||'将查看实际图像').slice(0,140))}</p>${button('unselect','移除')}</div>`;
  }
  async function load() {
    if(!g.open||!state.doc)return;
    const epoch=g.epoch,url=base();
    try {
      const [data,notes]=await Promise.all([api(url),api(url+'/notes')]);if(epoch!==g.epoch)return;
      const signature=JSON.stringify([data,notes]);
      if(signature!==g.signature){g.signature=signature;g.data=data.session===null?null:data;g.notes=notes.notes;if(g.note&&!g.notes.some(n=>n.id===g.note.id))g.note=null;render();}
      if(!g.restored && g.data){g.restored=true;const position=stored('parallel-guide-position-'+state.doc.id,{page:g.data.browse_page,fraction:0});scrollPaneToPage($('#source-pane'),position.page-1,position.fraction);setCurrentPage(position.page-1);panel.scrollTop=g.scroll;}
      clearTimeout(g.timer);if(GuidedReadingModel.active(g.data))g.timer=setTimeout(load,1200);
    }catch(e){if(epoch===g.epoch)panel.querySelector('#guide-notice').textContent=ReadingAssistantModel.loadError(e);}
  }
  async function mutate(action) {
    if(g.busy)return;
    const epoch=g.epoch;g.busy=true;render();
    try {await action();}catch(e){if(epoch===g.epoch)toast(e.message);}
    finally {if(epoch===g.epoch){g.busy=false;g.signature='';await load();}}
  }
  async function update(values) {
    const epoch=g.epoch;const data=await api(base(),json('PATCH',{revision:g.data.revision,...values}));
    if(epoch!==g.epoch)return false;
    g.data=data;return true;
  }
  async function submit(action, extra={}) {
    if(!validAgent())return;
    await mutate(async()=>{
      const url=base(),epoch=g.epoch;
      if(!g.data){const data=await api(url,{method:'POST'});if(epoch!==g.epoch)return;g.data=data;}
      let selection=g.selection || GuidedReadingModel.selected(state.doc,state.selected);
      if(['continue','start','summary','test'].includes(action))selection=null;
      const request={...settings(),revision:g.data.revision,action,mode_id:action==='test'?null:action==='start'?'quickread':action==='continue'?'guided':action==='message'&&g.citations.length?'citation':'followup',citations:action==='message'?g.citations.map(c=>c.request):[],question:action==='message'?g.draft:'',
        ...(selection?.bbox?{page:selection.page,bbox:selection.bbox}:selection?{block_id:selection.block_id}:{}),...extra};
      if(selection?.bbox && action==='message')request.action='visual';
      await api(url+'/messages',json('POST',request));
      if(epoch===g.epoch && action==='message'){g.draft='';g.citations=[];}
    });
  }
  function saveBrowse() {
    if(!g.data||!state.doc||g.busy)return;
    const url=base(),revision=g.data.revision,page=state.page+1;
    browseSave=api(url,json('PATCH',{revision,browse_page:page})).catch(()=>{});
    return browseSave;
  }
  async function openNote(id) {
    await mutate(async()=>{
      const epoch=g.epoch,note=await api(base()+'/notes/'+encodeURIComponent(id));
      if(epoch===g.epoch){g.note=note;g.tab='notes';}
    });
    panel.querySelector('.guide-conversation').scrollTop=0;
  }
  panel.addEventListener('input',e=>{if(e.target.id==='guide-question')g.draft=e.target.value;});
  panel.addEventListener('submit',e=>{if(e.target.id==='guide-form'){e.preventDefault();if(g.draft.trim())submit('message');}});
  panel.addEventListener('change',e=>{if(e.target.id==='guide-view'){g.view=e.target.value;layout();}if(e.target.id==='guide-route')mutate(()=>update({route:e.target.value}));});
  panel.addEventListener('click',async e=>{
    const menu=panel.querySelector('.guide-more');
    if(menu?.open&&(!e.target.closest('.guide-more')||e.target.closest('[data-guide-action]')))menu.open=false;
    const anchor=e.target.closest('a');
    if(anchor?.getAttribute('href')?.startsWith('#reading-source-')){e.preventDefault();showSource(currentSources()[decodeURIComponent(anchor.getAttribute('href').slice(16))]);return;}
    if(anchor && !anchor.hasAttribute('download')){anchor.target='_blank';anchor.rel='noopener noreferrer';}
    const image=e.target.closest('[data-image-source]');if(image){showSource(currentSources()[image.dataset.imageSource]);return;}
    const noteEntry=e.target.closest('[data-open-note]');if(noteEntry){openNote(noteEntry.dataset.openNote);return;}
    const favorite=e.target.closest('[data-favorite]');if(favorite){
      const saved=g.notes.find(n=>n.origin_session_id===g.data.id&&n.origin_message_id===favorite.dataset.favorite);
      if(saved){openNote(saved.id);return;}
      mutate(async()=>{await api(base()+'/notes',json('POST',{message_id:favorite.dataset.favorite}));});return;
    }
    const deletedTurn=e.target.closest('[data-delete-turn]');if(deletedTurn){
      if(confirm('删除这条提问和回答？已收藏的笔记可在笔记列表查看。'))mutate(()=>api(base()+'/messages/'+encodeURIComponent(deletedTurn.dataset.deleteTurn),json('DELETE',{revision:g.data.revision})));return;
    }
    const deletedNote=e.target.closest('[data-delete-note]');if(deletedNote){
      if(confirm('删除这篇本地笔记？'))mutate(()=>api(base()+'/notes/'+encodeURIComponent(deletedNote.dataset.deleteNote),{method:'DELETE'}));return;
    }
    const unit=e.target.closest('[data-unit]');if(unit){mutate(async()=>{if(!await update({unit_id:unit.dataset.unit}))return;showSource(g.data.structure.units.find(u=>u.id===unit.dataset.unit).source);});return;}
    const remove=e.target.closest('[data-remove-quote]');if(remove){g.citations.splice(Number(remove.dataset.removeQuote),1);g.tab==='annotations'?render():selectionPreview();return;}
    const preview=e.target.closest('[data-preview-quote]');if(preview){showSource(g.citations[Number(preview.dataset.previewQuote)].source);return;}
    const removeAnnotation=e.target.closest('[data-remove-annotation]');if(removeAnnotation){globalThis.OriginalTextSelection?.removeHighlight?.(removeAnnotation.dataset.removeAnnotation);return;}
    const annotation=e.target.closest('[data-annotation-highlight]');if(annotation){const item=(globalThis.OriginalTextSelection?.highlights?.()||[]).find(highlight=>highlight.id===annotation.dataset.annotationHighlight);if(item)showSource(item.source);return;}
    const quote=e.target.closest('[data-citation-source]');if(quote){showSource(currentSources()[quote.dataset.citationSource]);return;}
    const action=e.target.closest('[data-guide-action]')?.dataset.guideAction;if(!action)return;
    if(action==='notes'||action==='conversation'||action==='annotations'||action==='note-list'){g.tab=action==='conversation'?'conversation':action==='annotations'?'annotations':'notes';g.note=null;render();panel.querySelector('.guide-conversation').scrollTop=0;return;}
    if(action==='close'){show(false);return;}
    if(action==='history'){show(false);legacyToggle();return;}
    if(action==='unselect'){g.selection=null;clearBlockSelection();selectionPreview();return;}
    if(action==='crop'){g.crop=true;$('#reader').classList.add('quote-cropping');if(g.view==='translation')g.view='both';layout();toast('在原文页上拖动框选，按 Escape 取消');if(innerWidth<1040)show(false);return;}
    if(action==='clear'){if(confirm('清空本篇论文的全部助读对话并停止当前分析？已收藏的笔记可在笔记列表查看。'))mutate(async()=>{const epoch=g.epoch;await api(base()+'/messages',json('DELETE',{revision:g.data.revision}));if(epoch===g.epoch){g.draft='';g.citations=[];g.selection=null;}});return;}
    if(action==='archive'){if(confirm('归档旧版会话并创建新版会话？'))mutate(async()=>{const epoch=g.epoch;await api(base(),{method:'DELETE'});if(epoch===g.epoch){g.data=null;g.restored=false;}});return;}
    if(action==='stop'){const job=GuidedReadingModel.active(g.data);if(job)mutate(()=>api(base()+'/jobs/'+job.id,{method:'DELETE'}));return;}
    if(action==='retry'){const last=Object.values(g.data.jobs).at(-1);submit(last.action,{question:last.question,block_id:last.block_id,page:last.page,bbox:last.bbox,mode_id:last.mode_id,citations:last.citations||[]});return;}
    if(action==='visual'){submit('visual',{page:g.selection?.page || state.page+1,bbox:g.selection?.bbox || [0,0,1,1]});return;}
    submit(action);
  });
  // Capture before existing PDF click handlers so a crop does not change paragraph selection.
  let drag=null,suppressClick=false;
  const sourcePane=$('#source-pane');
  let lastWidth=innerWidth;
  window.addEventListener('resize',()=>{if(lastWidth>=1440&&innerWidth<1440){g.directory=false;$('#reading-directory').classList.add('hidden');$('#reader').classList.remove('directory-open');}lastWidth=innerWidth;});
  let positionTimer;
  sourcePane.addEventListener('scroll',()=>{clearTimeout(positionTimer);positionTimer=setTimeout(()=>{
    if(!state.doc)return;
    const pages=[...sourcePane.querySelectorAll('.reader-page')],rect=sourcePane.getBoundingClientRect();
    const page=pages.find(p=>p.getBoundingClientRect().bottom>rect.top+20);if(!page)return;
    const bounds=page.getBoundingClientRect();
    g.browseFraction=Math.max(0,Math.min(1,(rect.top-bounds.top)/bounds.height));renderDirectory();
    persist('parallel-guide-position-'+state.doc.id,{page:state.doc.pages[Number(page.dataset.pageIndex)].number,fraction:Math.max(0,Math.min(1,(rect.top-bounds.top)/bounds.height))});
  },200);},{passive:true});
  sourcePane.addEventListener('pointerdown',e=>{
    if(!g.crop)return;
    const page=e.target.closest('.reader-page');if(!page)return;
    e.preventDefault();e.stopImmediatePropagation();page.setPointerCapture(e.pointerId);
    const rect=page.getBoundingClientRect();drag={page,index:Number(page.dataset.pageIndex),start:[(e.clientX-rect.left)/rect.width,(e.clientY-rect.top)/rect.height]};
    $$('.guide-highlight').forEach(x=>x.remove());drag.overlay=document.createElement('div');drag.overlay.className='guide-highlight';page.append(drag.overlay);
  },true);
  sourcePane.addEventListener('pointermove',e=>{
    if(!drag)return;e.preventDefault();const r=drag.page.getBoundingClientRect();drag.bbox=GuidedReadingModel.rectangle(drag.start,[(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height]);
    const [x,y,b,c]=drag.bbox;Object.assign(drag.overlay.style,{left:x*100+'%',top:y*100+'%',width:(b-x)*100+'%',height:(c-y)*100+'%'});
  },true);
  sourcePane.addEventListener('pointerup',e=>{
    if(!drag)return;e.preventDefault();e.stopImmediatePropagation();suppressClick=true;
    const box=drag.bbox;if(box && box[2]-box[0]>.005 && box[3]-box[1]>.005){g.selection={page:state.doc.pages[drag.index].number,bbox:box};g.crop=false;$('#reader').classList.remove('quote-cropping');show(true);selectionPreview();}
    drag=null;
  },true);
  sourcePane.addEventListener('click',e=>{if(suppressClick){suppressClick=false;e.preventDefault();e.stopImmediatePropagation();}},true);
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&g.crop){g.crop=false;$('#reader').classList.remove('quote-cropping');drag=null;toast('已取消框选');}});
  $('#assistant-toggle').onclick=()=>show(!g.open);
  globalThis.PaperAssistant={
    async addCitations(quotes,explain){
      const epoch=g.epoch;g.tab='conversation';g.note=null;g.selection=null;clearBlockSelection();
      for(const c of quotes){if(!g.citations.some(x=>x.id===c.id))g.citations.push(c);}
      const loading=show(true);
      if(explain){await loading;if(epoch!==g.epoch)return;g.draft='请按 Skill 解释引用的文字，结合论文原文说明。';render();await submit('message');}
      else {selectionPreview();panel.querySelector('#guide-question')?.focus();}
    },
    documentChanged(){legacy?.documentChanged();if(g.id!==state.doc?.id){g.epoch++;g.id=state.doc?.id;clearTimeout(g.timer);Object.assign(g,{data:null,draft:'',selection:null,citations:[],outline:null,busy:false,signature:'',restored:false,scroll:0,view:'both',tab:'conversation',notes:[],note:null});}layout();loadOutline();globalThis.OriginalTextSelection?.refresh();if(g.open){render();load();}},
    selectionChanged(){legacy?.selectionChanged();g.selection=null;if(g.open)selectionPreview();}
  };
  document.addEventListener('parallel:highlights-changed',()=>{if(g.open)render();});
  $('#settings-dialog').addEventListener('close',()=>{if(g.open)load();});
  window.addEventListener('pagehide',saveBrowse);
}
