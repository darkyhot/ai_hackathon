import {JSDOM} from 'jsdom';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';
import path from 'node:path';
const repo=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const base=process.env.UI_TEST_BASE || 'http://127.0.0.1:8792';
const dom=new JSDOM(fs.readFileSync(repo+'/frontend/index.html','utf8'),{url:base,pretendToBeVisual:true});
const w=dom.window;
for(const k of ['window','document','localStorage','location','history','HTMLElement','HTMLInputElement','Event','KeyboardEvent','CustomEvent','Node'])globalThis[k]=k==='window'?w:w[k];
Object.defineProperty(globalThis,'navigator',{value:w.navigator,configurable:true});
globalThis.matchMedia=w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
w.HTMLElement.prototype.scrollIntoView=function(){};
w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
w.HTMLDialogElement.prototype.close=function(){this.open=false};
globalThis.ResizeObserver=class {observe(){} unobserve(){} disconnect(){}};
globalThis.requestAnimationFrame=w.requestAnimationFrame.bind(w);
globalThis.cancelAnimationFrame=w.cancelAnimationFrame.bind(w);
globalThis.CSS={escape:s=>s.replaceAll('"','\\"')};
globalThis.confirm=()=>true;
localStorage.setItem('voice','0');localStorage.setItem('cookieNotice','1');
const realFetch=globalThis.fetch;let cookie='';const requests=[];
globalThis.fetch=async(url,opt={})=>{
 const headers=new Headers(opt.headers);if(cookie)headers.set('Cookie',cookie);
 const r=await realFetch(new URL(url,base),{...opt,headers});
 const set=r.headers.getSetCookie();if(set.length)cookie=set.map(c=>c.split(';')[0]).join('; ');
 if(opt.method==='POST')requests.push({url:String(url),body:opt.body?JSON.parse(opt.body):null,status:r.status});
 return r;
};
const sources=[];
class SSE {
 constructor(url){this.ctrl=new AbortController();sources.push(this);this.run(url)}
 async run(url){try{
 const r=await globalThis.fetch(url,{signal:this.ctrl.signal});assert.equal(r.status,200);
 let pending='';const decoder=new TextDecoder();for await(const part of r.body){pending+=decoder.decode(part,{stream:true});let end;while((end=pending.indexOf('\n\n'))>=0){const ev=pending.slice(0,end);pending=pending.slice(end+2);for(const line of ev.split('\n'))if(line.startsWith('data:'))this.onmessage?.({data:line.slice(5).trim()});}}
 }catch(e){if(e.name!=='AbortError'){console.error(e);this.onerror?.(e)}}}
 close(){this.ctrl.abort()}
}
globalThis.EventSource=SSE;
const errors=[];const oldError=console.error;console.error=(...x)=>{errors.push(x.map(String).join(' '));oldError(...x)};
await import(pathToFileURL(repo+'/frontend/js/app.js'));
const stateMod=await import(pathToFileURL(repo+'/frontend/js/state.js'));
const {state}=stateMod;
const q=s=>document.querySelector(s);
const until=async(fn,label,ms=45000)=>{const end=Date.now()+ms;while(Date.now()<end){if(fn())return;await new Promise(r=>setTimeout(r,40))}throw Error('Timeout '+label+'; '+q('#stage').textContent.slice(0,180)+'; '+q('#decisionHost').textContent.slice(0,180))};
const action=async(selector)=>{await until(()=>q(selector)&&!q(selector).disabled,selector);q(selector).click();};
const button=(text,host='#decisionHost')=>[...q(host).querySelectorAll('button')].find(b=>b.textContent.trim().includes(text)&&!b.disabled);
const click=async(text,host='#decisionHost')=>{await until(()=>button(text,host),text);button(text,host).click()};
const form=q('#entryForm');
await until(()=>q('#account button'),'startup');
assert.equal(q('#stepper').hidden,true);assert.equal(document.querySelector('.tabs').hidden,true);
q('#portalBudget').value='9';q('#portalCity').value='Казань';q('[data-action="portal-search"]').click();
assert.equal(q('#entryDialog').open,true);assert.equal(state.sid,null,'search opens entry without duplicate request');
q('#entryQuery').value='Ищу 2-комнатную в Казани до 9 млн, ипотека, на взнос ещё копим, сын 4 года, есть маткапитал';
form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
await until(()=>q('#decisionHost .confirm-red_lines'),'red lines');
assert.equal(q('#chatLog .confirm-red_lines'),null,'active confirmation is in workspace');
q('[data-budget]').value='9000000';await click('Подтвердить рамки');
await until(()=>q('#decisionHost .confirm-finance'),'finance form');
q('#decisionHost [name=savings]').value='1500000';await click('Рассчитать стратегию');
await until(()=>state.results[2]?.finance,'finance result');
assert.ok(state.results[2].finance.inputs.savings===1500000);
await until(()=>state.awaiting==='selection'&&q('[data-selection="proceed"]:not(:disabled)'),'selection');
assert.equal(q('#decisionHost .confirm:not(.is-done)'),null);
assert.ok(q('#chatLog .confirm-finance.is-done'),'resolved confirmation moved to history');
assert.ok(q('.offer'),'live cards render');
// Render the finance view through the retained internal step control; hidden from customer UI.
q('[data-open-finance]').click();await until(()=>q('[data-loan-choice]'),'mortgage choices');
q('[data-loan-choice]').click();assert.ok(q('.loan-choice-summary'),'loan is selectable');
assert.ok(q('#stage').textContent.includes('Ваш финансовый помощник'));
q('[data-return-selection]').click();await until(()=>q('[data-vote="up"]'),'selection return');
const id=q('.offer').dataset.id;q('[data-vote="up"]').click();
assert.ok(state.marks.liked.has(id));
await action('[data-selection="proceed"]');
await click('Звонит ИИ');
await until(()=>q('#decisionHost .confirm-price'),'price',90000);
assert.ok(q('#stage .call'),'transcript visible beside price approval');
await click('Подтвердить');
await until(()=>q('#decisionHost .confirm-object'),'viewing');
assert.ok(state.results[7]?.slots.length,'server viewing slots retained');
await click('Выбираю эту квартиру');await click('Подписать электронно');
await until(()=>state.results[9],'summary');
assert.ok(requests.some(r=>r.body?.kind==='selection'&&r.body.data.liked.includes(id)&&r.status===200));
assert.ok(requests.some(r=>r.body?.kind==='finance'&&r.body.data.savings===1500000&&r.status===200));
assert.ok(requests.some(r=>r.body?.kind==='call'&&r.body.data.mode==='auto'&&r.status===200));
assert.ok(requests.every(r=>r.status===200),JSON.stringify(requests.filter(r=>r.status!==200)));
assert.deepEqual(errors,[]);
console.log('PASS: integrated DOM + actual API: entry, editable budget/finance, mortgage selection, liked IDs, call, price, viewing, signing, confirmation history.');
console.log('POST requests:',requests.length);
for(const s of sources)s.close();dom.window.close();process.exit(0);
