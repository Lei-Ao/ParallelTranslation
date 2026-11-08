'use strict';
// UI adapter: the backend owns checkpoints and never receives browser credentials on GET.
const QualityTranslation=(()=>{
  const $=s=>document.querySelector(s);
  const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={queued:'等待开始',running:'处理中',pausing:'正在暂停',paused:'已暂停',interrupted:'可恢复',failed:'失败',completed:'已完成',needs_review:'待核对',passed:'审校通过',preserved:'原样保留',blocked:'保留原文／旧译文',unreviewed:'未完成审校'};
  let panel=null,dirty=false,openedDoc=null;
  const base=id=>`/api/documents/${id}/quality`;
  const message=text=>{$('#quality-message').textContent=text;};

  function row(entry,index){
    const info=entry.origin==='manual'?'人工词条':`自动 · ${entry.source_ids?.join(', ')||'无出处'}`;
    return `<tr data-entry="${index}"><td><input type="checkbox" data-field="enabled" aria-label="启用词条" ${entry.enabled!==false?'checked':''}></td><td><input data-field="source" aria-label="原词" maxlength="200" value="${escape(entry.source)}"></td><td><input data-field="target" aria-label="译法" maxlength="500" value="${escape(entry.target)}"></td><td><input data-field="tgt_lng" aria-label="目标语言" maxlength="100" value="${escape(entry.tgt_lng)}"></td><td>${escape(info)}${entry.alternatives?.length?`<p>待核对：${escape(entry.alternatives.join(' / '))}</p>`:''}</td></tr>`;
  }
  function renderPanel(e){
    $('#quality-terms').innerHTML=panel.entries.map(row).join('');
    $('#quality-export').href=base(openedDoc)+'/glossary.csv';
    $('#quality-export').download='glossary.csv';
    $('#quality-jobs').innerHTML=[...panel.jobs].reverse().map(j=>`<div class="quality-job"><button class="button subtle" data-job="${escape(j.id)}">${escape(new Date(j.created).toLocaleString())} · 历史记录 · ${escape(labels[j.status]||j.status)} (${j.completed_ids.length}/${j.block_ids.length})</button></div>`).join('')||'<p>没有历史审校记录。</p>';
  }
  async function reload(e){
    if(!e.state.doc)return;
    openedDoc=e.state.doc.id;panel=await e.api(base(openedDoc));dirty=false;
    renderPanel(e);message('仅使用手动词条；不会自动抽取术语。');
  }
  async function showJob(e,id){
    const job=await e.api(`${base(openedDoc)}/jobs/${id}`);
    const stages={terms:'术语',draft:'初译',review:'审校',revise:'修订'};
    let stats=Object.entries(job.metrics).map(([stage,m])=>`${stages[stage]||stage}：${m.requests} 次请求，缓存 ${m.cache_hits} 次，${(m.ms/1000).toFixed(1)} 秒，token ${m.usage?.total_tokens??'未知'}`).join('；');
    $('#quality-records').innerHTML=`<p>${escape(stats)}</p>${job.gaps.map(g=>`<p class="quality-warning">${escape(g)}</p>`).join('')}${job.error?`<p>${escape(job.error)}</p>`:''}`+
      Object.entries({...job.drafts,...job.records}).map(([bid,r])=>`<details class="quality-record"><summary>${escape(bid)} · ${escape(labels[r.status]||r.status)}${r.published?' · 已保存':r.accepted?' · 已接受':''}</summary><p>${escape(r.error)}</p><h4>原文</h4><pre>${escape(r.original.text)}</pre><h4>任务开始时的译文</h4><pre>${escape(r.original.translation||'尚未翻译')}</pre><h4>初稿</h4><pre>${escape(r.draft)}</pre>${r.initial_issues?.length?`<h4>首次审校意见</h4><ul>${r.initial_issues.map(i=>`<li>${escape(i.suggestion)}<br>原文：${escape(i.source_quote)}<br>译文：${escape(i.translation_quote)}</li>`).join('')}</ul>`:''}<h4>候选译文</h4><pre>${escape(r.candidate||r.revision)}</pre>${r.issues?.length?`<h4>仍待核对</h4><ul>${r.issues.map(i=>`<li>${escape(i.suggestion)}<br>原文：${escape(i.source_quote)}<br>译文：${escape(i.translation_quote)}</li>`).join('')}</ul>`:''}${r.candidate&&!r.published&&!r.accepted&&['passed','needs_review'].includes(r.status)?`<button class="button secondary" data-accept="${escape(bid)}" data-job-id="${escape(job.id)}">${r.status==='needs_review'?'已核对，接受候选译文':'接受修改'}</button>`:''}</details>`).join('');
  }
  function bind(e){
    if(!$('#quality-open'))return;
    const safe=fn=>async event=>{try{await fn(event);}catch(error){message(error.message);e.toast(error.message);}};
    $('#quality-open').onclick=safe(async()=>{await reload(e);$('#quality-records').innerHTML='';$('#quality-dialog').showModal();});
    $('#quality-reload').onclick=safe(async()=>{if(dirty){message('请先保存词条修改，再刷新。');return;}await reload(e);});
    $('#quality-terms').oninput=event=>{const tr=event.target.closest('[data-entry]');if(tr){const field=event.target.dataset.field,entry=panel.entries[Number(tr.dataset.entry)];entry[field]=field==='enabled'?event.target.checked:event.target.value;entry.changed=true;dirty=true;}};
    $('#quality-add').onclick=()=>{panel.entries.push({source:'',target:'',tgt_lng:e.state.target,enabled:true,origin:'manual',changed:true});dirty=true;renderPanel(e);};
    $('#quality-save').onclick=safe(async()=>{
      const entries=panel.entries.filter(x=>x.origin==='manual'||x.changed).map(x=>({source:x.source.trim(),target:x.target.trim(),tgt_lng:x.tgt_lng,enabled:x.enabled!==false}));
      if(entries.some(x=>!x.source||!x.target)){message('请填写原词和译法。');return;}
      const result=await e.api(base(openedDoc)+'/glossary',{method:'PUT',body:JSON.stringify({revision:panel.revision,entries})});
      await reload(e);message(`已保存。${result.affected.length} 个已有译文段落可能受影响；可按需重新翻译，不会自动改写。`);
    });
    $('#quality-import').onchange=safe(async event=>{
      if(dirty){message('请先保存词条修改。');event.target.value='';return;}
      const file=event.target.files[0];if(!file)return;
      if(file.size>2000000)throw new Error('CSV 超过 2 MB');
      await e.api(base(openedDoc)+'/glossary/import',{method:'POST',body:JSON.stringify({revision:panel.revision,text:await file.text(),target:e.state.target})});
      event.target.value='';await reload(e);
    });
    $('#quality-jobs').onclick=safe(async event=>{
      const button=event.target.closest('button');if(!button)return;
      if(button.dataset.job)await showJob(e,button.dataset.job);
    });
    $('#quality-records').onclick=safe(async event=>{
      const button=event.target.closest('[data-accept]');if(!button)return;button.disabled=true;
      try{const block=await e.api(`${base(openedDoc)}/jobs/${button.dataset.jobId}/accept`,{method:'POST',body:JSON.stringify({block_id:button.dataset.accept})});
        Object.assign(e.blocks().find(b=>b.id===block.id),block);e.invalidateLayout(true);e.renderTranslations();await e.refreshLayout();await showJob(e,button.dataset.jobId);
      }finally{button.disabled=false;}
    });
  }
  return {bind,escape};
})();
if(typeof module!=='undefined')module.exports=QualityTranslation;
