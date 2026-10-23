'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const TranslationQueue=require('../static/translation-queue.js');

function reader(){
  const elements=new Map(),timers=[];
  const classes=()=>{const set=new Set();return {add:x=>set.add(x),remove:x=>set.delete(x),
    contains:x=>set.has(x),toggle:(x,on)=>on?set.add(x):set.delete(x)};};
  const sizes=[{width:600,height:800},{width:800,height:500},{width:420,height:700}];
  function pane(left,width){
    let scrollTop=0,scrollLeft=0;
    const p={clientWidth:width,clientHeight:460,classList:classes(),capture:null,
      getBoundingClientRect:()=>({left,top:90,bottom:550}),
      setPointerCapture(id){this.capture=id;},hasPointerCapture(id){return this.capture===id;},releasePointerCapture(){this.capture=null;}};
    const pages=sizes.map((size,i)=>({style:{width: `${size.width}px`},dataset:{pageIndex:String(i)},
      classList:{contains:name=>name==='pdf-page'},getBoundingClientRect(){
        const w=parseFloat(this.style.width),h=w*size.height/size.width;
        const top=106+pages.slice(0,i).reduce((sum,page,j)=>sum+parseFloat(page.style.width)*sizes[j].height/sizes[j].width+16,0)-p.scrollTop;
        return {left:left+18+Math.max(0,(p.clientWidth-36-w)/2)-p.scrollLeft,top,width:w,height:h,bottom:top+h};
      }}));
    p.pages=pages;p.querySelectorAll=selector=>selector==='.reader-page'?pages:[];p.querySelector=()=>null;
    Object.defineProperties(p,{
      scrollWidth:{get:()=>Math.max(p.clientWidth,Math.max(...pages.map(page=>parseFloat(page.style.width)))+36)},
      scrollHeight:{get:()=>pages.reduce((sum,page,i)=>sum+parseFloat(page.style.width)*sizes[i].height/sizes[i].width+16,44)},
      scrollTop:{get:()=>scrollTop,set:value=>{scrollTop=Math.max(0,Math.min(value,p.scrollHeight-p.clientHeight));}},
      scrollLeft:{get:()=>scrollLeft,set:value=>{scrollLeft=Math.max(0,Math.min(value,p.scrollWidth-p.clientWidth));}}
    });
    return p;
  }
  const source=pane(0,500),translation=pane(510,520);
  elements.set('#source-pane',source);elements.set('#translation-pane',translation);
  elements.set('#sync-scroll',{checked:true});elements.set('#reader',{classList:classes()});
  for(const id of ['zoom-in','zoom-out','zoom-reset','zoom-fit','zoom-hint'])elements.set('#'+id,{setAttribute(){}});
  const context=vm.createContext({TranslationQueue,localStorage:{getItem:()=>null},
    document:{querySelector:q=>elements.get(q),querySelectorAll:()=>[]},
    getComputedStyle:()=>({paddingLeft:'18px',paddingRight:'18px'}),
    setTimeout:fn=>timers.push(fn),clearTimeout(){},window:{devicePixelRatio:1}});
  const code=fs.readFileSync(require.resolve('../static/app.js'),'utf8').split("$('#settings-button').onclick")[0];
  vm.runInContext(code+`\nstate.doc={id:'mixed',pages:${JSON.stringify(sizes.map((s,i)=>({...s,number:i+1,blocks:[]})))}};applyViewportGeometry();`,context);
  return {source,translation,elements,eval:s=>vm.runInContext(s,context)};
}

test('fit uses one physical scale for both panes and mixed portrait/landscape pages',()=>{
  const r=reader();
  for(let i=0;i<3;i++)assert.equal(r.source.pages[i].style.width,r.translation.pages[i].style.width);
  assert.equal(r.source.pages[1].getBoundingClientRect().width,464);
  assert.ok(Math.abs(r.source.pages[0].getBoundingClientRect().width-348)<.001);
  r.eval('setReaderZoom(2)');
  assert.equal(r.source.pages[0].getBoundingClientRect().width,1600);
  assert.equal(r.translation.pages[0].getBoundingClientRect().width,1600);
  assert.ok(r.source.classList.contains('can-pan'));
});

test('zoom retains the point under the cursor on a later page and both horizontal positions',()=>{
  const r=reader();r.eval('setReaderZoom(1)');
  r.source.scrollTop=1200;r.translation.scrollTop=1230;
  r.source.scrollLeft=100;r.translation.scrollLeft=100;
  const point={x:760,y:280},box=r.translation.pages[1].getBoundingClientRect();
  const x=(point.x-box.left)/box.width,y=(point.y-box.top)/box.height;
  r.eval(`setReaderZoom(2,{side:'translation',clientX:${point.x},clientY:${point.y}})`);
  const after=r.translation.pages[1].getBoundingClientRect();
  assert.ok(Math.abs(after.left+x*after.width-point.x)<.001);
  assert.ok(Math.abs(after.top+y*after.height-point.y)<.001);
  assert.ok(r.source.scrollLeft>100);
  assert.ok(r.translation.scrollLeft>100);
  r.eval("synchronize('source');synchronize('translation')");
  assert.ok(Math.abs(r.translation.pages[1].getBoundingClientRect().top-after.top)<.001);
});

