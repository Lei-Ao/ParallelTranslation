'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const TranslationQueue=require('../static/translation-queue.js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function workspace({saved=false,empty=false}={}){
  const elements=new Map(),requests=[],opened=[];
  function element(selector){
    if(!elements.has(selector)){
      const classes=new Set();
      elements.set(selector,{style:{},value:'',classList:{
        add:(...names)=>names.forEach(n=>classes.add(n)),remove:(...names)=>names.forEach(n=>classes.delete(n)),
        toggle:(name,force)=>{if(force)classes.add(name);else classes.delete(name);},contains:name=>classes.has(name)
      },removeAttribute(){},showModal(){},scrollTop:0});
    }
    return elements.get(selector);
  }
  const doc={id:'demo',name:'demo.pdf',pages:[{number:1,blocks:empty?[]:[
    {id:'a',text:'A',translation:saved?'甲':'',target:'简体中文'},
    {id:'b',text:'B',translation:saved?'乙':'',target:'简体中文'}]}]};
  const context=vm.createContext({TranslationQueue,localStorage:{getItem:()=>null},
    document:{querySelector:element,querySelectorAll:()=>[],body:element('body')},
    FormData:class{append(){}},setTimeout:()=>0,clearTimeout(){},
    request:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
    opened:ready=>opened.push(ready)});
  const source=fs.readFileSync(require.resolve('../static/app.js'),'utf8').split("$('#settings-button').onclick")[0];
  vm.runInContext(source+`
    api=(path,options)=>request(path,options);
    renderDocument=ready=>opened(ready);
    showSettings=()=>{};
    state.agent.model='mock';state.concurrency=1;
    state.doc=${JSON.stringify(doc)};
    state.pendingOpen=true;
  `,context);
  return {requests,opened,element,eval:code=>vm.runInContext(code,context)};
}
async function finishBlock(r,index,id){
  r.requests[index].resolve({id,translation:'译文',target:'简体中文'});await tick();
}
test('import stays in the library through translation and opens only after final layout',async()=>{
  const r=workspace();
  const job=r.eval('uploadFile({size:100})');
  assert.equal(r.opened.length,0);
  r.requests[0].resolve(r.eval('state.doc'));await tick();
  assert.equal(r.requests[1].path,'/api/documents');r.requests[1].resolve([]);await tick();
  await finishBlock(r,2,'a');
  assert.equal(r.element('#open-percent').textContent,'50%');
  assert.equal(r.opened.length,0);
  await finishBlock(r,3,'b');
  assert.equal(r.requests[4].path,'/api/documents');r.requests[4].resolve([]);await tick();
  assert.match(r.requests[5].path,/\/layout$/);
  assert.equal(r.element('#open-percent').textContent,'100%');
  assert.match(r.element('#open-status').textContent,/正在排版/);
  assert.equal(r.opened.length,0);
  r.requests[5].resolve({revision:'ready',placements:{},issues:[]});await job;
  assert.deepEqual(r.opened,[true]);assert.equal(r.eval('state.pendingOpen'),false);
});
test('pause drains work, stays on the progress card and resumes only remaining blocks',async()=>{
  const r=workspace(),job=r.eval('prepareDocument()');
  r.eval('state.stop=true');await finishBlock(r,0,'a');
  assert.equal(r.requests[1].path,'/api/documents');r.requests[1].resolve([]);await job;
  assert.equal(r.opened.length,0);assert.match(r.element('#open-status').textContent,/已暂停/);
  const resumed=r.eval('prepareDocument()');
  assert.match(r.requests[2].path,/blocks\/b\/translate$/);await finishBlock(r,2,'b');
  r.requests[3].resolve([]);await tick();r.requests[4].resolve({revision:'ready',issues:[]});await resumed;
  assert.deepEqual(r.opened,[true]);
});
test('translation failure retains progress and never opens the reader',async()=>{
  const r=workspace(),job=r.eval('prepareDocument()');
  r.requests[0].reject(new Error('rate limit'));await tick();r.requests[1].resolve([]);await job;
  assert.equal(r.opened.length,0);assert.match(r.element('#open-status').textContent,/翻译失败：rate limit/);
  assert.equal(r.eval('state.preparing || state.running'),false);
});
test('batch fallback updates progress per single result and opens after completion',async()=>{
  const r=workspace();r.eval('state.batchSupported=true;state.batchSize=3');
  const job=r.eval('prepareDocument()');
  assert.match(r.requests[0].path,/translate-batch$/);
  r.requests[0].reject(Object.assign(new Error('合并译文格式不完整'),{fallback:'single'}));await tick();
  assert.match(r.requests[1].path,/blocks\/a\/translate$/);
  await finishBlock(r,1,'a');assert.equal(r.element('#open-percent').textContent,'50%');
  assert.equal(r.eval('state.batchSize'),1);assert.equal(r.opened.length,0);
  await finishBlock(r,2,'b');r.requests[3].resolve([]);await tick();
  r.requests[4].resolve({revision:'ready',issues:[]});await job;
  assert.deepEqual(r.opened,[true]);
});
test('saved translations skip agent calls and layout failure can be retried',async()=>{
  const r=workspace({saved:true}),job=r.eval('prepareDocument()');await tick();
  assert.match(r.requests[0].path,/\/layout$/);
  r.requests[0].reject(new Error('layout unavailable'));await job;
  assert.equal(r.opened.length,0);assert.match(r.element('#open-status').textContent,/layout unavailable/);
  const retry=r.eval('prepareDocument()');await tick();
  r.requests[1].resolve({revision:'ready',issues:[]});await retry;assert.deepEqual(r.opened,[true]);
});
test('opening an existing document waits for layout and reuses saved translations',async()=>{
  const r=workspace({saved:true}),job=r.eval("openDocument('demo')");
  assert.equal(r.requests[0].path,'/api/documents/demo');
  assert.equal(r.opened.length,0);
  r.requests[0].resolve(r.eval('state.doc'));await tick();
  assert.equal(r.opened.length,0);assert.match(r.requests[1].path,/\/layout$/);
  r.requests[1].resolve({revision:'saved',issues:[]});await job;
  assert.deepEqual(r.opened,[true]);assert.equal(r.requests.length,2);
});
test('translations in another language do not count toward completion',async()=>{
  const r=workspace({saved:true});r.eval("state.target='English';state.agent.model=''");
  await r.eval('prepareDocument()');
  assert.equal(r.element('#open-percent').textContent,'0%');
  assert.equal(r.opened.length,0);assert.equal(r.requests.length,0);
});
test('missing configuration and image-only documents stay in the library',async()=>{
  const r=workspace();r.eval("state.agent.model=''");await r.eval('prepareDocument()');
  assert.equal(r.requests.length,0);assert.match(r.element('#open-status').textContent,/配置 Agent/);
  const scan=workspace({empty:true});await scan.eval('prepareDocument()');
  assert.equal(scan.requests.length,0);assert.match(scan.element('#open-status').textContent,/OCR/);
  assert.equal(scan.element('#open-action').disabled,true);
});
