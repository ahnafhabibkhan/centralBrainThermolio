const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const source = fs.readFileSync('src/central_brain/static/library.js', 'utf8');
const upload = source.slice(source.indexOf('  async function uploadQueue('), source.indexOf('  function message('));
async function scenario(failAt) {
  const bytes = Buffer.from('original document');
  const hash = Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex');
  const inventory = [0, 2, 5].map(index => ({index, name:'file.txt', size:bytes.length, sha256:hash, archive_id:'batch', destination:'/Project', ready:false}));
  const controls = [{disabled:false}];
  const notice = {};
  const file = {name:'renamed.txt', size:bytes.length, arrayBuffer:async()=>bytes};
  const form = {elements:{files:{files:[file]}, folder:{files:[]}, csrf_token:{value:'csrf'}}, dataset:{inventory:JSON.stringify(inventory)}, querySelectorAll:()=>controls};
  const calls = []; let refreshed = false;
  const context = vm.createContext({crypto:webcrypto, FormData, Uint8Array, console, document:{querySelector:()=>notice}, busy:false,
    postJSON:async(url,data)=>{calls.push(Number(data.get('index'))); if(calls.length === failAt) throw new Error('Connection interrupted.');},
    refresh:async()=>{refreshed = true;}});
  await vm.runInContext(upload+'; uploadQueue',context)(form);
  return {calls,form,notice,controls,context,refreshed};
}
test('Interrupted recovery preserves confirmed uploads and stable file indices',async()=>{
  const result = await scenario(2);
  assert.deepEqual(result.calls,[0,2]);
  assert.deepEqual(JSON.parse(result.form.dataset.inventory).map(x=>x.ready),[true,false,false]);
  assert.match(result.notice.textContent,/1 uploads confirmed.*Connection interrupted.*preserved/);
  assert.equal(result.context.busy,false);
  assert.equal(result.controls[0].disabled,false);
  assert.equal(result.refreshed,true);
});
test('One original can complete matching pending destinations without renaming it',async()=>{
  const result = await scenario(-1);
  assert.deepEqual(result.calls,[0,2,5]);
  assert.equal(JSON.parse(result.form.dataset.inventory).every(x=>x.ready),true);
  assert.match(result.notice.textContent,/3 originals received/);
});
