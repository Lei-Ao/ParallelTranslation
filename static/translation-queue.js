'use strict';
// A bounded queue: pause/error stops new work; every in-flight result is drained.
(function(root){
  async function run({items, concurrency=3, shouldStop=()=>false, translate,
    onStart=()=>{}, onSuccess=()=>{}, onError=()=>{}, onSettled=()=>{},
    maxConsecutiveFailures=3, ready=()=>true}){
    let completed=0, failed=0, consecutive=0, halted=false, active=0;
    const pending=items.slice();
    const limit=()=>Math.max(1,Math.min(12,Math.floor(Number(typeof concurrency==='function'?concurrency():concurrency)) || 3));
    const fatal=error=>Boolean(error && (error.fatal || error.status===401 || error.status===403));
    return new Promise(resolve=>{
      async function execute(item){
        active++;
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
        }finally{onSettled(item);active--;pump();}
      }
      function pump(){
        while(pending.length && active<limit() && !halted && !shouldStop()){
          const index=pending.findIndex(ready);
          if(index<0)break;
          execute(pending.splice(index,1)[0]);
        }
        if(!active){
          if(!halted && !shouldStop())for(const item of pending.splice(0)){
            failed++;onError(item,Object.assign(new Error('前一续写片段尚未完成，当前段落等待其重试成功'),{dependency:true}));onSettled(item);
          }
          resolve({completed,failed,paused:shouldStop()});
        }
      }
      pump();
    });
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
  function groupByPage(pages,items,size=3,maxCharacters=12000,longBlockCharacters=4000,dependencies={}){
    const automatic=size==='auto';
    // Keep the hard transport limits, but avoid output-heavy groups: in live
    // reasoning-model tests a 7-block prose batch took longer than two smaller
    // requests. Short labels still benefit from the full twelve-block budget.
    if(automatic){size=12;maxCharacters=1200;longBlockCharacters=1000;}
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
        if(group.length && (group.length>=size || characters+length>maxCharacters || dependencies[block.id]
          || (automatic && Boolean(group[0].reference)!==Boolean(block.reference))))flush();
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
  function createPagePrewarmer(task,onError=()=>{}){
    const pending=new Set();let active=null;
    const start=()=>{
      if(!active)active=Promise.resolve().then(async()=>{
        while(pending.size){const pages=[...pending].slice(0,2);for(const page of pages)pending.delete(page);try{await task(pages);}catch(error){onError(error);}}
      }).finally(()=>{active=null;if(pending.size)return start();});
      return active;
    };
    return {add(page){pending.add(page);return start();},cancelPending(){pending.clear();},drain:()=>active || Promise.resolve()};
  }
  function paused(){return Object.assign(new Error('翻译已暂停'),{cancelled:true});}
  function retryDelay(value,now=Date.now()){
    if(value===null || value===undefined || value==='')return 0;
    const seconds=Number(value);
    return Number.isFinite(seconds)?Math.max(0,seconds*1000):Math.max(0,(Date.parse(value)||0)-now);
  }
  async function pauseableWait(ms,shouldStop){
    const until=Date.now()+ms;
    while(Date.now()<until){if(shouldStop())throw paused();await new Promise(resolve=>setTimeout(resolve,Math.min(250,until-Date.now())));}
    if(shouldStop())throw paused();
  }
  function createAdaptiveConcurrency({initial=3,maximum=6,now=Date.now}={}){
    let limit=initial,successes=0,timeouts=0,cooldown=0;
    return {limit:()=>limit,
      success(clean=true){timeouts=0;if(!clean){successes=0;return;}if(++successes>=12){limit=Math.min(maximum,limit+1);successes=0;}},
      failure(error){
        successes=0;
        const limited=(error.agentStatus || error.status)===429;
        timeouts=(error.status===504 || error.agentStatus===408)?timeouts+1:0;
        if(limited || timeouts>=2){limit=Math.max(1,Math.floor(limit/2));timeouts=0;}
        if(limited)cooldown=Math.max(cooldown,now()+(retryDelay(error.retryAfter,now())||2500));
      },
      enter(shouldStop=()=>false){
        if(shouldStop())throw paused();
        if(cooldown<=now())return;
        return (async()=>{while(cooldown>now())await pauseableWait(Math.min(250,cooldown-now()),shouldStop);})();
      }
    };
  }
  async function withRetry(task,{shouldStop=()=>false,onRetry=()=>{},beforeAttempt=()=>{},onAttemptError=()=>{},onWait=()=>{},wait=null,delays=[1000,2500]}={}){
    for(let attempt=0;;attempt++){
      if(shouldStop())throw paused();
      try{const waiting=beforeAttempt();if(waiting)await waiting;if(shouldStop())throw paused();return await task();}
      catch(error){
        if(!error.cancelled)onAttemptError(error);
        if(!error.retryable || attempt>=delays.length)throw error;
        if(shouldStop())throw paused();
        onRetry(error,attempt+1,delays.length);
        // A provider rate limit states how long to wait; ignore that hint and
        // retrying after one second only burns the remaining attempts.
        const delay=retryDelay(error.retryAfter) || delays[attempt];
        const started=Date.now();
        try{await (wait?wait(delay):pauseableWait(delay,shouldStop));}finally{onWait(Date.now()-started);}
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
      try{
        const result=await translateBatch(blocks);
        const parsed=acceptedBatch(result,blocks);
        if(!parsed)throw Object.assign(new Error('合并译文格式不完整'),{fallback:'single'});
        for(const block of blocks){const item=parsed.accepted.get(block.id);if(item)onBlock(item);}
        remaining=blocks.filter(block=>!parsed.accepted.has(block.id));
        onBatchAccepted();
      }catch(error){
        if(error.fallback!=='single')throw error;
        onFallback();remaining=blocks;
      }
      if(remaining.length>1 && canBatch()){
        // The model often omits exactly one paragraph from an otherwise valid
        // JSON object. Ask once for only the missing ones before paying for
        // individual requests.
        try{
          const size=Math.max(1,Math.floor(blocks.length/2));
          if(size>1){
            for(let index=0;index<remaining.length;index+=size)await translateGroup({
              blocks:remaining.slice(index,index+size),canBatch,translateBatch,translateOne,onBlock,onFallback,onBatchAccepted,shouldStop});
            return;
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
  const api={run,createPageTracker,createCoalescedTask,createPagePrewarmer,groupByPage,withRetry,translateGroup,createAdaptiveConcurrency,retryDelay};
  if(typeof module==='object' && module.exports)module.exports=api;
  else root.TranslationQueue=api;
})(typeof window==='object'?window:globalThis);
