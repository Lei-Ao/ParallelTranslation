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
  const doc={id:'test',pages:[{number:1,blocks:[
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
  const frames=[node('b',source,400),node('b',translation,250)];
  for(const frame of frames){delete frame.dataset.block;frame.dataset.blockGroup='b';}
  for(const [owner,map] of [[source,originals],[translation,translations]]){
    const page={getBoundingClientRect:()=>({top:10-owner.scrollTop,height:1000})};
    owner.querySelectorAll=selector=>selector==='.reader-page'?[page]:[];
    owner.querySelector=selector=>map.get(selector.match(/data-block="([^"]+)"/)?.[1]) || null;
  }
  const context=vm.createContext({TranslationQueue,localStorage:{getItem:()=>null},
    document:{querySelector:selector=>elements.get(selector),querySelectorAll:selector=>
      selector==='[data-block-group]'?frames:selector==='[data-block]'?nodes.filter(node=>node.dataset.block):[]}});
  const code=fs.readFileSync(require.resolve('../static/app.js'),'utf8').split("$('#settings-button').onclick")[0];
  vm.runInContext(code+`\nstate.doc=${JSON.stringify(doc)};function updateControls(){}`,context);
  return {source,translation,elements,originals,translations,frames,eval:code=>vm.runInContext(code,context)};
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
test('hovering any merged source fragment highlights every member on both sides',()=>{
  const r=reader();
  r.eval("state.layout={block_groups:[{page:1,blocks:['b','c']},{page:2,blocks:['a','right']}]};hoverBlock('c')");
  for(const map of [r.originals,r.translations]){
    assert.ok(map.get('b').classes.has('linked-hover'));
    assert.ok(map.get('c').classes.has('linked-hover'));
    assert.ok(!map.get('a').classes.has('linked-hover'));
    assert.ok(!map.get('right').classes.has('linked-hover'));
  }
  r.eval("hoverBlock('b');selectBlock('c','translation')");
  for(const frame of r.frames){
    assert.ok(frame.classes.has('linked-hover'));
    assert.ok(frame.classes.has('selected'));
  }
  for(const map of [r.originals,r.translations]){
    assert.ok(map.get('b').classes.has('selected'));
    assert.ok(map.get('c').classes.has('selected'));
    assert.ok(!map.get('right').classes.has('selected'));
  }
  r.eval('hoverBlock(null);clearBlockSelection()');
  for(const frame of r.frames){
    assert.ok(!frame.classes.has('linked-hover'));
    assert.ok(!frame.classes.has('selected'));
  }
  for(const map of [r.originals,r.translations])for(const node of map.values()){
    assert.ok(!node.classes.has('linked-hover'));
    assert.ok(!node.classes.has('selected'));
  }
});
test('a multi-fragment equation paints one union frame per side without stacking',()=>{
  const r=reader();
  r.eval(`state.doc.pages[0].blocks=[
    {id:'a',bbox:[.1,.2,.3,.24]},
    {id:'b',bbox:[.15,.24,.4,.3]},
    {id:'c',bbox:[.45,.25,.49,.27]},
    {id:'right',bbox:[.55,.2,.9,.3]}];
    state.layout={block_groups:[{page:1,blocks:['a','b','c']}],
      placements:{a:[.12,.3,.5,.4],b:[.12,.3,.5,.4],c:[.12,.3,.5,.4]}};`);
  function bounds(side){
    const html=r.eval(`groupHighlightHtml(state.doc.pages[0],'${side}')`);
    assert.equal((html.match(/data-block-group=/g)||[]).length,1,html);
    assert.match(html,/data-block-group="a"/);
    assert.match(html,/aria-hidden="true"/);
    return Object.fromEntries([...html.matchAll(/(left|top|width|height):([\d.e+-]+)%/g)].map(match=>[match[1],Number(match[2])]));
  }
  for(const [side,expected] of [['source',[10,20,39,10]],['translation',[12,30,38,10]]]){
    const box=bounds(side);
    Object.values(box).forEach((value,i)=>assert.ok(Math.abs(value-expected[i])<1e-8));
  }
  // The same normalized-coordinate rule covers a 90-degree displayed page.
  r.eval(`state.doc={...state.doc,pages:[{number:1,blocks:[
    {id:'a',bbox:[.7,.1,.8,.3]},{id:'b',bbox:[.65,.3,.78,.45]}]}]};
    state.layout={block_groups:[{page:1,blocks:['a','b']}]};`);
  const box=bounds('source');
  assert.ok(Math.abs(box.left-65)<1e-8 && Math.abs(box.width-15)<1e-8);
  r.eval("state.view='paragraph'");
  assert.equal(r.eval("groupHighlightHtml(state.doc.pages[0],'source')"),'');
});
test('older placement reports keep merged hover without changing independent blocks',()=>{
  const r=reader();
  r.eval("state.layout={placements:{b:[.1,.2,.5,.4],c:[.1,.2,.5,.4],right:[.6,.2,.9,.4]}};hoverBlock('c')");
  assert.ok(r.originals.get('b').classes.has('linked-hover'));
  assert.ok(!r.originals.get('right').classes.has('linked-hover'));
  r.eval("state.layout={block_groups:[],placements:{}};hoverBlock('c')");
  assert.ok(!r.originals.get('b').classes.has('linked-hover'));
  r.eval("state.view='paragraph';state.layout={block_groups:[{page:1,blocks:['b','c']}]};hoverBlock('c')");
  assert.ok(!r.originals.get('b').classes.has('linked-hover'));
  assert.ok(r.translations.get('c').classes.has('linked-hover'));
});
test('sync can be disabled, and horizontal scrolling uses each pane range',()=>{
  const r=reader();r.elements.get('#sync-scroll').checked=false;
  r.source.scrollTop=320;r.eval("synchronize('source')");assert.equal(r.translation.scrollTop,0);
  r.elements.get('#sync-scroll').checked=true;
  r.source.scrollWidth=1000;r.translation.scrollWidth=1500;r.source.scrollLeft=100;
  r.eval("synchronize('source')");assert.equal(r.translation.scrollLeft,200);
});
test('source hit regions follow a detached equation label while paragraph view keeps saved bounds',()=>{
  const r=reader();
  r.eval("state.layout={source_placements:{b:[.1,.3,.5,.6]}};");
  assert.equal(r.eval("JSON.stringify(sourceBlockBounds(state.doc.pages[0].blocks[1]))"),'[0.1,0.3,0.5,0.6]');
  assert.equal(r.eval("JSON.stringify(state.doc.pages[0].blocks[1].bbox)"),'[0.08,0.4,0.48,0.6]');
  r.eval("state.view='paragraph'");
  assert.equal(r.eval("JSON.stringify(sourceBlockBounds(state.doc.pages[0].blocks[1]))"),'[0.08,0.4,0.48,0.6]');
});
