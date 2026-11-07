'use strict';
// A bounded queue: pause/error stops new work; every in-flight result is drained.
(function(root){
  async function run({items, concurrency=3, shouldStop=()=>false, translate,
    onStart=()=>{}, onSuccess=()=>{}, onError=()=>{}, onSettled=()=>{},
    maxConsecutiveFailures=3}){
    let next=0, completed=0, failed=0, consecutive=0, halted=false;
    const limit=Math.min(items.length,Math.max(1,Math.min(12,Math.floor(Number(concurrency)) || 3)));
    const fatal=error=>Boolean(error && (error.fatal || error.status===401 || error.status===403));
    async function worker(){
      while(next<items.length && !halted && !shouldStop()){
        const item=items[next++];
        onStart(item);
        try{
          const result=await translate(item);
          onSuccess(item,result);completed++;consecutive=0;
        }catch(error){
          failed++;consecutive++;
          // One malformed paragraph must not stop independent work. A burst of
          // failures or an authentication error is systemic, so stop scheduling
          // new groups while already-started requests are still drained.
          if(fatal(error) || consecutive>=maxConsecutiveFailures)halted=true;
          onError(item,error);
        }finally{onSettled(item);}
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
  function groupByPage(pages,items,size=5,maxCharacters=12000,longBlockCharacters=4000){
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
        // A provider rate limit states how long to wait; ignore that hint and
        // retrying after one second only burns the remaining attempts.
        const retryAfter=Number(error.retryAfter);
        const delay=Number.isFinite(retryAfter) && retryAfter>0 ? Math.min(60000,retryAfter*1000) : delays[attempt];
        await wait(delay);
      }
    }
  }
  function acceptedBatch(result,blocks){
    if(!Array.isArray(result?.blocks))return null;
    const wanted=new Set(blocks.map(block=>block.id)),accepted=new Map();
    let duplicate=false;
    const ambiguous=new Set();
    for(const item of result.blocks){
      if(!item || !wanted.has(item.id)){duplicate=true;continue;}
      if(ambiguous.has(item.id) || accepted.has(item.id)){
        accepted.delete(item.id);ambiguous.add(item.id);duplicate=true;continue;
      }
      if(typeof item.translation!=='string' || !item.translation.trim()){duplicate=true;continue;}
      accepted.set(item.id,item);
    }
    if(!accepted.size)return null;
    // A duplicate or unknown id means the response cannot be trusted as a
    // complete mapping, but individually valid paragraphs are still usable.
    return {accepted,duplicate};
  }
  async function translateGroup({blocks,canBatch=()=>true,translateBatch,translateOne,onBlock,onFallback=()=>{},onBatchAccepted=()=>{},shouldStop=()=>false}){
    if(shouldStop())throw paused();
    let remaining=blocks;
    if(blocks.length>1 && canBatch()){
      let fellBack=false;
      try{
        const result=await translateBatch(blocks);
        const parsed=acceptedBatch(result,blocks);
        if(!parsed)throw Object.assign(new Error('合并译文格式不完整'),{fallback:'single'});
        for(const block of blocks){const item=parsed.accepted.get(block.id);if(item)onBlock(item);}
        remaining=blocks.filter(block=>!parsed.accepted.has(block.id));
        onBatchAccepted();
      }catch(error){
        if(error.fallback!=='single')throw error;
        onFallback();fellBack=true;remaining=blocks;
      }
      if(!fellBack && remaining.length>1 && canBatch()){
        // The model often omits exactly one paragraph from an otherwise valid
        // JSON object. Ask once for only the missing ones before paying for
        // individual requests.
        try{
          const retry=acceptedBatch(await translateBatch(remaining),remaining);
          if(retry){
            for(const block of remaining){const item=retry.accepted.get(block.id);if(item)onBlock(item);}
            remaining=remaining.filter(block=>!retry.accepted.has(block.id));
          }
        }catch(error){
          if(error.fallback!=='single')throw error;
          onFallback();
        }
      }
    }
    // Save each successful single result immediately, including partial fallback success.
    for(const block of remaining){
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
