'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {run,createPageTracker,createCoalescedTask,groupByPage,withRetry,translateGroup}=require('../static/translation-queue.js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function controlled(){
  const pending=new Map(),started=[],saved=[];
  return {pending,started,saved, options:{
    translate:item=>new Promise((resolve,reject)=>{started.push(item);pending.set(item,{resolve,reject});}),
    onSuccess:(item,value)=>saved.push([item,value])
  }};
}
test('bounded parallelism preserves identity for out-of-order results',async()=>{
  const c=controlled();
  const job=run({items:[0,1,2,3,4],concurrency:3,...c.options});
  assert.deepEqual(c.started,[0,1,2]);
  c.pending.get(1).resolve('one');await tick();assert.deepEqual(c.started,[0,1,2,3]);
  c.pending.get(3).resolve('three');await tick();assert.deepEqual(c.started,[0,1,2,3,4]);
  c.pending.get(4).resolve('four');c.pending.get(2).resolve('two');c.pending.get(0).resolve('zero');
  assert.equal((await job).completed,5);
  assert.deepEqual(c.saved,[[1,'one'],[3,'three'],[4,'four'],[2,'two'],[0,'zero']]);
});
test('pause stops new requests and drains in-flight work before resolving',async()=>{
  const c=controlled();let stop=false,finished=false;
  const job=run({items:[0,1,2,3],concurrency:2,shouldStop:()=>stop,...c.options}).then(r=>{finished=true;return r;});
  stop=true;c.pending.get(0).resolve('zero');await tick();
  assert.equal(finished,false);assert.deepEqual(c.started,[0,1]);
  c.pending.get(1).resolve('one');
  assert.deepEqual(await job,{completed:2,failed:0,paused:true});
});
test('failure stops scheduling but retains successful concurrent results',async()=>{
  const c=controlled(),errors=[];
  const job=run({items:[0,1,2,3,4],concurrency:3,...c.options,onError:(item,error)=>errors.push([item,error.message])});
  c.pending.get(1).reject(new Error('rate limit'));await tick();
  assert.deepEqual(c.started,[0,1,2]);
  c.pending.get(0).resolve('zero');c.pending.get(2).resolve('two');
  assert.deepEqual(await job,{completed:2,failed:1,paused:false});
  assert.deepEqual(errors,[[1,'rate limit']]);assert.equal(c.saved.length,2);
});
test('sequential mode receives prior results before the next request starts',async()=>{
  const saved=[];
  await run({items:[0,1,2],concurrency:1,translate:async item=>{assert.equal(saved.length,item);return item;},onSuccess:(item,result)=>saved.push(result)});
  assert.deepEqual(saved,[0,1,2]);
});
test('empty and already paused queues make no requests',async()=>{
  const translate=()=>assert.fail('unexpected request');
  assert.equal((await run({items:[],translate})).completed,0);
  assert.equal((await run({items:[1],shouldStop:()=>true,translate})).paused,true);
});
test('page completion waits for all selected blocks, including out-of-order results',()=>{
  const pages=[{number:1,blocks:[{id:'a'},{id:'b'}]},{number:2,blocks:[{id:'c'},{id:'d'}]}],complete=[];
  const tracker=createPageTracker(pages,[{id:'a'},{id:'b'},{id:'c'},{id:'d'}],page=>complete.push(page));
  tracker.mark('b');tracker.mark('d');assert.deepEqual(complete,[]);
  tracker.mark('c');assert.deepEqual(complete,[2]);
  tracker.mark('a');tracker.mark('a');tracker.mark('unknown');assert.deepEqual(complete,[2,1]);
});
test('resumed page completion ignores already translated blocks',()=>{
  const complete=[];
  const tracker=createPageTracker([{number:1,blocks:[{id:'saved'},{id:'pending'}]},{number:2,blocks:[]}],[{id:'pending'}],page=>complete.push(page));
  tracker.mark('pending');assert.deepEqual(complete,[1]);
});
test('multiple page updates coalesce without overlapping layout jobs',async()=>{
  const gates=[];let active=0,peak=0,calls=0;
  const queue=createCoalescedTask(()=>new Promise(resolve=>{
    calls++;active++;peak=Math.max(peak,active);
    gates.push(()=>{active--;resolve();});
  }));
  const first=queue.request();await tick();
  queue.request();queue.request();queue.request();assert.equal(calls,1);
  gates[0]();await tick();assert.equal(calls,2);
  gates[1]();await first;assert.equal(peak,1);
  const next=queue.request();await tick();gates[2]();await next;assert.equal(calls,3);
});
test('batch groups remain within pages, skip saved blocks and respect text limits',()=>{
  const pages=[{number:1,blocks:[{id:'a',text:'aaa'},{id:'b',text:'bbb'},{id:'saved',text:'x'},{id:'c',text:'cccccc'}]},
    {number:2,blocks:[{id:'d',text:'d'}]}];
  const pending=pages.flatMap(page=>page.blocks).filter(b=>b.id!=='saved');
  assert.deepEqual(groupByPage(pages,pending,3,6).map(g=>g.blocks.map(b=>b.id)),[['a','b'],['c'],['d']]);
  assert.deepEqual(groupByPage(pages,pending,1).map(g=>g.blocks.length),[1,1,1,1]);
});
test('very long paragraphs run alone so short paragraphs are not held back',()=>{
  const pages=[{number:1,blocks:[
    {id:'a',text:'a'.repeat(10)},
    {id:'long',text:'x'.repeat(1500)},
    {id:'b',text:'b'.repeat(10)},
    {id:'c',text:'c'.repeat(10)}]}];
  const pending=pages[0].blocks;
  assert.deepEqual(groupByPage(pages,pending,5,6000).map(g=>g.blocks.map(b=>b.id)),
    [['a'],['long'],['b','c']]);
});
test('concurrency can reach twelve requests at once',async()=>{
  let release;
  const gate=new Promise(resolve=>{release=resolve;});
  const started=[];
  const items=Array.from({length:12},(_,index)=>index);
  const job=run({items,concurrency:20,translate:async item=>{started.push(item);await gate;return item;}});
  await tick();
  assert.equal(started.length,12);
  release();
  assert.equal((await job).completed,12);
});
test('temporary errors retry with bounded backoff and then succeed',async()=>{
  let calls=0;const waits=[],notices=[];
  const result=await withRetry(async()=>{if(++calls<3)throw Object.assign(new Error('HTTP 502'),{retryable:true});return 'ok';},
    {wait:async ms=>waits.push(ms),onRetry:(error,n)=>notices.push(n)});
  assert.equal(result,'ok');assert.equal(calls,3);assert.deepEqual(waits,[1000,2500]);assert.deepEqual(notices,[1,2]);
});
test('retries stop at the limit, and permanent errors are never retried',async()=>{
  for(const retryable of [true,false]){
    let calls=0;
    await assert.rejects(withRetry(async()=>{calls++;throw Object.assign(new Error('failure'),{retryable});},{wait:async()=>{}}),/failure/);
    assert.equal(calls,retryable?3:1);
  }
});
test('pause during backoff prevents another request',async()=>{
  let stop=false,calls=0;
  await assert.rejects(withRetry(async()=>{calls++;throw Object.assign(new Error('502'),{retryable:true});},
    {shouldStop:()=>stop,wait:async()=>{stop=true;}}),error=>error.cancelled===true);
  assert.equal(calls,1);
});
test('batch format failure falls back to singles and saves each result immediately',async()=>{
  const saved=[],calls=[];let fallback=0;
  await translateGroup({blocks:[{id:'a'},{id:'b'}],
    translateBatch:async()=>{throw Object.assign(new Error('format'),{fallback:'single'});},
    translateOne:async block=>{calls.push(block.id);assert.equal(saved.length,calls.length-1);return {...block,translation:'译文'};},
    onFallback:()=>fallback++,onBlock:block=>saved.push(block.id)});
  assert.deepEqual(saved,['a','b']);assert.equal(fallback,1);
});
test('invalid duplicate or missing batch IDs are rejected before accepting results',async()=>{
  for(const output of [[{id:'a',translation:'甲'}],[{id:'a',translation:'甲'},{id:'a',translation:'甲'}]]){
    const saved=[];
    await translateGroup({blocks:[{id:'a'},{id:'b'}],translateBatch:async()=>({blocks:output}),
      translateOne:async b=>({...b,translation:'单段'}),onBlock:b=>saved.push(b.translation)});
    assert.deepEqual(saved,['单段','单段']);
  }
});
test('a later single failure preserves earlier fallback results',async()=>{
  const saved=[];
  await assert.rejects(translateGroup({blocks:[{id:'a'},{id:'b'}],canBatch:()=>false,
    translateOne:async b=>{if(b.id==='b')throw new Error('502');return {...b,translation:'甲'};},
    onBlock:b=>saved.push(b.id)}),/502/);
  assert.deepEqual(saved,['a']);
});
test('authentication failures do not trigger fallback or extra requests',async()=>{
  await assert.rejects(translateGroup({blocks:[{id:'a'},{id:'b'}],
    translateBatch:async()=>{throw new Error('认证失败');},translateOne:()=>assert.fail('unexpected request'),
    onFallback:()=>assert.fail('unexpected fallback'),onBlock:()=>{}}),/认证失败/);
});
