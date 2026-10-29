'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const TranslationQueue=require('../static/translation-queue.js');

// The reader-link contract: every layout report link carries the page it lives
// on as a 1-based `source_page`. A goto link's `page` is the TARGET page, so
// overlays must be placed by `source_page` only.
function blankElement(){
  const classes=new Set();
  return {innerHTML:'',textContent:'',value:'',checked:false,scrollTop:0,scrollLeft:0,
    scrollWidth:500,clientWidth:0,clientHeight:400,style:{},
    classList:{toggle:(name,on)=>{if(on)classes.add(name);else classes.delete(name);},
      add:(...names)=>names.forEach(name=>classes.add(name)),
      remove:(...names)=>names.forEach(name=>classes.delete(name)),
      contains:name=>classes.has(name)},
    getBoundingClientRect:()=>({top:0,height:0}),prepend(){},querySelectorAll:()=>[]};
}
function harness(overrides={}){
  const elements=new Map(Object.entries(overrides));
  const context=vm.createContext({
    TranslationQueue,
    localStorage:{getItem:()=>null},
    document:{
      querySelector:selector=>{if(!elements.has(selector))elements.set(selector,blankElement());return elements.get(selector);},
      querySelectorAll:()=>[],
      createElement:()=>blankElement()
    }
  });
  const source=fs.readFileSync(require.resolve('../static/app.js'),'utf8').split("$('#settings-button').onclick")[0];
  // updateControls touches many unrelated DOM nodes; the link tests only exercise
  // overlay rendering and page filtering.
  vm.runInContext(source+'\nfunction updateControls(){}',context);
  return {elements,eval:expression=>vm.runInContext(expression,context)};
}
function json(value){return JSON.stringify(value);}
function parse(r,expression){return JSON.parse(r.eval(`JSON.stringify(${expression})`));}

const readerLinks=[
  {kind:'uri',from:[0,.5,.25,.75],uri:'https://example.org/one',source_page:1},
  {kind:'goto',from:[.5,.5,.75,.75],page:3,fraction:.42,source_page:1},
  {kind:'uri',from:[0,.5,.25,.75],uri:'https://example.org/two',source_page:2},
  {kind:'uri',from:[.25,.5,.5,.75],uri:'https://example.org/unsourced'}
];

test('layoutLinksForPage keeps every link on its source page, including goto targets',()=>{
  const r=harness();
  r.eval(`state.layout={revision:'rev',placements:{},links:${json(readerLinks)}}`);
  const pageOne=parse(r,'layoutLinksForPage(1).map(link=>({kind:link.kind,uri:link.uri,page:link.page,source_page:link.source_page}))');
  assert.equal(pageOne.length,2);
  assert.deepEqual(pageOne.map(link=>link.kind),['uri','goto']);
  assert.equal(pageOne[1].page,3); // target page, not the page the overlay lives on
  const pageTwo=parse(r,'layoutLinksForPage(2)');
  assert.equal(pageTwo.length,1);
  assert.equal(pageTwo[0].uri,'https://example.org/two');
  // A goto target page must not pull the overlay onto the target page, and a
  // link without source_page must not be guessed onto any page.
  assert.deepEqual(parse(r,'layoutLinksForPage(3)'),[]);
  assert.deepEqual(parse(r,'layoutLinksForPage(0)'),[]);
  r.eval('state.layout=null');
  assert.deepEqual(parse(r,'layoutLinksForPage(1)'),[]);
});

test('linkOverlays renders a normalized, safe anchor for uri links',()=>{
  const r=harness();
  const html=r.eval(`linkOverlays(${json([{kind:'uri',from:[.125,.25,.625,.75],
    uri:'https://example.org/paper?x=1&y=2',source_page:4}])})`);
  assert.match(html,/^<a class="link-overlay"/);
  assert.match(html,/<a class="link-overlay"[^>]*href="https:\/\/example\.org\/paper\?x=1&amp;y=2"/);
  assert.match(html,/target="_blank"/);
  assert.match(html,/rel="noopener noreferrer"/);
  assert.match(html,/style="left:12\.5%;top:25%;width:50%;height:50%"/);
  assert.doesNotMatch(html,/<button/);
});

