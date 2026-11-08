'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const Q=require('../static/translation-queue.js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('auto batching respects references, page boundaries, continuations and character budgets',()=>{
  const blocks=Array.from({length:15},(_,i)=>({id:String(i),text:'word '.repeat(10)}));
  blocks[6].reference={};blocks[7].reference={};
  const groups=Q.groupByPage([{number:1,blocks}],blocks,'auto',12000,4000,{'4':'3'});
  assert.deepEqual(groups.map(g=>g.blocks.map(b=>b.id)),[['0','1','2','3'],['4','5'],['6','7'],['8','9','10','11','12','13','14']]);
  const long=Array.from({length:14},(_,i)=>({id:String(i),text:'x'.repeat(2000)}));
  assert.ok(Q.groupByPage([{blocks:long}],long,'auto').every(g=>g.blocks.length<=3));
});

test('auto batching isolates output-heavy prose while merging short labels',()=>{
  const blocks=[{id:'long',text:'x'.repeat(1300)},...Array.from({length:12},(_,i)=>({id:String(i),text:'label'}))];
  assert.deepEqual(Q.groupByPage([{blocks}],blocks,'auto').map(g=>g.blocks.length),[1,12]);
});

test('dependencies wait for saved results while independent groups proceed',async()=>{
  const complete=new Set(),started=[],gates={};
  const job=Q.run({items:['a','b','c'],concurrency:3,ready:x=>x!=='b'||complete.has('a'),
    translate:x=>{started.push(x);return new Promise(resolve=>gates[x]=resolve);},onSuccess:x=>complete.add(x)});
  assert.deepEqual(started,['a','c']);gates.c();await tick();assert.deepEqual(started,['a','c']);
  gates.a();await tick();assert.deepEqual(started,['a','c','b']);gates.b();assert.equal((await job).completed,3);
});

test('failed dependencies report blocked work without sending it',async()=>{
  const errors=[];
  const result=await Q.run({items:['a','b'],ready:x=>x==='a',translate:async()=>{throw new Error('failed');},onError:(x,e)=>errors.push([x,!!e.dependency])});
  assert.deepEqual(errors,[['a',false],['b',true]]);assert.equal(result.failed,2);
});

test('adaptive concurrency grows after clean requests and backs off on upstream limits and timeouts',()=>{
  const control=Q.createAdaptiveConcurrency();
  for(let i=0;i<11;i++)control.success();assert.equal(control.limit(),3);
  control.success();assert.equal(control.limit(),4);
  for(let i=0;i<36;i++)control.success();assert.equal(control.limit(),6);
  control.failure({status:502,agentStatus:429});assert.equal(control.limit(),3);
  control.failure({status:504});assert.equal(control.limit(),3);
  control.failure({status:504});assert.equal(control.limit(),1);
  for(let i=0;i<11;i++)control.success();control.success(false);control.success();assert.equal(control.limit(),1);
});

test('rate limit hints accept dates and are never shortened to sixty seconds',async()=>{
  assert.equal(Q.retryDelay('120'),120000);
  assert.equal(Q.retryDelay('Thu, 01 Jan 1970 00:02:00 GMT',0),120000);
  const waits=[];let calls=0;
  await Q.withRetry(async()=>{if(!calls++)throw {retryable:true,retryAfter:'120'};},{wait:async delay=>waits.push(delay)});
  assert.deepEqual(waits,[120000]);
});

test('partial batches retry only missing IDs in progressively smaller groups',async()=>{
  const blocks=Array.from({length:12},(_,i)=>({id:String(i)})),calls=[],saved=[];
  await Q.translateGroup({blocks,translateBatch:async selected=>{
    calls.push(selected.map(b=>b.id));
    if(calls.length===1)return {blocks:selected.slice(0,3).map(b=>({...b,translation:'valid'}))};
    if(selected.length>3)throw {fallback:'single'};
    return {blocks:selected.map(b=>({...b,translation:'valid'}))};
  },translateOne:async b=>({...b,translation:'valid'}),onBlock:b=>saved.push(b.id)});
  assert.equal(new Set(saved).size,12);assert.equal(saved.length,12);
  assert.ok(calls.slice(1).every(ids=>!ids.includes('0')&&!ids.includes('1')&&!ids.includes('2')));
  assert.ok(calls.slice(1).every(ids=>ids.length<=6));
});

test('prewarm coalesces pending pages and recovers after an error',async()=>{
  let finish;const calls=[],errors=[];
  const warm=Q.createPagePrewarmer(async pages=>{calls.push(pages);if(calls.length===1){await new Promise(resolve=>finish=resolve);throw new Error('failed');}},error=>errors.push(error));
  warm.add(1);await tick();warm.add(2);warm.add(3);warm.add(2);finish();await warm.drain();
  assert.deepEqual(calls,[[1],[2,3]]);assert.equal(errors.length,1);
});

test('dynamic limit reduction drains in-flight requests before scheduling replacements',async()=>{
  let limit=3;const started=[],gates={};
  const job=Q.run({items:[0,1,2,3],concurrency:()=>limit,translate:i=>{started.push(i);return new Promise(r=>gates[i]=r);}});
  limit=1;gates[0]();await tick();gates[1]();await tick();assert.deepEqual(started,[0,1,2]);
  gates[2]();await tick();assert.deepEqual(started,[0,1,2,3]);gates[3]();await job;
});
