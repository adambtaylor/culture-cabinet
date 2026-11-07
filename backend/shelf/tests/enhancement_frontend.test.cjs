// Run with installed Node; optional argument selects the actual static script under review.
const vm=require('node:vm');const fs=require('node:fs');const assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2] || require('node:path').resolve(__dirname, '../../../static/enhancement.js'),'utf8');
function setup(responses,{draft=false,statusUrl='',state=''}={}) {
 const nodes={};const events={};const requests=[];const timers=[];let now=0;
 const node=()=>({textContent:'',disabled:false,hidden:false,value:'',addEventListener(name,fn){this[name]=fn}});
 ['label','explanation','message','check','apply','cancel','cancel-note','status-link'].forEach(k=>nodes[k]=node());
 const button=node();const inputs={expected_digest:node(),preview_token:node(),category:node()};
 const form={action:'/enhance/',elements:{namedItem:n=>inputs[n]},querySelector:()=>button,addEventListener(name,fn){events[name]=fn}};nodes.form=form;
 const root={dataset:{state,statusUrl},querySelector(selector){if(selector.includes('csrfmiddlewaretoken'))return{value:'csrf'};const key=/data-enhance-([^\]]+)/.exec(selector)?.[1];return nodes[key]}};
 const note={value:draft?'unsent':'',defaultValue:''};const deck={innerHTML:'original',getBoundingClientRect:()=>({top:100})};let authority=0;
 const document={querySelector(selector){if(selector==='[data-enhancement]')return root;if(selector.startsWith('[data-feedback-deck'))return deck;},querySelectorAll(){return[note]}};
 const window={fetch:true,FormData:true,clearTimeout(){},setTimeout(fn,ms){timers.push({fn,ms});return timers.length},scrollBy(){},CabinetFeedback:{isBusy:()=>false,refreshAuthority(){authority++}}};
 class FormData{constructor(form){this.values=form?{expected_digest:inputs.expected_digest.value,preview_token:inputs.preview_token.value,category:inputs.category.value}:{}}set(k,v){this.values[k]=v}}
 async function fetch(url,options){requests.push({url,...options});const response=responses.shift();if(response instanceof Error)throw response;return{ok:response.status===undefined||response.status===200,status:response.status||200,headers:{get:()=>response.type||'application/json'},json:async()=>response.body};}
 vm.runInNewContext(source,{window,document,FormData,AbortController,fetch,Date:{now:()=>now},Set});
 return{inputs,form,advance(ms){now+=ms},nodes,requests,note,deck,timers,window,button,get authority(){return authority},submit:()=>events.submit({preventDefault(){}})};
}
const flush=()=>new Promise(r=>setImmediate(r));
const pending={state:'awaiting_authorized_worker',request_id:'r1',status_url:'/status/r1/',label:'Awaiting authorized worker',explanation:'Actual model work is pending.'};
(async()=>{
 const d=setup([],{draft:true});await d.submit();assert.equal(d.requests.length,0);assert.match(d.nodes.message.textContent,/Save your current reactions/);
 const p=setup([{body:pending},{body:pending}]);await p.submit();assert.equal(p.requests.length,1);assert.equal(p.nodes.label.textContent,'Awaiting authorized worker');assert(p.timers.some(t=>t.ms===2000));await p.submit();assert.equal(p.requests.length,1,'no blind new reservation for existing work');p.advance(600001);await p.timers.find(t=>t.ms===2000).fn();assert.equal(p.timers.filter(t=>t.ms===2000).length,1,'polling stops at ten minutes');assert.match(p.nodes.message.textContent,/ten minutes/);
 const ready={...pending,state:'completed',application_state:'pending',apply_url:'/apply/r1/',cancel_url:'/cancel/r1/'};
 const r=setup([{body:ready},{body:{...ready,application_state:'applied',deck_kind:'catalogue',deck_html:'verified actual deck',revision:2,nonce:'new'}}],{statusUrl:'/status/r1/'});await flush();assert.equal(r.requests.length,2);assert.equal(r.requests[1].url,'/apply/r1/');assert.equal(r.requests[1].body.values.csrfmiddlewaretoken,'csrf');assert.equal(Object.keys(r.requests[1].body.values).length,1,'apply carries only CSRF');assert.equal(r.deck.innerHTML,'verified actual deck');assert.equal(r.authority,1);
 const held=setup([{body:ready}],{draft:true,statusUrl:'/status/r1/'});await flush();assert.equal(held.requests.length,1,'ready result does not discard unsent notes');assert.equal(held.deck.innerHTML,'original');assert.equal(held.note.value,'unsent');assert.match(held.nodes.message.textContent,/drafts have been preserved/);
 const html=setup([{type:'text/html',body:'login'}]);await html.submit();assert.match(html.nodes.message.textContent,/No confirmed JSON/);await html.submit();assert.equal(html.requests.length,1,'uncertain start cannot blind retry model work');
 const fail=setup([{body:{...pending,state:'unknown',label:'Unknown',explanation:'Check existing work'}}],{statusUrl:'/status/r1/'});await flush();assert.equal(fail.requests.length,1);assert.equal(fail.timers.filter(t=>t.ms===2000).length,0,'unknown work does not auto reserve or poll indefinitely');
 const cancelled=setup([{body:ready},{body:{...ready,application_state:'cancelled',external_work_cancelled:false}}],{draft:true,statusUrl:'/status/r1/'});await flush();cancelled.note.value='';await cancelled.nodes.cancel.click();assert.equal(cancelled.requests[1].url,'/cancel/r1/');assert.match(cancelled.nodes.message.textContent,/External model work may continue/);

 const reused=setup([{body:ready},{body:{...ready,application_state:'applied',deck_kind:'catalogue',deck_html:'reused actual result'}}]);await reused.submit();assert.equal(reused.requests.length,2,'completed reservation response applies without waiting for a poll');assert.equal(reused.deck.innerHTML,'reused actual result');
 for (const context of [
   {state:'stale',request_id:'old',status_url:'/old/'},
   {state:'cancelled',attempts:0,request_id:'old',status_url:'/old/'},
   {state:'completed',application_state:'applied',request_id:'old',status_url:'/old/'},
   {state:'no_candidates',request_id:'old',status_url:'/old/'},
   {state:'completed',outcome:'unchanged',request_id:'old',status_url:'/old/'}
 ]) {
   const fresh=setup([{body:pending}]);fresh.window.CabinetEnhancement.refreshContext({enhancement:context});assert.equal(fresh.button.disabled,false,'explicit new consumer request permitted for '+context.state);await fresh.submit();assert.equal(fresh.requests.length,1);
 }
 for (const state of ['failed','unknown','queued','running']) {
   const locked=setup([]);locked.window.CabinetEnhancement.refreshContext({enhancement:{state,request_id:'old',status_url:'/old/'}});await locked.submit();assert.equal(locked.requests.length,0,'no blind retry for '+state);
 }
 const freshContext={enhance_digest:'fresh-digest',enhance_token:'fresh-token',active_queue:'book',enhance_url:'/enhance/book/',enhancement:{state:'completed',application_state:'applied',label:'Context label'}};
 const changed=setup([{body:ready},{body:{...ready,application_state:'applied',deck_kind:'catalogue',deck_html:'changed verified deck',label:'Applied receipt label',enhance_context:freshContext}},{body:pending}]);
 changed.inputs.preview_token.value='old-token';await changed.submit();
 assert.equal(changed.deck.innerHTML,'changed verified deck');assert.equal(changed.inputs.preview_token.value,'fresh-token');assert.equal(changed.inputs.expected_digest.value,'fresh-digest');assert.equal(changed.form.action,'/enhance/book/');assert.equal(changed.nodes.label.textContent,'Applied receipt label','application receipt remains the displayed status');
 await changed.submit();assert.equal(changed.requests[2].body.values.preview_token,'fresh-token');assert.equal(changed.requests[2].body.values.category,'book');
 // A feedback context update invalidates a delayed status receipt before it can apply.
 const stale=setup([{body:pending}],{statusUrl:'/status/old/'});
 stale.window.CabinetEnhancement.refreshContext({enhancement:{state:'stale',label:'Current feedback context'}});await flush();
 assert.equal(stale.nodes.label.textContent,'Current feedback context');assert.equal(stale.requests.length,1);
 console.log('PASS: draft gate, honest worker status, bounded polling, no duplicate reservation, actual apply CSRF/deck authority, ready draft preservation, HTML rejection, unknown stop, honest application cancellation, fresh apply context, receipt status and generation guard');
})().catch(error=>{console.error(error);process.exitCode=1});
