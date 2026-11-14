import assert from 'node:assert/strict';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { Miniflare } from 'miniflare';
import ts from 'typescript';
for(const name of ['packet-validation','tool-contract','gateway']) {
  const code=ts.transpileModule(readFileSync('lib/'+name+'.ts','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll('"./tool-contract"','"./tool-contract-test.mjs"').replaceAll('"./packet-validation"','"./packet-validation-test.mjs"');
  writeFileSync('.test-build/'+name+'-test.mjs',code);
}
const {handleMcp,handleAgent,digest}=await import('../.test-build/gateway-test.mjs');
const mf=new Miniflare({modules:true,script:'export default {fetch(){return new Response("test")}}',compatibilityDate:'2026-05-22',d1Databases:['DB'],r2Buckets:['BUCKET']});
try {
  const db=await mf.getD1Database('DB'), bucket=await mf.getR2Bucket('BUCKET');
  const s={db,bucket,secret:'synthetic-test-secret-not-for-production'};
  for(const filename of readdirSync('migrations').filter(x=>x.endsWith('.sql'))) {
    for(const statement of readFileSync('migrations/'+filename,'utf8').split('--> statement-breakpoint').map(x=>x.trim()).filter(Boolean)) await db.prepare(statement).run();
  }
  const rpc=(method,params={},user='synthetic-owner')=>new Request('https://example.test/mcp',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id:1,method,params})});
  const call=async(name,args={},user='synthetic-owner')=>{const res=await handleMcp(rpc('tools/call',{name,arguments:args},user),s,user);return {status:res.status,...await res.json()};};
  const agent=async(payload,secret=s.secret)=>{const res=await handleAgent(new Request('https://example.test/api/agent',{method:'POST',headers:{'X-ThreadSatchel-Key':secret},body:JSON.stringify(payload)}),s);return {status:res.status,...await res.json()};};
  assert.equal((await handleMcp(rpc('initialize',{protocolVersion:'2025-03-26'},null),s)).status,200);
  const discovery=await (await handleMcp(rpc('tools/list',{},null),s)).json();
  assert.equal(discovery.result.tools.length,15);
  assert.equal(discovery.result.tools.find(x=>x.name==='save_memory').annotations.readOnlyHint,false);
  assert.equal(discovery.result.tools.find(x=>x.name==='search_memory').annotations.readOnlyHint,true);
  const listing=discovery.result.tools.find(x=>x.name==='list_memories');
  assert.equal(listing.annotations.readOnlyHint,true);
  assert.equal(listing.inputSchema.properties.limit.default,3);
  assert.equal(discovery.result.tools.find(x=>x.name==='search_memory').inputSchema.properties.limit.maximum,20);
  assert.equal((await call('list_memories',{},null)).status,401);
  assert.equal((await call('connection_status',{},null)).status,401);
  assert.equal((await call('connection_status')).status,200);
  assert.equal((await call('connection_status',{},'different-user')).status,403);
  assert.equal((await agent({action:'poll'},'wrong')).status,401);
  assert.equal((await call('search_memory',{query:'SYNTHETIC'})).result.isError,true);
  assert.equal((await call('run_shell',{command:'bad'})).result.isError,true);
  assert.equal((await call('search_memory',{query:'test',limit:999})).result.isError,true);
  assert.equal((await call('list_memories',{limit:101})).result.isError,true);
  assert.equal((await call('list_memories',{limit:true})).result.isError,true);
  assert.equal((await call('list_memories',{cursor:'!'})).result.isError,true);
  await agent({action:'poll'});
  const args={request_key:'synthetic-save',packet:{format:'threadsatchel/1',kind:'note',messages:[{text:'SYNTHETIC harmless test'}]}};
  const pending=call('save_memory',args);
  let job;
  for(let n=0;n<50&&!job;n++){await new Promise(r=>setTimeout(r,30));job=(await agent({action:'poll'})).job;}
  assert.ok(job);assert.equal(await digest(job.payload),job.sha256);
  // Reproduce the incident with a 210-second job while dedicated heartbeats
  // continue. They must not claim queued work or revive an invalid lease.
  const realNow=Date.now;
  const baseNow=realNow();
  const queuedId='b'.repeat(64);
  await db.prepare("INSERT INTO jobs(id,owner,action,digest,status,created,expires) VALUES(?,?,?,?,'pending',?,?)").bind(queuedId,'synthetic-owner','save_memory','synthetic',Math.floor(baseNow/1000),Math.floor(baseNow/1000)+86400).run();
  try {
    for(const elapsed of [70,140,210]) {
      Date.now=()=>baseNow+elapsed*1000;
      const beat=await agent({action:'heartbeat',id:job.id,lease:job.lease});
      assert.equal(beat.status,200);assert.equal(beat.acknowledged,true);
      assert.equal(beat.lease_until,Math.floor(Date.now()/1000)+300);
      assert.equal(JSON.parse((await call('connection_status')).result.content[0].text).online,true);
      assert.equal((await db.prepare('SELECT status FROM jobs WHERE id=?').bind(queuedId).first()).status,'pending');
    }
    const before=(await db.prepare("SELECT value FROM state WHERE key='heartbeat'").first()).value;
    Date.now=()=>baseNow+220000;
    assert.equal((await agent({action:'heartbeat',id:job.id,lease:job.lease},'wrong')).status,401);
    assert.equal((await agent({action:'heartbeat',id:job.id,lease:'wrong'})).status,409);
    assert.equal((await agent({action:'heartbeat',id:job.id,lease:''})).status,400);
    assert.equal((await agent({action:'heartbeat',id:queuedId,lease:job.lease})).status,409);
    assert.equal((await db.prepare("SELECT value FROM state WHERE key='heartbeat'").first()).value,before);
    await db.prepare('UPDATE jobs SET lease_until=? WHERE id=?').bind(Math.floor(Date.now()/1000)-1,job.id).run();
    assert.equal((await agent({action:'heartbeat',id:job.id,lease:job.lease})).status,409);
    assert.equal((await db.prepare("SELECT value FROM state WHERE key='heartbeat'").first()).value,before);
    // Heartbeats cannot extend a lease beyond the request's retention window.
    const expires=Math.floor(Date.now()/1000)+100;
    await db.prepare('UPDATE jobs SET lease_until=?,expires=? WHERE id=?').bind(expires,expires,job.id).run();
    assert.equal((await agent({action:'heartbeat',id:job.id,lease:job.lease})).lease_until,expires);
  } finally {Date.now=realNow;await db.prepare('DELETE FROM jobs WHERE id=?').bind(queuedId).run();}
  assert.equal((await agent({action:'complete',id:job.id,lease:'wrong',result:{ok:true}})).status,409);
  const complete={action:'complete',id:job.id,lease:job.lease,result:{ok:true,result:{saved:true,marker:'SYNTHETIC'}}};
  assert.equal((await agent(complete)).acknowledged,true);
  assert.equal((await agent(complete)).acknowledged,true);
  assert.equal((await agent({action:'heartbeat',id:job.id,lease:job.lease})).completed,true);
  assert.equal((await agent({action:'heartbeat',id:job.id,lease:'wrong'})).status,409);
  const final=await pending; assert.equal(final.result.isError,false);
  const retrieved=JSON.parse(final.result.content[0].text);assert.equal(retrieved.status,'completed');
  const again=await call('save_memory',args);assert.equal(JSON.parse(again.result.content[0].text).operation_id,job.id);
  const conflicting=await call('save_memory',{...args,packet:{...args.packet,title:'Changed'}});assert.equal(conflicting.result.isError,true);
  assert.equal((await call('get_operation',{operation_id:job.id},'different-user')).status,403);
  assert.equal((await db.prepare('SELECT count(*) AS n FROM jobs').first()).n,1);
  await db.prepare('UPDATE jobs SET expires=0 WHERE id=?').bind(job.id).run();
  const originalPerformanceNow=performance.now.bind(performance);
  Object.defineProperty(performance,'now',{configurable:true,value:()=>originalPerformanceNow()+60001});
  try { await call('connection_status'); }
  finally { delete performance.now; }
  assert.equal((await db.prepare('SELECT count(*) AS n FROM jobs').first()).n,0);
  assert.equal((await bucket.list()).objects.length,0);
  async function roundTrip(name,args,expectedArgs,result) {
    const pending=call(name,args);
    let job;
    for(let n=0;n<50&&!job;n++){await new Promise(r=>setTimeout(r,30));job=(await agent({action:'poll'})).job;}
    assert.ok(job);
    assert.deepEqual(JSON.parse(job.payload),{action:name,args:expectedArgs});
    await agent({action:'complete',id:job.id,lease:job.lease,result:{ok:true,result}});
    const reply=await pending;
    assert.equal(reply.result.isError,false);
    assert.deepEqual(JSON.parse(reply.result.content[0].text).result,result);
  }
  await roundTrip('search_memory',{query:'SYNTHETIC'},{query:'SYNTHETIC',limit:10,full:false,budget_tokens:2000},{hits:[]});
  await roundTrip('search_memory',{query:'SYNTHETIC',limit:20},{query:'SYNTHETIC',limit:20,full:false,budget_tokens:2000},{hits:Array.from({length:20},(_,n)=>({ref:'r'+(n+1).toString(16).padStart(12,'0')}))});
  await roundTrip('peek_memory',{id:'synthetic',revision:'a'.repeat(64),start_char:4,lookup_id:'b'.repeat(32)},
    {id:'synthetic',revision:'a'.repeat(64),start_char:4,before:500,after:500,budget_tokens:2000,lookup_id:'b'.repeat(32)},{text:'exact snippet'});
  const sourceRef={ref:'r'+'a'.repeat(12),lookup_id:'b'.repeat(32)};
  await roundTrip('get_memory',sourceRef,{...sourceRef,full:false,budget_tokens:2000},{text:'exact source'});
  await roundTrip('peek_memory',sourceRef,{...sourceRef,before:500,after:500,budget_tokens:2000},{text:'exact window'});
  await roundTrip('timeline_memory',sourceRef,{...sourceRef,before:3,after:3,budget_tokens:2000},{kind:'timeline',items:[{ref:'r'+'c'.repeat(12)}]});
  const firstPage={records:[{id:'first'}],total_count:2,returned_count:1,returned_total:1,has_more:true,next_cursor:'opaque'};
  await roundTrip('list_memories',{}, {limit:3,budget_tokens:2000},firstPage);
  await roundTrip('list_memories',{limit:1,cursor:'opaque'},{limit:1,cursor:'opaque',budget_tokens:2000}, {...firstPage,records:[{id:'last'}],returned_total:2,has_more:false,next_cursor:null});
  await roundTrip('list_memories',{cursor:null},{limit:3,cursor:null,budget_tokens:2000},firstPage);
  const extraPacket={format:'threadsatchel/1',kind:'note',provenance:{note:'SYNTHETIC source note'},messages:[{text:'SYNTHETIC extra field save',confidence:0.8}]};
  await roundTrip('save_memory',{request_key:'synthetic-extra',packet:extraPacket},{request_key:'synthetic-extra',packet:extraPacket},{saved:true,import_status:'imported_with_warnings',has_warnings:true});
  await roundTrip('recent_save_errors',{}, {limit:10},{errors:[],retention:{max_records:1000}});
  assert.equal((await call('recent_save_errors',{limit:21})).result.isError,true);
  for(const [packet,code] of [
    [{...extraPacket,messages:[]},'invalid_messages'],
    [{...extraPacket,messages:[{text:' '}]},'invalid_text'],
    [{...extraPacket,source_date:123},'invalid_metadata'],
    [{...extraPacket,messages:[{text:'a',message_id:'same'},{text:'b',message_id:'same'}]},'duplicate_identity']
  ]) {
    const invalid=await call('save_memory',{request_key:'synthetic-invalid',packet});
    const diagnostic=JSON.parse(invalid.result.content[0].text);
    assert.equal(invalid.result.isError,true);assert.equal(diagnostic.error_detail.code,code);
    assert.equal(diagnostic.error_detail.submitted,false);
  }
  const failedArgs={request_key:'synthetic-failure-retry',packet:extraPacket};
  const failedPending=call('save_memory',failedArgs);
  let failedJob;
  for(let n=0;n<50&&!failedJob;n++){await new Promise(r=>setTimeout(r,30));failedJob=(await agent({action:'poll'})).job;}
  assert.ok(failedJob);
  const failure={ok:false,error:'Every message needs nonempty text.',error_detail:{code:'importer_rejected',importer_code:'invalid_text',location:'$.messages[0].text'}};
  await agent({action:'complete',id:failedJob.id,lease:failedJob.lease,result:failure});
  assert.equal((await failedPending).result.isError,true);
  const inspect=JSON.parse((await call('get_operation',{operation_id:failedJob.id})).result.content[0].text);
  assert.deepEqual(inspect.error_detail,failure.error_detail);
  const retry=JSON.parse((await call('save_memory',failedArgs)).result.content[0].text);
  assert.equal(retry.status,'pending');assert.equal(retry.operation_id,failedJob.id);
  const retryJob=(await agent({action:'poll'})).job;assert.equal(retryJob.id,failedJob.id);
  await agent({action:'complete',id:retryJob.id,lease:retryJob.lease,result:{ok:true,result:{saved:true}}});
  console.log('PASS: discovery, authentication, owner isolation, tool validation, offline behavior, transport integrity, leases, retry deduplication, key conflicts, expiry cleanup.');
} finally {await mf.dispose();}
