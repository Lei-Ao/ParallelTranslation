'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const quality=require('../static/translation-quality.js');

test('quality output escapes model and source HTML',()=>{
  assert.equal(quality.escape('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
});

test('history UI exposes no model workflow runner',()=>{
  assert.equal(quality.run,undefined);
});
