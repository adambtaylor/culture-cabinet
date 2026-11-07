// Run with installed Node; optional argument selects the actual static script under review.
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = readFileSync(process.argv[2] || require('node:path').resolve(__dirname, '../../../static/card-feedback.js'), 'utf8');
function setup(responses) {
  const listeners = {};
  const status = {_text:'', get textContent(){return this._text}, set textContent(text){this._text=text;if(this.retry && document.activeElement===this.retry) document.activeElement=document.body;this.retry=null}, contains(element){return Boolean(this.retry) && element === this.retry}, append(...items) { this.retry = items.find(x => typeof x === 'object'); }};
  const authority = {revision:{value:'1'},nonce:{value:'old'},run_id:{value:'run'},queue:{value:'book'}};
  const otherNote = {value:'Unsent note stays exact.\n'};
  const latestUndo = {disabled:true,dataset:{}};
  const latestRun = {value:''};
  const buttons = [{disabled:false,dataset:{}}, latestUndo];
  let replaced = false; let focusCalls = 0;
  const card = {dataset:{cardKey:'book:1'},setAttribute(){}, removeAttribute(){},getBoundingClientRect(){return {top:100}},contains(element){return element?.inside === true},replaceWith(){replaced=true}};
  const noteField = {tagName:'TEXTAREA',readOnly:false};
  const selectField = {tagName:'SELECT',disabled:false,options:[{selected:true,defaultSelected:false}]};
  const form = {dataset:{},querySelectorAll(){return [noteField, selectField]},action:'/choose/',getAttribute(name){return name==='action'?'/choose/':null},matches(){return true},hasAttribute(){return false},closest(){return card}};
  const requests=[];
  const document = {
    body:{},activeElement:{inside:true},getElementById(){return status},
    querySelectorAll(selector) {
      if(selector.startsWith('[data-feedback-latest-undo] input')) return [latestRun];
      if(selector === '[data-feedback-latest-undo] button') return [latestUndo];
      if(selector.includes('button')) return buttons;
      if(selector === '[data-feedback-card]') return [];
      const name=/name="([^"]+)"/.exec(selector)?.[1];
      return authority[name] ? [authority[name]] : [];
    },
    addEventListener(name,fn){listeners[name]=fn},
    createElement(tag){
      if(tag==='template') return {content:{querySelector(){return {focus(){focusCalls++},getBoundingClientRect(){return {top:100}}}}}};
      return {addEventListener(name,fn){this[name]=fn}};
    }
  };
  class FormData {constructor(){this.values={nonce:'old',note:'exact typed note'}} set(k,v){this.values[k]=v}}
  const window = {fetch:true,FormData:true,setTimeout(){return 1},clearTimeout(){},scrollBy(){},addEventListener(){}};
  const fetch = async(url,options) => {
    requests.push(options);
    const next=responses.shift();
    if(next instanceof Error) throw next;
    return {ok:next.status===200,status:next.status,headers:{get(){return next.type||'application/json'}},json:async()=>next.body,text:async()=>next.body};
  };
  vm.runInNewContext(source,{document,window,FormData,fetch,AbortController});
  const submit=()=>listeners.submit({target:form,submitter:{name:'choice',value:'intrigued'},preventDefault(){}});
  return {document,latestUndo,latestRun,get focusCalls(){return focusCalls},submit,status,requests,authority,otherNote,buttons,noteField,selectField,get replaced(){return replaced}};
}
const flush = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const ok={status:200,body:{ok:true,submitted_key:'book:1',replacement_html:'<article></article>',revision:2,nonce:'new',queue:'book',run_id:'new-run',counts:{intrigued:1}}};
  const s=setup([ok]); s.submit(); s.submit(); await flush();
  assert.equal(s.requests.length,1,'double submission is serialized');
  assert.equal(s.replaced,true,'only card replaced after confirmed receipt');
  assert.equal(s.authority.nonce.value,'new');
  assert.equal(s.otherNote.value,'Unsent note stays exact.\n');
  assert.equal(s.focusCalls,1,'focus follows replacement while still in submitted card');
  assert.equal(s.latestUndo.disabled,false,'first response enables latest undo');
  assert.equal(s.latestRun.value,'new-run','first response updates latest undo run authority');
  const moved=setup([ok]);moved.submit();moved.document.activeElement=moved.otherNote;await flush();
  assert.equal(moved.focusCalls,0,'slow receipt preserves focus moved to a sibling note');
  const u=setup([new Error('network'),ok]); u.submit(); await flush();
  assert.equal(u.replaced,false); assert.equal(u.noteField.readOnly,true); assert.equal(u.selectField.disabled,true); assert(u.status.retry,'uncertain response has explicit retry');
  u.submit(); assert.equal(u.requests.length,1,'other submits blocked during uncertainty');
  u.document.activeElement=u.status.retry;u.status.retry.click(); await flush();
  assert.equal(u.focusCalls,1,'focused explicit retry returns focus to replacement');
  assert.equal(u.requests[0].body,u.requests[1].body,'retry uses exact original FormData object');
  assert.equal(u.requests[1].body.values.nonce,'old');
  const movedRetry=setup([new Error('network'),ok]);movedRetry.submit();await flush();movedRetry.document.activeElement=movedRetry.status.retry;movedRetry.status.retry.click();movedRetry.document.activeElement=movedRetry.otherNote;await flush();
  assert.equal(movedRetry.focusCalls,0,'moving to a sibling while retrying also preserves focus');
  const c=setup([{status:409,body:{message:'stale'}}]);c.submit();await flush();
  assert.equal(c.replaced,false);assert.match(c.status.textContent,/reload/i);c.submit();assert.equal(c.requests.length,1);
  const h=setup([{status:200,type:'text/html',body:'<html>Login</html>'}]);h.submit();await flush();
  assert.equal(h.replaced,false,'login/error HTML cannot replace a card');assert(h.status.retry);
  const e=setup([{status:400,type:'text/plain',body:'Invalid note'}]);e.submit();await flush();
  assert.equal(e.replaced,false);assert.equal(e.status.textContent,'Invalid note');assert.equal(e.buttons[0].disabled,false); assert.equal(e.noteField.readOnly,false); assert.equal(e.selectField.disabled,false);
  const correction=setup([{...ok,body:{...ok.body,operation:'correct',replacement_html:''}}]);correction.submit();await flush();assert.equal(correction.replaced,false);
  console.log('PASS: serialized submit, receipt replacement, authority/draft preservation, identical uncertain retry, 409 block, HTML rejection, plain error, correction retention, sibling/retry focus, latest undo availability');
})().catch(error=>{console.error(error);process.exitCode=1});