test('linkOverlays renders a goto button that remembers the target page and fraction',()=>{
  const r=harness();
  const html=r.eval(`linkOverlays(${json([
    {kind:'goto',from:[.5,.25,.75,.75],page:3,fraction:.42,source_page:1},
    {kind:'goto',from:[.5,.25,.75,.75],page:2,source_page:1}
  ])})`);
  const buttons=html.match(/<button[^>]*>/g)||[];
  assert.equal(buttons.length,2);
  assert.match(buttons[0],/^<button type="button" class="link-overlay"/);
  assert.match(buttons[0],/data-link-page="2"/);
  assert.match(buttons[0],/data-link-fraction="0\.42"/);
  assert.match(buttons[0],/style="left:50%;top:25%;width:25%;height:50%"/);
  assert.doesNotMatch(buttons[0],/href=/);
  assert.match(buttons[1],/data-link-page="1"/);
  assert.match(buttons[1],/data-link-fraction="0"/);
  assert.doesNotMatch(html,/<a /);
});

test('wrapped link fragments share a group so one hover highlights the whole link',()=>{
  const r=harness();
  const grouped=r.eval(`linkOverlays(${json([
    {kind:'uri',from:[.1,.1,.4,.15],uri:'https://example.org/long',source_page:1,group:'g1'},
    {kind:'uri',from:[.1,.15,.35,.2],uri:'https://example.org/long',source_page:1,group:'g1'},
    {kind:'uri',from:[.5,.5,.7,.55],uri:'https://example.org/other',source_page:1,group:'g2'}
  ])})`);
  assert.ok(grouped.startsWith('<span class="link-group">'), grouped);
  assert.equal((grouped.match(/class="link-group"/g)||[]).length,1,grouped);
  assert.equal((grouped.match(/<a class="link-overlay"/g)||[]).length,3,grouped);
  // Entries without a group keep the previous one-element behavior.
  const plain=r.eval(`linkOverlays(${json([
    {kind:'uri',from:[.1,.1,.4,.15],uri:'https://example.org/a',source_page:1},
    {kind:'uri',from:[.1,.2,.4,.25],uri:'https://example.org/a',source_page:1}
  ])})`);
  assert.equal((plain.match(/<a class="link-overlay"/g)||[]).length,2,plain);
  assert.doesNotMatch(plain,/link-group/);
});

test('linkOverlays drops malformed or incomplete reader links',()=>{
  const r=harness();
  const invalid=[
    {kind:'uri',uri:'https://example.org/no-rect',source_page:1},
    {kind:'uri',from:[.1,.2,.3],uri:'https://example.org/short-rect',source_page:1},
    {kind:'uri',from:[.1,.2,.3,.4],source_page:1},
    {kind:'goto',from:[.1,.2,.3,.4],page:0,fraction:.5,source_page:1},
    {kind:'goto',from:[.1,.2,.3,.4],page:-2,fraction:.5,source_page:1},
    {kind:'goto',from:[.1,.2,.3,.4],page:1.5,fraction:.5,source_page:1},
    {kind:'goto',from:[.1,.2,.3,.4],fraction:.5,source_page:1},
    {kind:'external',from:[.1,.2,.3,.4],uri:'https://example.org/unknown-kind',source_page:1}
  ];
  assert.equal(r.eval(`linkOverlays(${json(invalid)})`),'');
  assert.equal(r.eval('linkOverlays(undefined)'),'');
  assert.equal(r.eval('linkOverlays([])'),'');
});

