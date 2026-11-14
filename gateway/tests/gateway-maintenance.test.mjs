import assert from 'node:assert/strict';
import { readFileSync, readdirSync, writeFileSync, mkdirSync } from 'node:fs';
import ts from 'typescript';
import {Miniflare} from 'miniflare';

mkdirSync('.test-build',{recursive:true});
for(const name of ['packet-validation','tool-contract','gateway']) {
  const code=ts.transpileModule(readFileSync('lib/'+name+'.ts','utf8'),{
    compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}
  }).outputText.replaceAll('"./tool-contract"','"./tool-contract-test.mjs"')
    .replaceAll('"./packet-validation"','"./packet-validation-test.mjs"');
  writeFileSync('.test-build/'+name+'-test.mjs',code);
}
const {handleAgent,handleMcp,digest}=await import('../.test-build/gateway-test.mjs');
const mf=new Miniflare({modules:true,script:'export default {fetch(){return new Response("test")}}',
  compatibilityDate:'2026-05-22',d1Databases:['DB'],r2Buckets:['BUCKET']});
const realNow=Date.now;
const base=Math.floor(realNow()/1000);
try {
  const db=await mf.getD1Database('DB'),bucket=await mf.getR2Bucket('BUCKET');
  const s={db,bucket,secret:'synthetic-test-secret-not-for-production'};
  for(const filename of readdirSync('migrations').filter(x=>x.endsWith('.sql'))) {
    for(const statement of readFileSync('migrations/'+filename,'utf8').split('--> statement-breakpoint').map(x=>x.trim()).filter(Boolean)) await db.prepare(statement).run();
  }
  Date.now=()=>base*1000;
  const agent=async(payload,secret=s.secret,services=s)=>{
    const response=await handleAgent(new Request('https://example.test/api/agent',{
      method:'POST',headers:{'X-ThreadSatchel-Key':secret},body:JSON.stringify(payload)
    }),services);
    return {status:response.status,...await response.json()};
  };
  const call=async(name,args)=>{
    const response=await handleMcp(new Request('https://example.test/mcp',{
      method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}})
    }),s,'synthetic-owner');
    const rpc=await response.json();
    return {rpc,value:JSON.parse(rpc.result.content[0].text)};
  };
  const queue=async(label,created)=>{
    const id=await digest(label),payload=JSON.stringify({action:'get_memory',args:{id:label}}),hash=await digest(payload);
    await bucket.put(`${id}/input-${hash}`,payload);
    await db.prepare("INSERT INTO jobs(id,owner,action,digest,status,created,expires) VALUES(?,?,?,?,'pending',?,?)")
      .bind(id,'synthetic-owner','get_memory',hash,created,base+86400).run();
    return id;
  };

  for(const mode of ['pause','status','resume']) {
    assert.equal((await agent({action:'maintenance',mode},'wrong')).status,401);
  }
  assert.equal(await db.prepare("SELECT value FROM state WHERE key='claims_paused_until'").first(),null);
  assert.equal((await agent({action:'maintenance',mode:'invalid'})).status,400);
  const initial=await agent({action:'maintenance',mode:'status'});
  assert.deepEqual(initial,{status:200,server_time:base,paused_until:null,active_jobs:0,paused:false});

  const activeId=await queue('active-before-maintenance',base-30);
  const active=(await agent({action:'poll'})).job;
  assert.equal(active.id,activeId);
  const pendingId=await queue('pending-during-maintenance',base-20);

  // Pause after an in-flight poll's earlier work but before its claim SQL runs.
  // Checking the gate in a separate read would race this exact boundary.
  let announceClaim,releaseClaim;
  const claimReached=new Promise(resolve=>{announceClaim=resolve;});
  const claimReleased=new Promise(resolve=>{releaseClaim=resolve;});
  const delayedDb={prepare:sql=>db.prepare(sql),async batch(statements){
    announceClaim();await claimReleased;return db.batch(statements);
  }};
  const inflight=agent({action:'poll'},s.secret,{...s,db:delayedDb});
  await claimReached;
  let paused;
  try {
    paused=await agent({action:'maintenance',mode:'pause',duration:86400});
    assert.deepEqual(paused,{status:200,server_time:base,paused_until:base+180,active_jobs:1,paused:true});
  } finally {releaseClaim();}
  assert.equal((await inflight).job,null);
  assert.equal((await agent({action:'poll'})).job,null);
  assert.equal((await db.prepare('SELECT status FROM jobs WHERE id=?').bind(pendingId).first()).status,'pending');

  assert.equal((await agent({action:'heartbeat',id:active.id,lease:active.lease})).acknowledged,true);
  assert.equal((await agent({action:'maintenance',mode:'status'})).active_jobs,1);
  assert.equal((await agent({action:'complete',id:active.id,lease:active.lease,result:{ok:true,result:{marker:'synthetic'}}})).acknowledged,true);
  assert.equal((await agent({action:'maintenance',mode:'status'})).active_jobs,0);
  const inspect=await call('get_operation',{operation_id:pendingId});
  assert.equal(inspect.value.status,'pending');
  assert.equal(inspect.value.online,true);

  // A new submission remains durable while claims are paused. It is never
  // reported as completed, dropped, or rejected as an offline connector.
  const delayed=await call('save_memory',{request_key:'synthetic-maintenance-save',packet:{
    format:'threadsatchel/1',kind:'note',messages:[{text:'Synthetic maintenance fixture'}]
  }});
  assert.equal(delayed.rpc.result.isError,false);
  assert.equal(delayed.value.status,'pending');
  assert.equal(delayed.value.online,true);
  const durable=await db.prepare('SELECT * FROM jobs WHERE id=?').bind(delayed.value.operation_id).first();
  assert.equal(durable.status,'pending');
  assert.equal(JSON.parse(durable.input_json).args.request_key,'synthetic-maintenance-save');
  assert.equal(await bucket.get(`${durable.id}/input-${durable.created}-${durable.digest}`),null);

  const resumed=await agent({action:'maintenance',mode:'resume'});
  assert.deepEqual(resumed,{status:200,server_time:base,paused_until:null,active_jobs:0,paused:false});
  const afterResume=(await agent({action:'poll'})).job;
  assert.equal(afterResume.id,pendingId);
  await agent({action:'complete',id:afterResume.id,lease:afterResume.lease,result:{ok:true,result:{}}});

  await agent({action:'maintenance',mode:'pause'});
  Date.now=()=>(base+180)*1000;
  assert.equal((await agent({action:'poll'})).job,null); // Inclusive expiry matches status.
  assert.equal((await agent({action:'maintenance',mode:'status'})).paused,true);
  Date.now=()=>(base+181)*1000;
  assert.equal((await agent({action:'maintenance',mode:'status'})).paused,false);
  const afterExpiry=(await agent({action:'poll'})).job;
  assert.equal(afterExpiry.id,durable.id);
  assert.equal((await agent({action:'maintenance',mode:'status'})).active_jobs,1);

  // Expired leases do not prevent a drain, and maintenance does not run cleanup.
  await db.prepare('UPDATE jobs SET lease_until=? WHERE id=?').bind(base+180,durable.id).run();
  const retainedId=await queue('expired-maintenance-fixture',base-60);
  await db.prepare("UPDATE jobs SET status='leased',lease='synthetic',lease_until=?,expires=? WHERE id=?")
    .bind(base+500,base+180,retainedId).run();
  assert.equal((await agent({action:'maintenance',mode:'status'})).active_jobs,0);
  assert.ok(await db.prepare('SELECT id FROM jobs WHERE id=?').bind(retainedId).first());
  await db.prepare("UPDATE state SET value='invalid' WHERE key='claims_paused_until'").run();
  assert.equal((await agent({action:'maintenance',mode:'status'})).status,503);
  assert.equal((await agent({action:'maintenance',mode:'resume'})).paused,false);
  console.log('PASS: authenticated maintenance, atomic in-flight claim gate, drain without blocking completion/heartbeat, durable pending submissions, explicit resume, automatic expiry, and fail-closed metadata.');
} finally {
  Date.now=realNow;
  await mf.dispose();
}
