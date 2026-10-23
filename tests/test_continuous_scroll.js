'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const read=name=>fs.readFileSync(path.join(__dirname,'..','static',name),'utf8');
const app=read('app.js');
const html=read('index.html');
const css=read('style.css');

test('reader renders every page in a continuous stack',()=>{
  assert.match(html,/id="pdf-page" class="pages-stack"/);
  assert.match(app,/state\.doc\.pages\.map\(\(page,pageIndex\)=>`<div class="pdf-page reader-page"/);
  assert.match(app,/class="reader-page paragraph-page"/);
  assert.match(app,/loading="lazy"/);
  assert.match(css,/\.pages-stack\{display:flex;flex-direction:column/);
  assert.match(app,/pane\.classList\.toggle\('pages-stack',state\.view==='pdf'\)/);
  assert.match(css,/\.pages-stack>\.reader-page\{flex:none\}/);
});

test('page controls scroll instead of replacing the current page',()=>{
  assert.match(app,/function choosePage\(index\)\{scrollToPage\(index\);\}/);
  assert.match(app,/function scrollToPage\(index\)\{[\s\S]*scrollPaneToPage\(\$\('#translation-pane'\),state\.page\)/);
  assert.match(app,/function synchronize\(from\)\{[\s\S]*setCurrentPage\(position\.index\)/);
});
