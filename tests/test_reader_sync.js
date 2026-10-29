'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const TranslationQueue=require('../static/translation-queue.js');

function reader(){
  const elements=new Map(),nodes=[];
  function pane(){return {scrollTop:0,scrollLeft:0,scrollHeight:2400,clientHeight:400,scrollWidth:500,clientWidth:500,
    getBoundingClientRect:()=>({top:10}),querySelectorAll:()=>[],querySelector:()=>null};}
  const source=pane(),translation=pane();
  elements.set('#source-pane',source);elements.set('#translation-pane',translation);
  elements.set('#sync-scroll',{checked:true});elements.set('#page-select',{value:0});
  const doc={id:'test',pages:[{blocks:[
    {id:'a',bbox:[.08,.1,.48,.2]}, {id:'b',bbox:[.08,.4,.48,.6]},
    {id:'c',bbox:[.08,.7,.48,.8]}, {id:'right',bbox:[.52,.4,.92,.6]}]}]};
  function node(id,owner,top,height=80){
    const classes=new Set(),attrs=new Map();
    const el={dataset:{block:id},classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:name=>classes.has(name)},
      getBoundingClientRect:()=>({top:10+top-owner.scrollTop,height}),setAttribute:(key,val)=>attrs.set(key,val),removeAttribute:key=>attrs.delete(key),
      matches:selector=>selector==='.block-overlay',focus(){},classes,attrs};
    nodes.push(el);return el;
  }
  const originals=new Map(),translations=new Map();
  for(const [id,s,t] of [['a',100,100],['b',400,250],['c',700,450],['right',400,330]]){
    originals.set(id,node(id,source,s));translations.set(id,node(id,translation,t));
  }
  for(const [owner,map] of [[source,originals],[translation,translations]]){
    const page={getBoundingClientRect:()=>({top:10-owner.scrollTop,height:1000})};
    owner.querySelectorAll=selector=>selector==='.reader-page'?[page]:[];
    owner.querySelector=selector=>map.get(selector.match(/data-block="([^"]+)"/)?.[1]) || null;
  }
  const context=vm.createContext({TranslationQueue,localStorage:{getItem:()=>null},
    document:{querySelector:selector=>elements.get(selector),querySelectorAll:()=>nodes}});
  const code=fs.readFileSync(require.resolve('../static/app.js'),'utf8').split("$('#settings-button').onclick")[0];
  vm.runInContext(code+`\nstate.doc=${JSON.stringify(doc)};function updateControls(){}`,context);
  return {source,translation,elements,originals,translations,eval:code=>vm.runInContext(code,context)};
}

test('PDF scrolling follows translated paragraph positions in both directions',()=>{
  const r=reader();r.source.scrollTop=320;r.eval("synchronize('source')");
  assert.equal(r.translation.scrollTop,170);
  r.eval("synchronize('translation')");assert.equal(r.source.scrollTop,320);
  r.translation.scrollTop=270;r.eval("synchronize('translation')");
  assert.equal(r.source.scrollTop,470);
});
test('fresh user input is not swallowed by a timed synchronization lock',()=>{
  const r=reader();r.source.scrollTop=320;r.eval("synchronize('source')");
  r.translation.scrollTop=200;r.eval("synchronize('translation')");
  assert.equal(r.source.scrollTop,365);
  r.source.scrollTop=400;r.eval("synchronize('source')");
  assert.ok(r.translation.scrollTop>200);
});
test('the active column supplies matching PDF anchors',()=>{
  const r=reader();r.eval('readingInteraction.lane=.75');
  r.source.scrollTop=320;r.eval("synchronize('source')");assert.equal(r.translation.scrollTop,250);
});
test('selection preserves the clicked paragraph position and mirrors range selection',()=>{
  const r=reader();r.source.scrollTop=300;r.eval("selectBlock('b','source')");
  assert.equal(r.translation.scrollTop,150);
  assert.ok(r.originals.get('b').classes.has('selected'));
  assert.ok(r.translations.get('b').classes.has('selected'));
  r.eval("selectBlock('c','translation',true)");
  assert.equal(r.eval('readingInteraction.selection.size'),2);
  assert.ok(r.originals.get('b').classes.has('selected'));
  assert.ok(r.translations.get('c').classes.has('selected'));
});
test('clicking blank space clears the selection, block clicks keep selecting',()=>{
  const r=reader();r.eval("selectBlock('b','source')");
  r.eval("selectBlock('c','translation',true)");
  assert.equal(r.eval('readingInteraction.selection.size'),2);
  r.eval("readerClick({target:{closest:()=>null}},'source')");
  assert.equal(r.eval('state.selected'),null);
  assert.equal(r.eval('readingInteraction.selection.size'),0);
  assert.ok(!r.originals.get('b').classes.has('selected'));
  assert.ok(!r.translations.get('c').classes.has('selected'));
  r.eval("readerClick({target:{closest:selector=>selector==='[data-block]'?{dataset:{block:'b'}}:null}},'translation')");
  assert.equal(r.eval('state.selected'),'b');
  assert.ok(r.originals.get('b').classes.has('selected'));
  assert.ok(r.translations.get('b').classes.has('selected'));
});
test('hover is shared, and escape clears selection on both sides',()=>{
  const r=reader();r.eval("hoverBlock('b');selectBlock('b','source')");
  assert.ok(r.originals.get('b').classes.has('linked-hover'));
  assert.ok(r.translations.get('b').classes.has('linked-hover'));
  r.eval("readerKeydown({key:'Escape',target:{closest:()=>null}},'source');hoverBlock(null)");
  assert.equal(r.eval('state.selected'),null);
  assert.ok(!r.originals.get('b').classes.has('selected'));
  assert.ok(!r.translations.get('b').classes.has('linked-hover'));
});
test('sync can be disabled, and horizontal scrolling uses each pane range',()=>{
  const r=reader();r.elements.get('#sync-scroll').checked=false;
  r.source.scrollTop=320;r.eval("synchronize('source')");assert.equal(r.translation.scrollTop,0);
  r.elements.get('#sync-scroll').checked=true;
  r.source.scrollWidth=1000;r.translation.scrollWidth=1500;r.source.scrollLeft=100;
  r.eval("synchronize('source')");assert.equal(r.translation.scrollLeft,200);
});
