'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const TranslationQueue=require('../static/translation-queue.js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function reader(){
  const requests=[];
  const context=vm.createContext({TranslationQueue,localStorage:{getItem:()=>null,setItem:()=>{}},document:{},
    api:(path,options)=>new Promise(resolve=>requests.push({path,options,resolve}))});
  const source=fs.readFileSync(require.resolve('../static/app.js'),'utf8').split('function showEdit(')[0];
  vm.runInContext(source+`
    function blocks(){return state.doc.pages.flatMap(page=>page.blocks);}
    function updateLayoutControls(){}
    function renderTranslations(){}
    state.doc={id:'first',pages:[{number:1,blocks:[{id:'a',translation:'first',target:state.target}]}]};
  `,context);
  return {requests,eval:code=>vm.runInContext(code,context)};
}
test('a completed PDF snapshot remains visible while later blocks translate',async()=>{
  const r=reader(),job=r.eval('refreshLayout()');await tick();
  r.eval("state.doc.pages[0].blocks[0].translation='new';invalidateLayout(true)");
  r.requests[0].resolve({revision:'snapshot'});await job;
  assert.equal(r.eval('state.layout.revision'),'snapshot');
  assert.equal(r.eval('state.layoutKey===layoutKey()'),false);
  r.eval('invalidateLayout(true)');assert.equal(r.eval('state.layout.revision'),'snapshot');
});
test('old document/layout results cannot overwrite a newly opened document',async()=>{
  const r=reader(),job=r.eval('refreshLayout()');await tick();
  r.eval("state.doc.id='second';invalidateLayout();refreshLayout()");
  r.requests[0].resolve({revision:'old'});await tick();
  assert.equal(r.eval('state.layout'),null);
  assert.equal(r.requests.length,2);assert.match(r.requests[1].path,/second/);
  r.requests[1].resolve({revision:'new'});await job;
  assert.equal(r.eval('state.layout.revision'),'new');
});
test('queued page update publishes the latest snapshot after an older one',async()=>{
  const r=reader(),job=r.eval('refreshLayout()');await tick();
  r.eval("state.doc.pages[0].blocks[0].translation='new';invalidateLayout(true);refreshLayout();refreshLayout()");
  r.requests[0].resolve({revision:'old'});await tick();
  assert.equal(r.eval('state.layout.revision'),'old');assert.equal(r.requests.length,2);
  r.requests[1].resolve({revision:'latest'});await job;
  assert.equal(r.eval('state.layout.revision'),'latest');assert.equal(r.eval('state.layoutKey===layoutKey()'),true);
});

test('final timing includes the tail after translation and ignores unrelated documents',async()=>{
  const r=reader();
  r.eval('state.performance={started:Date.now()-1000,translation_ms:500,translation_finished:Date.now()-500,characters:50};state.performanceDocId="first"');
  const job=r.eval('refreshLayout()');await tick();r.requests[0].resolve({revision:'final'});await job;
  assert.ok(r.eval('state.performance.total_ms')>=1000);
  assert.ok(r.eval('state.performance.layout_tail_ms')>=500);
  r.eval('state.doc.id="second";invalidateLayout();state.performance.total_ms=123');
  const other=r.eval('refreshLayout()');await tick();r.requests[1].resolve({revision:'other'});await other;
  assert.equal(r.eval('state.performance.total_ms'),123);
});

test('a server-reported stale snapshot is never marked current',async()=>{
  const r=reader(),job=r.eval('refreshLayout()');await tick();
  r.requests[0].resolve({revision:'older',stale:true});await job;
  assert.equal(r.eval('state.layoutKey===layoutKey()'),false);
  assert.match(r.eval('state.layoutError'),/文档已更新/);
});
