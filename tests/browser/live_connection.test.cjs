const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
function setup(){
 let now=0,id=0;const timers=new Map(),listeners=new Map(),statuses=[],messages=[],sockets=[];
 class Socket{static OPEN=1;constructor(){this.readyState=0;this.sent=[];sockets.push(this);}open(){this.readyState=1;this.onopen?.();}receive(message){this.onmessage?.({data:JSON.stringify(message)});}send(message){this.sent.push(JSON.parse(message));}close(){this.readyState=3; /* Deliberately no close event: a stalled transport. */}}
 const sandbox={WebSocket:Socket,location:{protocol:'http:',host:'localhost'},performance:{now:()=>now},console,
  window:{addEventListener:(k,fn)=>listeners.set(k,fn),removeEventListener:k=>listeners.delete(k)},
  setTimeout:(fn,ms)=>{timers.set(++id,{fn,at:now+ms});return id;},clearTimeout:i=>timers.delete(i),
  setInterval:(fn,ms)=>{timers.set(++id,{fn,at:now+ms,every:ms});return id;},clearInterval:i=>timers.delete(i)};
 const source=fs.readFileSync(path.join(__dirname,'../../app/static/js/api.js'),'utf8').replaceAll('export ','');
 vm.runInNewContext(source+'\nglobalThis.TestConnection=LiveConnection;',sandbox);
 const connection=new sandbox.TestConnection(m=>messages.push(m),s=>statuses.push(s));
 function advance(ms){const end=now+ms;while(true){const next=[...timers].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;now=next[1].at;timers.delete(next[0]);if(next[1].every)timers.set(next[0],{...next[1],at:now+next[1].every});next[1].fn();}now=end;}
 return {connection,sockets,statuses,messages,listeners,advance,timers};
}
test('browser offline immediately invalidates online status',()=>{const h=setup();h.sockets[0].open();h.listeners.get('offline')?.();assert.equal(h.statuses.at(-1),false);h.connection.close();});
test('silent open socket becomes stale and reconnects without a close event',()=>{const h=setup();h.sockets[0].open();h.advance(40000);assert.equal(h.statuses.at(-1),false);assert.ok(h.sockets.length>=2);h.connection.close();});
test('valid traffic keeps the connection alive',()=>{const h=setup();h.sockets[0].open();for(let i=0;i<8;i++){h.advance(10000);h.sockets[0].receive({type:'pong'});}assert.equal(h.sockets.length,1);assert.equal(h.statuses.at(-1),true);h.connection.close();});
test('reconnect subscribes to the selected tenant and rejects callbacks from retired socket',()=>{const h=setup();h.connection.subscribe('100001');h.sockets[0].open();const late=h.sockets[0].onmessage;h.advance(40000);const current=h.sockets.at(-1);current.open();assert.equal(current.sent[0].tenant_id,'100001');late({data:'{"type":"old"}'});assert.equal(h.messages.length,0);current.receive({type:'admin.snapshot'});assert.equal(h.messages.length,1);h.connection.close();});
test('intentional close cancels reconnect timers and event listeners',()=>{const h=setup();h.sockets[0].open();h.connection.close();h.advance(120000);assert.equal(h.sockets.length,1);assert.equal(h.timers.size,0);assert.equal(h.listeners.size,0);});
test('connection that never opens is also retried',()=>{const h=setup();h.advance(40000);assert.ok(h.sockets.length>=2);h.connection.close();});
