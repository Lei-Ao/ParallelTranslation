'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const TranslationQueue=require('../static/translation-queue.js');

function reader(){
  const elements=new Map();
  const selection={isCollapsed:true,anchorNode:null,focusNode:null,removeAllRanges(){},addRange(range){this.range=range;}};
  const pane={contains:node=>node?.inPane,scrollTop:70,scrollLeft:40};
  elements.set('#source-pane',pane);elements.set('#translation-pane',pane);
  const document={querySelector:q=>elements.get(q),querySelectorAll:()=>[],
    createRange:()=>({setStart(node,offset){this.start=[node,offset];},setEnd(node,offset){this.end=[node,offset];}})};
  const context=vm.createContext({TranslationQueue,document,Intl,getSelection:()=>selection,localStorage:{getItem:()=>null}});
  vm.runInContext(fs.readFileSync(require.resolve('../static/app.js'),'utf8').split("$('#settings-button').onclick")[0],context);
  return {selection,pane,context,eval:code=>vm.runInContext(code,context)};
}

test('drag selection takes precedence over paragraph jumps in either pane',()=>{
  const r=reader();r.selection.isCollapsed=false;r.selection.anchorNode={inPane:true};
  r.context.event={target:{closest:selector=>selector==='[data-block]'?{dataset:{block:'b'}}:null}};
  r.eval("let selected=0;selectBlock=()=>selected++;readerClick(event,'source');readerClick(event,'translation')");
  assert.equal(r.eval('selected'),0);
  assert.deepEqual([r.pane.scrollLeft,r.pane.scrollTop],[40,70]);
  r.selection.isCollapsed=true;r.eval("readerClick(event,'source')");
  assert.equal(r.eval('selected'),1);
});

test('copying PDF text puts plain text on the clipboard without transparent positioning styles',()=>{
  const r=reader(),values=new Map();let prevented=false;
  r.selection.isCollapsed=false;
  r.selection.anchorNode={inPane:true,parentElement:{closest:()=>({})}};
  r.selection.toString=()=> 'alpha beta\n选择词语';
  r.context.event={clipboardData:{setData:(type,value)=>values.set(type,value)},preventDefault(){prevented=true;}};
  r.eval("copyReaderText(event,'source')");
  assert.equal(values.get('text/plain'),'alpha beta\n选择词语');
  assert.equal(values.has('text/html'),false);assert.equal(prevented,true);
});

test('reader clicks select overlays but never the text underneath',()=>{
  const r=reader();
  r.eval("var selected=0,cleared=0;selectBlock=()=>selected++;clearBlockSelection=()=>cleared++;");
  r.context.event={target:{closest:selector=>selector==='[data-block]'?{dataset:{block:'b'}}:null}};
  r.eval("readerClick(event,'source')");
  assert.equal(r.eval('selected'),1);
  assert.equal(r.eval('cleared'),0);
  r.context.event={target:{closest:selector=>selector==='.pdf-char'?{}:null}};
  r.eval("readerClick(event,'translation')");
  assert.equal(r.eval('selected'),1);
  assert.equal(r.eval('cleared'),1);
});

test('English and CJK word selection uses logical text nodes rather than separate glyph boxes',()=>{
  for(const [text,index,expected] of [['Select alpha beta.',9,'alpha'],['可以自由选择词语',5,'选择']]){
    const r=reader();
    const chars=[...text].map(textContent=>({textContent,firstChild:{textContent}}));
    const row={querySelectorAll:()=>chars};chars.forEach(char=>{char.parentElement=row;});
    r.context.event={target:{closest:()=>chars[index]},preventDefault(){this.prevented=true;}};
    r.eval('selectReaderWord(event)');
    const range=r.selection.range;
    const start=chars.findIndex(c=>c.firstChild===range.start[0])+range.start[1];
    const end=chars.findIndex(c=>c.firstChild===range.end[0])+range.end[1];
    assert.equal(text.slice(start,end),expected);
    assert.equal(r.context.event.prevented,true);
  }
});

test('link hit forwarding preserves links while dragging linked words stays selectable',()=>{
  const r=reader();let clicked=0;
  const link={click:()=>clicked++,getBoundingClientRect:()=>({left:10,right:100,top:20,bottom:40})};
  const page={querySelectorAll:selector=>selector==='.link-overlay'?[link]:[]};
  r.context.event={clientX:30,clientY:30,target:{closest:selector=>selector==='.pdf-text-layer'?{}:selector==='.pdf-page'?page:null}};
  r.eval("readerClick(event,'source')");assert.equal(clicked,1);
  r.selection.isCollapsed=false;r.selection.anchorNode={inPane:true};
  r.eval("readerClick(event,'source')");assert.equal(clicked,1);
});

test('source and translated selection URLs use the corresponding actual PDF revision',()=>{
  const r=reader();
  assert.match(r.eval("textLayerHtml({width:800,height:500},'/api/documents/doc/pages/2.png')"),/data-text-url="\/api\/documents\/doc\/pages\/2\/text"/);
  assert.match(r.eval("textLayerHtml({width:500,height:700},'/api/documents/doc/layout/rev/pages/3.png')"),/data-text-url="\/api\/documents\/doc\/layout\/rev\/pages\/3\/text"/);
});

test('a late text response cannot populate a replaced document or layout layer',async()=>{
  const r=reader();let resolve,calls=0,replaced=false;
  const layer={dataset:{textUrl:'/old-revision/text'},isConnected:true,replaceChildren(){replaced=true;}};
  r.context.page={querySelector:()=>layer};
  r.context.api=()=>{calls++;return new Promise(done=>{resolve=done;});};
  const pending=r.eval('loadReaderText(page)');
  await r.eval('loadReaderText(page)');assert.equal(calls,1);
  layer.isConnected=false;resolve({lines:[]});await pending;
  assert.equal(replaced,false);assert.equal(layer.dataset.loaded,undefined);
  assert.equal(layer.dataset.loading,undefined);
});