test('Ctrl wheel is intercepted locally; normal wheel and paragraph editing are untouched',()=>{
  const r=reader();const initial=r.eval('readerViewport.zoom');
  r.eval("let prevented=0;readerWheel({ctrlKey:false,deltaY:-100,preventDefault(){prevented++}},'source')");
  assert.equal(r.eval('readerViewport.zoom'),initial);assert.equal(r.eval('prevented'),0);
  r.eval("readerWheel({ctrlKey:true,deltaY:-3,deltaMode:1,clientX:200,clientY:200,preventDefault(){prevented++}},'source')");
  assert.ok(r.eval('readerViewport.zoom')>initial);assert.equal(r.eval('prevented'),1);
  r.eval("state.view='paragraph';readerWheel({ctrlKey:true,deltaY:-100,preventDefault(){prevented++}},'source')");
  assert.equal(r.eval('prevented'),1);
});

test('zoom bounds, reset and fit stay valid across narrow windows and new documents',()=>{
  const r=reader();r.eval('setReaderZoom(100)');assert.equal(r.eval('readerViewport.zoom'),6);
  r.eval('setReaderZoom(-1)');assert.equal(r.eval('readerViewport.zoom'),.1);
  r.eval('setReaderZoom(NaN)');assert.equal(r.eval('readerViewport.zoom'),.1);
  r.eval('setReaderZoom(1)');assert.equal(r.eval('readerViewport.zoom'),1);
  r.source.clientWidth=320;r.translation.clientWidth=360;
  r.eval('refreshReaderViewport()');assert.equal(r.eval('readerViewport.zoom'),1);
  r.eval('setReaderZoom(1,{fit:true})');assert.ok(r.source.scrollWidth<=320);
  r.eval("state.doc.id='different';setReaderZoom(3)");assert.equal(r.eval('readerViewport.fit'),true);
});

test('horizontal drag follows both panes without vertical jumps or accidental editing',()=>{
  const r=reader();r.eval('setReaderZoom(2)');
  r.source.scrollTop=100;r.translation.scrollTop=150;
  r.source.scrollLeft=0;r.translation.scrollLeft=0;
  r.eval(`let event={pointerId:1,button:0,pointerType:'mouse',clientX:300,clientY:200,
    target:{closest:()=>true},preventDefault(){}};
    startReaderPan(event,'source');moveReaderPan({...event,clientX:297});`);
  assert.equal(r.source.scrollLeft,0);assert.equal(r.eval('readerViewport.pan.moved'),false);
  r.eval('moveReaderPan({...event,clientX:160});endReaderPan(event)');
  assert.equal(r.source.scrollLeft,140);
  assert.ok(r.translation.scrollLeft>0);assert.equal(r.translation.scrollTop,150);
  assert.equal(r.source.capture,null);
  r.eval("let blocked=0;suppressPanClick({detail:1,preventDefault(){blocked++},stopImmediatePropagation(){}},'source')");
  assert.equal(r.eval('blocked'),1);
  r.eval("startReaderPan(event,'source');endReaderPan(event);suppressPanClick({detail:1,preventDefault(){blocked++},stopImmediatePropagation(){}},'source')");
  assert.equal(r.eval('blocked'),1);
});

test('sync-off affects panning only; zoom still applies to both panes',()=>{
  const r=reader();r.elements.get('#sync-scroll').checked=false;r.eval('setReaderZoom(2)');
  r.translation.scrollLeft=0;
  r.eval(`let e={pointerId:2,button:0,pointerType:'mouse',clientX:350,clientY:200,
    target:{closest:()=>true},preventDefault(){}};startReaderPan(e,'source');moveReaderPan({...e,clientX:250});endReaderPan(e);`);
  assert.equal(r.translation.scrollLeft,0);
  assert.equal(r.source.pages[0].style.width,r.translation.pages[0].style.width);
});

test('Shift selection, touch scrolling and small click motions never start a drag',()=>{
  const r=reader();r.eval('setReaderZoom(2)');
  for(const extra of ['shiftKey:true','pointerType:"touch"','button:2']){
    r.eval(`startReaderPan({button:0,pointerId:4,clientX:100,clientY:200,target:{closest:()=>true},${extra}},'source')`);
    assert.equal(r.eval('readerViewport.pan'),null);
  }
});
