const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const scope={};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../app/static/js/components/cards.js'),'utf8').replaceAll('export ','')+'\nglobalThis.render=renderHands;',scope);
const round=devices=>({devices,state:'collecting',enabled:true});
test('live rows show actual arbitrary ids and exclude idle registrations',()=>{
 const html=scope.render(round([{client_id:'01',online:false},{client_id:'emu_A-12',online:true},{client_id:'worker_z',online:true}]));
 assert.match(html,/客户端 emu_A-12/);assert.match(html,/客户端 worker_z/);assert.doesNotMatch(html,/客户端 01/);
 assert.match(html,/等待客户端连接/);
});
test('switching from 07 to 12 changes the visible identity',()=>{
 const devices=[{client_id:'07',online:true},{client_id:'12',online:false}];
 assert.match(scope.render(round(devices)),/客户端 07/);
 devices[0].online=false;devices[1].online=true;
 const html=scope.render(round(devices));assert.match(html,/客户端 12/);assert.doesNotMatch(html,/客户端 07/);
});
test('a submitted hand remains visible when that client disconnects',()=>{
 const html=scope.render(round([{client_id:'table_A',online:false,cards:[{rank:'A',suit:'s'}]}]));
 assert.match(html,/客户端 table_A/);assert.match(html,/离线/);assert.match(html,/已上报/);
});
test('history retains recorded identities and display escapes text',()=>{
 const html=scope.render(round([{client_id:'archived_12',online:false},{client_id:'<id>',online:false}]),false);
 assert.match(html,/客户端 archived_12/);assert.match(html,/&lt;id&gt;/);assert.doesNotMatch(html,/<id>/);
});