test('renderTranslations places layout link overlays by source page',()=>{
  const r=harness();
  r.eval(`state.doc={id:'demo',pages:[
    {number:1,width:595,height:842,blocks:[{id:'b1',bbox:[.1,.1,.9,.2]}]},
    {number:2,width:595,height:842,blocks:[{id:'b2',bbox:[.1,.1,.9,.2]}]}]}`);
  r.eval(`state.view='pdf';state.layout={revision:'rev',placements:{},links:${json([
    {kind:'uri',from:[.1,.1,.3,.15],uri:'https://example.org/one',source_page:1},
    {kind:'goto',from:[.1,.2,.2,.25],page:2,fraction:.35,source_page:1},
    {kind:'uri',from:[.1,.1,.3,.15],uri:'https://example.org/two',source_page:2},
    {kind:'uri',from:[.1,.3,.2,.35],uri:'https://example.org/unsourced'}
  ])}}`);
  r.eval('renderTranslations()');
  const html=r.elements.get('#translation-pane').innerHTML;
  const pages=html.split('<div class="pdf-page reader-page"').slice(1);
  assert.equal(pages.length,2);
  assert.match(pages[0],/href="https:\/\/example\.org\/one"/);
  assert.match(pages[0],/data-link-page="1"/);
  assert.match(pages[0],/data-link-fraction="0\.35"/);
  assert.doesNotMatch(pages[0],/example\.org\/two/);
  assert.doesNotMatch(pages[0],/example\.org\/unsourced/);
  assert.match(pages[1],/href="https:\/\/example\.org\/two"/);
  assert.doesNotMatch(pages[1],/example\.org\/one/);
  assert.doesNotMatch(pages[1],/data-link-page/);
});

test('renderTranslations still uses original page links without a layout',()=>{
  const r=harness();
  r.eval(`state.doc={id:'demo',pages:[
    {number:1,width:595,height:842,blocks:[{id:'a',bbox:[.1,.1,.9,.2]}],
     links:[{kind:'uri',from:[.1,.3,.3,.35],uri:'https://example.org/original'}]}]}`);
  r.eval("state.view='pdf';state.layout=null;state.layoutError=''");
  r.eval('renderTranslations()');
  const html=r.elements.get('#translation-pane').innerHTML;
  assert.match(html,/href="https:\/\/example\.org\/original"/);
});

test('paragraph view keeps one source paragraph as one translated paragraph',()=>{
  const r=harness();
  const prose={id:'a',target:'简体中文',
    text:'This paragraph has enough ordinary words and the common stop words to be prose for this reader test.',
    translation:'第一句。\n第二句。'};
  const html=r.eval(`renderCard(${json(prose)},0)`);
  assert.match(html,/第一句。第二句。/,html);
  assert.doesNotMatch(html,/第一句。\s*\n/,html);
  // Line-structured blocks keep their line breaks.
  const list={id:'b',target:'简体中文',text:'• first item\n• second item',
    translation:'• 第一项\n• 第二项'};
  const listHtml=r.eval(`renderCard(${json(list)},1)`);
  assert.match(listHtml,/第一项\n/,listHtml);
});

test('readerClick jumps both panes to the goto target page',()=>{
  const source=blankElement();
  source.querySelectorAll=()=>[{getBoundingClientRect:()=>({top:0,height:1000}),offsetHeight:1000},
    {getBoundingClientRect:()=>({top:1000,height:1000}),offsetHeight:1000}];
  source.getBoundingClientRect=()=>({top:0,height:400});
  const translation=blankElement();
  translation.querySelectorAll=source.querySelectorAll;
  translation.getBoundingClientRect=()=>({top:0,height:400});
  const r=harness({'#source-pane':source,'#translation-pane':translation});
  r.eval(`state.doc={id:'demo',pages:[
    {number:1,width:595,height:842,blocks:[]},{number:2,width:595,height:842,blocks:[]}]}`);
  r.eval("state.view='pdf';state.page=0");
  r.eval("readerClick({target:{closest:selector=>selector==='[data-link-page]'?{dataset:{linkPage:'1',linkFraction:'0.5'}}:null}},'translation')");
  assert.equal(r.eval('state.page'),1);
  assert.equal(r.elements.get('#page-select').value,1);
  // page top (1000) + fraction (0.5 * 1000) - 14px breathing room
  assert.equal(source.scrollTop,1486);
  assert.equal(translation.scrollTop,1486);
});