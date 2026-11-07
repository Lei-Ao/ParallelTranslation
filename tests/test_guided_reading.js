const {test}=require('node:test');
const assert=require('node:assert/strict');
const model=require('../static/guided-reading.js');
test('route filter falls back for unrecognized disciplines',()=>{
 const units=[{id:'a',title:'Proof'},{id:'b',title:'Discussion'}];
 assert.deepEqual(model.routeUnits({route:'method',structure:{units}}),units);
 assert.deepEqual(model.routeUnits({route:'method',structure:{units:[...units,{id:'c',title:'Methods'}]}}),[{id:'c',title:'Methods'}]);
});
test('selection uses current document only',()=>{
 const doc={pages:[{number:2,blocks:[{id:'b',text:'source'}]}]};
 assert.equal(model.selected(doc,'missing'),null);
 assert.equal(model.selected(doc,'b').page,2);
});
test('crop normalizes reverse drags and constrains viewport',()=>{
 assert.deepEqual(model.rectangle([.8,.9],[.2,.1]),[.2,.1,.8,.9]);
 assert.deepEqual(model.rectangle([-.3,.2],[1.1,1.2]),[0,.2,1,1]);
});
test('terminal and budget-limited tasks never keep polling',()=>{
 assert.equal(model.active({jobs:{a:{status:'limited'},b:{status:'cancelled'}}}),undefined);
 assert.equal(model.active({jobs:{a:{status:'running'}}}).status,'running');
});
test('analysis progress supports old jobs and clamps numeric percentages',()=>{
 assert.equal(model.progress({stage:'reading'}),null);
 assert.equal(model.progress({progress:{percent:'50'}}),null);
 assert.equal(model.progress({progress:{percent:32.8,completed_chunks:2,total_chunks:6}}).percent,32);
 assert.equal(model.progress({progress:{percent:110}}).percent,100);
 assert.equal(model.progress({progress:{percent:-5}}).percent,0);
});
test('answer references preserve all unique structured, legacy, quote and image sources',()=>{
 const turn={refs:['a','a'],images:['image'],citations:[{id:'quote'}],structured:{sections:[{source_ids:['a','b']}]},answer:'See [[b]] and [[old]].'};
 assert.deepEqual(model.sourceIds(turn),['a','image','quote','b','old']);
 assert.deepEqual(model.sourceIds({answer:'Legacy text'}),[]);
});
test('annotation count merges highlights, staged quotes and sent citations without duplicates',()=>{
 const data={messages:[{citations:[{id:'sent-a'}]},{citations:[{id:'sent-a'},{id:'sent-b'}]}]};
 const highlights=[{id:'highlight-a'},{id:'sent-a'}];
 const staged=[{id:'staged-a'},{id:'highlight-a'}];
 assert.equal(model.annotationCount(data,staged,highlights),4);
 assert.equal(model.annotationCount(null,[],[]),0);
});
test('multimodal notes retain visual, context and issue references',()=>{
 const turn={visual_sources:['page','zoom'],context_sources:['before','after'],structured:{sections:[],issues:[{source_ids:['zoom','other']}]}};
 assert.deepEqual(model.sourceIds(turn),['page','zoom','before','after','other']);
 assert.equal(model.progress({progress:{phase:'inspecting',percent:null}}),null);
});
