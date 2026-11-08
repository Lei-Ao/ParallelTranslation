'use strict';
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs');
const assert=require('node:assert/strict');
(async()=>{
  const root=path.resolve(__dirname,'..');
  const server=spawn(path.join(root,'.venv/Scripts/python.exe'),['-u',path.join(__dirname,'structure_browser_server.py')],{cwd:root,windowsHide:true});
  let browser;
  try{
    const fixture=await new Promise((resolve,reject)=>{
      let buffer='';const timer=setTimeout(()=>reject(Error('startup timeout')),20000);
      server.stdout.on('data',chunk=>{buffer+=chunk;const line=buffer.split('\n').find(x=>x.startsWith('FIXTURE '));if(line){clearTimeout(timer);resolve(JSON.parse(line.slice(8)));}});
      server.stderr.on('data',chunk=>process.stderr.write(chunk));
      server.on('exit',code=>{clearTimeout(timer);reject(Error('fixture exit '+code));});
    });
    browser=await chromium.launch({channel:'msedge',headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[],requests=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>requests.push(r.url()));
    await page.addInitScript(()=>{if(location.protocol==='http:')localStorage.setItem('parallel-agent',JSON.stringify({mode:'openai',base_url:'https://example.com/v1',model:'fixture'}));});
    for(let i=0;i<40;i++){try{await page.goto(fixture.url);break;}catch(e){if(i===39)throw e;await new Promise(r=>setTimeout(r,200));}}
    // Open via the same public UI function used by the library click handler.
    await page.evaluate(id=>openDocument(id),fixture.id);
    await page.waitForFunction(()=>state.doc?.pages.flatMap(p=>p.blocks).every(b=>b.translation)&&state.layout?.revision,{},{timeout:60000});
    const data=await page.evaluate(()=>({doc:state.doc,report:state.layout}));
    assert(data.doc.pages.flatMap(p=>p.blocks).every(b=>!b.translation.includes('[[PT_')));
    assert.equal(data.report.structure.model_pages,1);
    assert.equal(data.report.visual_check.status,'passed');
    assert(requests.some(url=>url.endsWith('/structure')));
    await page.waitForFunction(()=>{const images=[...document.querySelectorAll('.reader-page img')];return images.length>=2&&images.every(img=>img.complete&&img.naturalWidth>0);});
    await page.evaluate(()=>document.querySelector('#toast').classList.add('hidden'));
    assert.equal(errors.length,0,errors.join('\n'));
    fs.mkdirSync(path.join(root,'tmp/structure-browser'),{recursive:true});
    await page.screenshot({path:path.join(root,'tmp/structure-browser/translated.png'),fullPage:true});
    await page.setViewportSize({width:430,height:900});
    await page.waitForFunction(()=>[...document.querySelectorAll('.reader-page img')].every(img=>img.complete&&img.naturalWidth>0));
    await page.screenshot({path:path.join(root,'tmp/structure-browser/mobile.png'),fullPage:true});
    console.log(JSON.stringify({passed:true,blocks:data.doc.pages.flatMap(p=>p.blocks).length,modelPages:data.report.structure.model_pages,checks:['API structure preparation','multimodal image payload','API visual acceptance','protected direct translation','plain saved text','PDF layout','no page errors']}));
  }finally{if(browser)await browser.close();server.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
