'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const model=require('../static/reading-assistant.js');
test('missing backend routes request service restart instead of a sidebar retry',()=>{
  const message=model.loadError({status:404,message:'Not Found'});
  assert.match(message,/重新启动服务/);
  assert.match(message,/刷新/);
  assert.doesNotMatch(message,/可重新打开助手重试/);
  assert.match(model.loadError({status:403,message:'expired'}),/会话已失效/);
  assert.equal(model.loadError({status:404,message:'文档不存在'}),'文档不存在 · 可重新打开助手重试');
});
test('latest analysis is scoped by exact paragraph and operation',()=>{
  const results={a:{kind:'summary',block_id:'a',created:'2026-01-01'},b:{kind:'summary',block_id:'b',created:'2026-02-01'},c:{kind:'explain',block_id:'a',created:'2026-03-01'}};
  assert.equal(model.latest(results,'summary','a'),results.a);
  assert.equal(model.latest(results,'overview'),undefined);
});
test('old model settings and changed source content are visibly stale',()=>{
  const result={config_key:'a',input_digest:'b'};
  assert.equal(model.stale(result,{...result}),false);
  assert.equal(model.stale(result,{config_key:'other',input_digest:'b'}),true);
  assert.equal(model.stale(result,{config_key:'a',input_digest:'changed'}),true);
});
test('source navigation rejects foreign documents and misplaced block IDs',()=>{
  const doc={id:'one',pages:[{number:1,blocks:[{id:'a'}]},{number:2,blocks:[{id:'b'}]}]};
  assert.equal(model.belongs({doc_id:'one',page:1,block_id:'a'},doc),true);
  assert.equal(model.belongs({doc_id:'two',page:1,block_id:'a'},doc),false);
  assert.equal(model.belongs({doc_id:'one',page:2,block_id:'a'},doc),false);
  assert.equal(model.belongs({doc_id:'one',page:2},doc),true);
});
test('only live jobs require polling',()=>{
  assert.deepEqual(model.active({a:{status:'completed'},b:{status:'running'},c:{status:'interrupted'}}),[{status:'running'}]);
});
