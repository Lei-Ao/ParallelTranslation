'use strict';
// A bounded queue: pause/error stops new work; every in-flight result is drained.
(function(root){
  async function run({items, concurrency=3, shouldStop=()=>false, translate,
    onStart=()=>{}, onSuccess=()=>{}, onError=()=>{}, onSettled=()=>{}}){
    let next=0, completed=0, failed=0;
    const limit=Math.min(items.length,Math.max(1,Math.min(12,Math.floor(Number(concurrency)) || 3)));
    async function worker(){
      while(next<items.length && !failed && !shouldStop()){
        const item=items[next++];
        onStart(item);
        try{const result=await translate(item);onSuccess(item,result);completed++;}
        catch(error){failed++;onError(item,error);}
        finally{onSettled(item);}
      }
    }
    await Promise.all(Array.from({length:limit},worker));
    return {completed,failed,paused:shouldStop()};
  }
  function createPageTracker(pages,items,onComplete){
    const selected=new Set(items.map(item=>item.id)),owners=new Map(),pending=new Map();
    for(const page of pages){
      const ids=new Set(page.blocks.map(block=>block.id).filter(id=>selected.has(id)));
      if(!ids.size)continue;
      pending.set(page.number,ids);
      for(const id of ids)owners.set(id,page.number);
    }
    return {mark(id){
      const number=owners.get(id),ids=pending.get(number);
      if(!ids || !ids.delete(id))return;
      if(!ids.size){pending.delete(number);onComplete(number);}
    }};
  }
  function groupByPage(pages,items,size=3,maxCharacters=6000,longBlockCharacters=1500){
    const selected=new Set(items.map(item=>item.id)),groups=[];
    for(const page of pages){
      let group=[],characters=0;
      const flush=()=>{if(group.length){groups.push({id:group[0].id,blocks:group});group=[];characters=0;}};
      for(const block of page.blocks){
        if(!selected.has(block.id))continue;
        const length=(block.text||'').length;
        // A very long paragraph can dominate a merged response; let it run on
        // its own worker instead of delaying several short paragraphs.
        if(length>=longBlockCharacters){flush();groups.push({id:block.id,blocks:[block]});continue;}
        if(group.length && (group.length>=size || characters+length>maxCharacters))flush();
        group.push(block);characters+=length;
      }
      flush();
    }
    return groups;
  }
  function createCoalescedTask(task){
    let active=null,pending=false;
    return {request(){
      if(active){pending=true;return active;}
      active=Promise.resolve().then(async()=>{
        do{pending=false;await task();}while(pending);
      }).finally(()=>{active=null;});
      return active;
    }};
  }
  function paused(){return Object.assign(new Error('翻译已暂停'),{cancelled:true});}
  async function withRetry(task,{shouldStop=()=>false,onRetry=()=>{},wait=ms=>new Promise(resolve=>setTimeout(resolve,ms)),delays=[1000,2500]}={}){
    for(let attempt=0;;attempt++){
      if(shouldStop())throw paused();
      try{return await task();}
      catch(error){
        if(!error.retryable || attempt>=delays.length)throw error;
        if(shouldStop())throw paused();
        onRetry(error,attempt+1,delays.length);
        await wait(delays[attempt]);
      }
    }
  }
  async function translateGroup({blocks,canBatch=()=>true,translateBatch,translateOne,onBlock,onFallback=()=>{},shouldStop=()=>false}){
    if(shouldStop())throw paused();
    if(blocks.length>1 && canBatch()){
      try{
        const result=await translateBatch(blocks);
        // Validate the whole group before accepting any result or changing progress.
        if(!Array.isArray(result?.blocks) || result.blocks.length!==blocks.length ||
          new Set(result.blocks.map(b=>b?.id)).size!==blocks.length ||
          blocks.some(block=>!result.blocks.some(b=>b?.id===block.id && typeof b.translation==='string' && b.translation.trim())))
          throw Object.assign(new Error('合并译文格式不完整'),{fallback:'single'});
        for(const block of blocks)onBlock(result.blocks.find(b=>b.id===block.id));
        return;
      }catch(error){if(error.fallback!=='single')throw error;onFallback();}
    }
    // Save each successful single result immediately, including partial fallback success.
    for(const block of blocks){
      if(shouldStop())throw paused();
      const result=await translateOne(block);
      if(result?.id!==block.id || typeof result.translation!=='string' || !result.translation.trim())throw new Error('单段译文格式不完整');
      onBlock(result);
    }
  }
  const api={run,createPageTracker,createCoalescedTask,groupByPage,withRetry,translateGroup};
  if(typeof module==='object' && module.exports)module.exports=api;
  else root.TranslationQueue=api;
})(typeof window==='object'?window:globalThis);
