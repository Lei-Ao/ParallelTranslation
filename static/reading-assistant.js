'use strict';

// Small pure helpers are shared with regression tests.
const ReadingAssistantModel = {
  loadError(error) {
    if(error.status===404 && error.message==='Not Found')return '当前后台服务尚未加载论文助手接口。请在系统托盘菜单选择「重新启动服务」，然后刷新此页面；只关闭或重新打开侧栏无效。';
    if(error.status===403)return '后台服务可能已重启，会话已失效。请刷新页面后重新打开论文助手。';
    return error.message+' · 可重新打开助手重试';
  },
  latest(results, kind, blockId) {
    return Object.values(results || {}).filter(r=>r.kind===kind && (!blockId || r.block_id===blockId))
      .sort((a,b)=>b.created.localeCompare(a.created))[0];
  },
  stale(result, config) {
    return Boolean(result && config && (result.config_key!==config.config_key || result.input_digest!==config.input_digest));
  },
  active(jobs) { return Object.values(jobs || {}).filter(j=>['queued','running'].includes(j.status)); },
  belongs(reference, doc) {
    return reference.doc_id===doc?.id && doc.pages.some(p=>p.number===reference.page &&
      (!reference.block_id || p.blocks.some(b=>b.id===reference.block_id)));
  }
};
if(typeof module!=='undefined')module.exports=ReadingAssistantModel;

if(typeof window!=='undefined') {
  const assistant={open:false, tab:'outline', id:null, epoch:0, data:null, outline:null,
    config:null, refs:[], timer:null, busy:false, signature:'', draft:'', scope:'document'};
  const root=$('#reading-assistant'),content=$('#assistant-content'),message=$('#assistant-status');
  const esc=escapeHtml;
  const settings=()=>({agent:{...state.agent},target:state.target,...state.context});
  function note(text){message.textContent=text;}
  function resize(){window.dispatchEvent(new Event('resize'));}
  function show(open){
    assistant.open=open;root.classList.toggle('hidden',!open);
    $('#assistant-toggle').setAttribute('aria-expanded',String(open));
    $('#reader').classList.toggle('assistant-open',open);resize();
    clearTimeout(assistant.timer);
    if(open){$('#assistant-close').focus();load();}else $('#assistant-toggle').focus();
  }
  function sourceButton(source,label){
    if(!ReadingAssistantModel.belongs(source,state.doc))return '<small>来源已不可定位</small>';
    const index=assistant.refs.push(source)-1;
    const number=state.doc.pages.find(p=>p.number===source.page)?.blocks.findIndex(b=>b.id===source.block_id);
    return `<button class="assistant-source" data-source-index="${index}">${esc(label || `第 ${source.page} 页${source.block_id?` · 第 ${number+1} 段`:' · 页面'}`)}</button>`;
  }
  function resultHTML(result){
    if(!result)return '<p class="assistant-empty">尚未生成。点击按钮将调用当前模型；已有结果会自动保存。</p>';
    const coverage=result.coverage;
    return `<p class="assistant-meta">${esc(result.model || '自定义模型')} · ${esc(result.target)} · ${esc(new Date(result.created).toLocaleString())}</p>`+
      (ReadingAssistantModel.stale(result,assistant.config)?'<p class="assistant-warning">由旧配置或旧内容生成，可保留查看；点击重新生成才会再次调用模型。</p>':'')+
      `<p class="assistant-meta">文字覆盖页：${coverage.covered_pages.join('、') || '无'} / 共 ${coverage.total_pages} 页。${esc(coverage.note)}</p>`+
      (coverage.unreadable_pages.length?`<p class="assistant-warning">无可提取文字：第 ${coverage.unreadable_pages.join('、')} 页。</p>`:'')+
      coverage.failed_chunks.map(c=>`<p class="assistant-warning">第 ${c.index} 块（页 ${c.pages.join('、')}）失败：${esc(c.reason)}</p>`).join('')+
      result.points.map(p=>`<article class="assistant-point"><strong>${esc(p.section)}</strong><small class="assistant-kind">${{claim:'论文陈述',explanation:'辅助解释',missing:'依据不足'}[p.kind]}</small><p>${esc(p.text)}</p><div>${p.sources.map(s=>sourceButton(s)).join('')}</div></article>`).join('');
  }
  function render(){
    const focused=document.activeElement;
    const restore=content.contains(focused) && focused.id ? {id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
    const top=root.scrollTop;
    renderContent();
    if(restore){const input=document.getElementById(restore.id);if(input){input.focus({preventScroll:true});if(typeof restore.start==='number')input.setSelectionRange(restore.start,restore.end);}}
    root.scrollTop=top;
  }
  function renderContent(){
    assistant.refs=[];
    $$('#assistant-tabs button').forEach(b=>{const active=b.dataset.assistantTab===assistant.tab;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});
    if(!assistant.data){content.innerHTML='<p>正在加载本地阅读数据…</p>';return;}
    const results=assistant.data.results;
    const active=ReadingAssistantModel.active(assistant.data.jobs);
    const disabled=assistant.busy?' disabled':'';
    if(assistant.tab==='outline') {
      content.innerHTML=`<p class="assistant-meta">${esc(assistant.outline?.label || '目录加载失败，可重新打开助手重试')}</p>`+
        (assistant.outline?.items || []).map(item=>`<div class="outline-item" style="padding-left:${Math.min(4,Math.max(0,item.level-1))*12}px">${sourceButton({doc_id:state.doc.id,...item},item.title)}</div>`).join('');
    }else if(assistant.tab==='overview') {
      const result=ReadingAssistantModel.latest(results,'overview');
      const running=active.some(j=>j.kind==='overview');
      content.innerHTML=`<div class="assistant-actions"><button class="button primary" data-action="overview"${disabled || (running?' disabled':'')}>${result?'重新生成概览':'生成概览'} · 调用模型</button>${result?.coverage.failed_chunks.length?'<button class="button secondary" data-action="resume">重试缺失分块</button>':''}</div>`+resultHTML(result);
    }else if(assistant.tab==='detail') {
      const block=blocks().find(b=>b.id===state.selected);
      if(!block){content.innerHTML='<p class="assistant-empty">在原文或译文中单击一个段落，查看翻译、解释和总结。</p>';return;}
      const page=state.doc.pages.find(p=>p.blocks.some(b=>b.id===block.id));
      content.innerHTML=`<p class="assistant-meta">当前选择：第 ${page.number} 页 · 第 ${page.blocks.findIndex(b=>b.id===block.id)+1} 段</p><details><summary>原文</summary><p class="assistant-text">${esc(block.text)}</p></details><details ${block.translation?'open':''}><summary>已有译文</summary><p class="assistant-text">${esc(block.translation || '尚未翻译')}</p></details><div class="assistant-actions"><button class="button secondary" data-action="translate"${disabled || (state.running?' disabled':'')}>翻译</button><button class="button secondary" data-action="explain"${disabled}>解释</button><button class="button secondary" data-action="summary"${disabled}>总结</button></div><p class="assistant-meta">首次点击调用模型，重复点击复用缓存。</p>`+
        ['explain','summary'].map(kind=>{const r=ReadingAssistantModel.latest(results,kind,block.id);return r?`<h3>${kind==='explain'?'段落解释':'段落总结'}</h3><button class="text-button" data-regenerate="${kind}"${disabled}>重新生成 · 调用模型</button>${resultHTML(r)}`:'';}).join('');
    }else {
      const last=Object.values(assistant.data.jobs).filter(j=>j.kind==='question').slice(-1)[0];
      const failed=last && ['failed','interrupted'].includes(last.status)?last:null;
      content.innerHTML=`<div class="assistant-actions"><button class="button subtle" data-action="clear">清空当前论文对话</button></div>`+
        assistant.data.conversation.map(turn=>`<section class="assistant-turn"><h3>${esc(turn.question)}</h3>${resultHTML(turn.result)}</section>`).join('')+
        (failed?`<p class="assistant-warning">${esc(failed.error)} <button data-retry-question="${esc(failed.id)}" class="text-button">重试上次问题</button></p>`:'')+
        `<form id="assistant-question"><label>提问范围<select id="assistant-scope"><option value="document" ${assistant.scope==='document'?'selected':''}>当前论文（检索相关原文）</option><option value="selection" ${assistant.scope==='selection'?'selected':''}>所选段落及相邻上下文</option></select></label><label>问题<textarea id="assistant-input" rows="4" maxlength="4000" required placeholder="例如：作者的方法与已有方法有什么区别？">${esc(assistant.draft)}</textarea></label><button class="button primary"${disabled || (active.some(j=>j.kind==='question')?' disabled':'')}>发送 · 调用模型</button></form>`;
    }
  }
  async function load(){
    if(!assistant.open || !state.doc)return;
    const id=state.doc.id,epoch=assistant.epoch;
    try{
      const requests=[api(`/api/documents/${id}/analysis`),api(`/api/documents/${id}/analysis/config`,{method:'POST',body:JSON.stringify(settings())})];
      if(!assistant.outline)requests.push(api(`/api/documents/${id}/outline`));
      const [data,config,outline]=await Promise.all(requests);
      if(epoch!==assistant.epoch || state.doc?.id!==id)return;
      assistant.data=data;assistant.config=config;if(outline)assistant.outline=outline;
      const signature=JSON.stringify([data,config]);
      if(signature!==assistant.signature){assistant.signature=signature;render();}
      const active=ReadingAssistantModel.active(data.jobs);
      const last=Object.values(data.jobs).slice(-1)[0];
      const failed=last && ['failed','interrupted'].includes(last.status)?last:null;
      note(active.length?active.map(j=>`${{overview:'概览',question:'问答',summary:'段落总结',explain:'段落解释'}[j.kind]}处理中${j.total?` · ${j.completed}/${j.total} 块`:''}，可关闭侧栏或刷新页面`).join('；'):failed?failed.error:'已保存于本机 · 点击来源可回到原文');
      clearTimeout(assistant.timer);
      if(active.length && assistant.open)assistant.timer=setTimeout(load,1500);
    }catch(error){if(epoch===assistant.epoch){note(ReadingAssistantModel.loadError(error));}}
  }
  async function submit(kind,force=false,extra={}){
    if(assistant.busy || !validAgent())return;
    const id=state.doc.id,epoch=assistant.epoch,block=state.selected;
    if((['explain','summary'].includes(kind) || extra.scope==='selection') && !block && !extra.block_id){toast('请先选中段落');return;}
    assistant.busy=true;render();note('正在提交任务…');
    try{
      const job=await api(`/api/documents/${id}/analysis/jobs`,{method:'POST',body:JSON.stringify({...settings(),kind,block_id:block,force,...extra})});
      if(epoch!==assistant.epoch || state.doc?.id!==id)return;
      if(kind==='question' && !job.cached)assistant.draft='';
      if(job.cached)note('已复用缓存，未调用模型');
    }catch(error){if(epoch===assistant.epoch)toast(error.message);}
    finally{if(epoch===assistant.epoch){assistant.busy=false;assistant.signature='';await load();}}
  }
  function jump(source){
    if(!ReadingAssistantModel.belongs(source,state.doc)){toast('来源不属于当前文档');return;}
    const index=state.doc.pages.findIndex(p=>p.number===source.page);
    scrollPaneToPage($('#source-pane'),index,source.fraction || 0);
    scrollPaneToPage($('#translation-pane'),index,source.fraction || 0);
    setCurrentPage(index);
    if(source.block_id)selectBlock(source.block_id,'source');
  }
  content.addEventListener('input',event=>{if(event.target.id==='assistant-input')assistant.draft=event.target.value;});
  content.addEventListener('change',event=>{if(event.target.id==='assistant-scope')assistant.scope=event.target.value;});
  content.addEventListener('submit',event=>{
    if(event.target.id!=='assistant-question')return;
    event.preventDefault();if(!assistant.draft.trim())return;
    submit('question',false,{question:assistant.draft,scope:assistant.scope});
  });
  content.addEventListener('click',async event=>{
    const ref=event.target.closest('[data-source-index]');
    if(ref){jump(assistant.refs[Number(ref.dataset.sourceIndex)]);return;}
    const regen=event.target.closest('[data-regenerate]');
    if(regen){submit(regen.dataset.regenerate,true);return;}
    const retry=event.target.closest('[data-retry-question]');
    if(retry){const job=assistant.data.jobs[retry.dataset.retryQuestion];submit('question',false,{question:job.question,scope:job.scope,block_id:job.block_id});return;}
    const action=event.target.closest('[data-action]')?.dataset.action;
    if(!action)return;
    if(action==='clear'){
      if(!window.confirm('清空当前论文的所有对话？正在生成的回答也不会再写入。'))return;
      const id=state.doc.id,epoch=assistant.epoch;
      try{await api(`/api/documents/${id}/analysis/conversation`,{method:'DELETE'});if(epoch===assistant.epoch)load();}catch(error){toast(error.message);}
    }else if(action==='translate'){
      const block=blocks().find(b=>b.id===state.selected);if(!block || state.running || !validAgent())return;
      await runTranslation([block]);if(assistant.tab==='detail')render();
    }else if(action==='resume')submit('overview');
    else submit(action,action==='overview' && Boolean(ReadingAssistantModel.latest(assistant.data.results,'overview')));
  });
  $('#assistant-tabs').onclick=event=>{const tab=event.target.closest('[data-assistant-tab]');if(tab){assistant.tab=tab.dataset.assistantTab;render();}};
  $('#assistant-toggle').onclick=()=>show(!assistant.open);
  $('#assistant-close').onclick=()=>show(false);
  $('#settings-dialog').addEventListener('close',()=>{if(assistant.open)load();});
  $('#context-dialog').addEventListener('close',()=>{if(assistant.open)load();});
  $('#target-language').addEventListener('change',()=>{if(assistant.open)load();});
  root.addEventListener('keydown',event=>{if(event.key==='Escape'){event.stopPropagation();show(false);}});
  globalThis.PaperAssistant={
    documentChanged(){
      if(assistant.id===state.doc?.id){if(assistant.open)load();return;}
      clearTimeout(assistant.timer);assistant.epoch++;assistant.id=state.doc?.id;
      Object.assign(assistant,{data:null,outline:null,config:null,signature:'',busy:false,draft:'',scope:'document'});
      if(assistant.open){render();load();}
    },
    selectionChanged(){if(assistant.open && assistant.tab==='detail')render();}
  };
}
