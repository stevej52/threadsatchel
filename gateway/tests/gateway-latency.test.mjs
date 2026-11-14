import assert from 'node:assert/strict';
import {readFileSync,readdirSync,writeFileSync,mkdirSync} from 'node:fs';
import {Miniflare} from 'miniflare';
import ts from 'typescript';

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
const realNow=Date.now,performanceDescriptor=Object.getOwnPropertyDescriptor(performance,'now');
const realPerformanceNow=performance.now.bind(performance);
const originalInfo=console.info,originalWarn=console.warn;
const logs=[];
let epoch=Math.floor(realNow()/1000),elapsedOffset=0;
Date.now=()=>epoch*1000;
Object.defineProperty(performance,'now',{configurable:true,value:()=>realPerformanceNow()+elapsedOffset});
console.info=value=>logs.push(JSON.parse(value));
console.warn=value=>logs.push(JSON.parse(value));

// Count binding round trips without replacing D1's transaction semantics.
function trackedDb(db) {
  const statements=new WeakMap(),calls=[];
  function wrap(statement,sql) {
    const proxy={
      bind(...args){return wrap(statement.bind(...args),sql);},
      async first(...args){calls.push({kind:'first',sql});return statement.first(...args);},
      async all(...args){calls.push({kind:'all',sql});return statement.all(...args);},
      async run(...args){calls.push({kind:'run',sql});return statement.run(...args);}
    };
    statements.set(proxy,{statement,sql});
    return proxy;
  }
  return {calls,db:{
    prepare(sql){return wrap(db.prepare(sql),sql);},
    async batch(items){
      calls.push({kind:'batch',sql:items.map(item=>statements.get(item).sql)});
      return db.batch(items.map(item=>statements.get(item).statement));
    }
  }};
}

try {
  const db=await mf.getD1Database('DB'),bucket=await mf.getR2Bucket('BUCKET');
  const secret='synthetic-test-secret-not-for-production';
  for(const filename of readdirSync('migrations').filter(x=>x.endsWith('.sql'))) {
    for(const statement of readFileSync('migrations/'+filename,'utf8').split('--> statement-breakpoint').map(x=>x.trim()).filter(Boolean)) await db.prepare(statement).run();
  }
  const tracked=trackedDb(db),background=[];
  // Keep this suite's object-generation races on the fallback storage path.
  // gateway-inline.test.mjs exercises inline and mixed-mode transfer races.
  const s={db:tracked.db,bucket,secret,inlineWrites:false,defer:task=>background.push(task)};
  const agent=async(payload,services=s)=>{
    const response=await handleAgent(new Request('https://example.test/api/agent',{
      method:'POST',headers:{'X-ThreadSatchel-Key':secret},body:JSON.stringify(payload)
    }),services);
    return {status:response.status,...await response.json()};
  };
  const call=async(name,args={},services=s,user='synthetic-owner')=>{
    const response=await handleMcp(new Request('https://example.test/mcp',{
      method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}})
    }),services,user);
    const rpc=await response.json();
    return {status:response.status,rpc,value:rpc.result?JSON.parse(rpc.result.content[0].text):null};
  };
  const packet={format:'threadsatchel/1',kind:'note',messages:[{text:'PRIVATE-SYNTHETIC-CONTENT-DO-NOT-LOG'}]};
  const args=key=>({request_key:key,packet});
  const operationId=key=>digest('synthetic-owner\nsave_memory\n'+key);
  async function waitForRow(id) {
    for(let attempt=0;attempt<100;attempt++) {
      const row=await db.prepare('SELECT * FROM jobs WHERE id=?').bind(id).first();
      if(row) return row;
      await new Promise(resolve=>setTimeout(resolve,10));
    }
    throw new Error('Synthetic job was not inserted');
  }
  async function finish(pending,id,services=s) {
    await waitForRow(id);
    const received=(await agent({action:'poll'},services)).job;
    assert.equal(received.id,id);
    await agent({action:'complete',id,lease:received.lease,result:{ok:true,result:{saved:true}}},services);
    return pending;
  }

  assert.equal((await agent({action:'poll'})).job,null);
  assert.equal(tracked.calls.length,1);
  assert.equal(tracked.calls[0].kind,'batch');
  assert.equal(tracked.calls[0].sql.length,2);
  assert.equal(background.length,1);
  await agent({action:'poll'});
  assert.equal(background.length,1); // Scheduling is single-flight until completion.
  assert.ok(tracked.calls.every(item=>item.kind==='batch'));
  await background.shift()();
  assert.equal(tracked.calls.filter(item=>item.kind==='all').length,1);
  await agent({action:'poll'});
  assert.equal(background.length,0);
  elapsedOffset+=60001;
  await agent({action:'poll'});
  assert.equal(background.length,1);
  await background.shift()();

  tracked.calls.length=0;
  const pending=call('save_memory',args('batched-delivery'));
  const freshId=await operationId('batched-delivery');
  const fresh=await finish(pending,freshId);
  assert.equal(fresh.value.status,'completed');
  const batches=tracked.calls.filter(item=>item.kind==='batch');
  assert.deepEqual(batches.map(item=>item.sql.length).sort(),[2,2,2,3]);
  assert.equal(batches.filter(item=>item.sql[0].includes("VALUES('owner'")).length,1);
  assert.equal(tracked.calls.filter(item=>item.kind==='all').length,0);
  const freshRow=await db.prepare('SELECT * FROM jobs WHERE id=?').bind(freshId).first();
  assert.ok(await bucket.get(`${freshId}/input-${freshRow.created}-${freshRow.digest}`));
  assert.equal(await bucket.get(`${freshId}/input-${freshRow.digest}`),null);
  const denied=await call('get_operation',{operation_id:freshId},s,'another-owner');
  assert.equal(denied.status,403);

  // Deployments must still claim and return legacy queued payloads.
  const legacyId=await digest('legacy-queued-input'),legacyPayload=JSON.stringify({action:'get_memory',args:{id:'synthetic'}});
  const legacyDigest=await digest(legacyPayload);
  await bucket.put(`${legacyId}/input-${legacyDigest}`,legacyPayload);
  await db.prepare("INSERT INTO jobs(id,owner,action,digest,status,created,expires) VALUES(?,?,?,?,'pending',?,?)")
    .bind(legacyId,'synthetic-owner','get_memory',legacyDigest,epoch-1,epoch+86400).run();
  const legacy=(await agent({action:'poll'})).job;
  assert.equal(legacy.id,legacyId);
  assert.equal(legacy.payload,legacyPayload);
  await agent({action:'complete',id:legacyId,lease:legacy.lease,result:{ok:true,result:{}}});

  // Two identical calls see no prior job, but choose different creation seconds.
  const concurrentArgs=args('concurrent-seconds'),concurrentId=await operationId(concurrentArgs.request_key);
  let announcePut,releasePut,firstKey;
  const putReached=new Promise(resolve=>{announcePut=resolve;});
  const putReleased=new Promise(resolve=>{releasePut=resolve;});
  const firstServices={...s,bucket:{
    get:key=>bucket.get(key),delete:key=>bucket.delete(key),
    async put(key,value,options){
      const result=await bucket.put(key,value,options);
      firstKey=key;announcePut();await putReleased;return result;
    }
  }};
  const first=call('save_memory',concurrentArgs,firstServices);
  await putReached;
  epoch++;
  const second=call('save_memory',concurrentArgs);
  const secondResult=await finish(second,concurrentId);
  releasePut();
  const firstResult=await first;
  assert.equal(firstResult.value.operation_id,secondResult.value.operation_id);
  assert.equal(firstResult.value.status,'completed');
  const winning=await db.prepare('SELECT * FROM jobs WHERE id=?').bind(concurrentId).first();
  const winnerKey=`${concurrentId}/input-${winning.created}-${winning.digest}`;
  assert.notEqual(firstKey,winnerKey);
  assert.equal(await bucket.get(firstKey),null);
  assert.ok(await bucket.get(winnerKey));

  // A stale sweep from another isolate must not delete a new incarnation.
  const expiredArgs=args('expired-reused-key'),expiredId=await operationId(expiredArgs.request_key);
  const expiredPayload=JSON.stringify({action:'save_memory',args:expiredArgs});
  const expiredDigest=await digest(expiredPayload),oldCreated=epoch-86401;
  const oldKey=`${expiredId}/input-${oldCreated}-${expiredDigest}`,oldLegacy=`${expiredId}/input-${expiredDigest}`;
  await bucket.put(oldKey,expiredPayload);await bucket.put(oldLegacy,expiredPayload);
  await db.prepare("INSERT INTO jobs(id,owner,action,digest,status,created,expires) VALUES(?,?,?,?,'done',?,?)")
    .bind(expiredId,'synthetic-owner','save_memory',expiredDigest,oldCreated,oldCreated+86400).run();
  const staleTracked=trackedDb(db),staleTasks=[];
  let announceDelete,releaseDelete;
  const deleteReached=new Promise(resolve=>{announceDelete=resolve;});
  const deleteReleased=new Promise(resolve=>{releaseDelete=resolve;});
  const staleServices={db:staleTracked.db,secret,defer:task=>staleTasks.push(task),bucket:{
    get:key=>bucket.get(key),put:(...values)=>bucket.put(...values),
    async delete(keys){announceDelete();await deleteReleased;return bucket.delete(keys);}
  }};
  for(let index=0;index<2;index++) {
    const expired=await call('save_memory',expiredArgs,staleServices);
    assert.equal(expired.rpc.result.isError,true);
    assert.equal(expired.value.error_detail.code,'operation_unavailable');
  }
  assert.equal(staleTasks.length,1);
  const staleWork=staleTasks.shift()();
  await deleteReached;
  // Another isolate finishes the same old sweep before this one's object call.
  await bucket.delete([oldKey,oldLegacy]);
  await db.prepare('DELETE FROM jobs WHERE id=? AND created=?').bind(expiredId,oldCreated).run();
  const reused=call('save_memory',expiredArgs);
  const replacement=await waitForRow(expiredId);
  const replacementKey=`${expiredId}/input-${replacement.created}-${replacement.digest}`;
  releaseDelete();await staleWork;
  assert.ok(await bucket.get(replacementKey));
  assert.equal((await db.prepare('SELECT created FROM jobs WHERE id=?').bind(expiredId).first()).created,epoch);
  assert.equal((await finish(reused,expiredId)).value.status,'completed');

  // Cleanup failures never turn a valid foreground operation into failure.
  const failedTasks=[];
  const failedDb={batch:items=>db.batch(items),prepare(sql){
    if(sql.startsWith('SELECT id,digest,created,result_key FROM jobs WHERE expires <')) return {bind(){return this;},async all(){throw new Error('PRIVATE-CLEANUP-ERROR');}};
    return db.prepare(sql);
  }};
  const failedServices={db:failedDb,bucket,secret,defer:task=>failedTasks.push(task)};
  assert.equal((await agent({action:'poll'},failedServices)).status,200);
  await failedTasks.shift()();
  assert.ok(logs.some(item=>item.event==='cleanup_failed'));
  assert.equal((await call('connection_status',{},failedServices)).value.online,true);

  // A backlog is processed in bounded batches without imposing another minute
  // of retention after a full pass; current rows and payloads remain untouched.
  const expiredRows=await Promise.all(Array.from({length:51},async(_,index)=>({
    id:await digest('cleanup-backlog-'+index),created:epoch-90000
  })));
  await db.batch(expiredRows.map(row=>db.prepare("INSERT INTO jobs(id,owner,action,digest,status,created,expires) VALUES(?,?,?,?,'done',?,?)")
    .bind(row.id,'synthetic-owner','get_memory','synthetic',row.created,epoch-1)));
  const backlog=trackedDb(db),backlogTasks=[];
  const backlogServices={db:backlog.db,bucket,secret,defer:task=>backlogTasks.push(task)};
  await agent({action:'poll'},backlogServices);
  await backlogTasks.shift()();
  assert.equal((await db.prepare('SELECT count(*) AS n FROM jobs WHERE expires < ?').bind(epoch).first()).n,1);
  await agent({action:'poll'},backlogServices);
  assert.equal(backlogTasks.length,1);
  await backlogTasks.shift()();
  assert.equal((await db.prepare('SELECT count(*) AS n FROM jobs WHERE expires < ?').bind(epoch).first()).n,0);
  await agent({action:'poll'},backlogServices);
  assert.equal(backlogTasks.length,0);
  assert.ok(await bucket.get(replacementKey));

  await db.prepare("UPDATE state SET value='0' WHERE key='heartbeat'").run();
  const countBefore=(await db.prepare('SELECT count(*) AS n FROM jobs').first()).n;
  const objectCountBefore=(await bucket.list()).objects.length;
  const offline=await call('save_memory',args('offline-no-submit'));
  assert.equal(offline.rpc.result.isError,true);
  assert.equal((await db.prepare('SELECT count(*) AS n FROM jobs').first()).n,countBefore);
  assert.equal((await bucket.list()).objects.length,objectCountBefore);
  const logText=JSON.stringify(logs);
  for(const privateValue of ['PRIVATE-SYNTHETIC-CONTENT-DO-NOT-LOG','PRIVATE-CLEANUP-ERROR',secret,expiredId,concurrentId]) assert.ok(!logText.includes(privateValue));
  const timings=logs.filter(item=>item.event==='gateway_timing');
  assert.ok(timings.some(item=>item.route==='agent'&&item.action==='poll'));
  assert.ok(timings.some(item=>item.route==='agent'&&item.action==='complete'));
  for(const item of timings) {
    for(const [key,value] of Object.entries(item)) {
      if(!['component','event','route','action'].includes(key)) {
        assert.ok(key.endsWith('_ms'));
        assert.ok(Number.isFinite(value)&&value>=0);
      }
    }
  }
  originalInfo('PASS: D1 batching, deferred/throttled cleanup, legacy payloads, concurrent generation candidates, stale-sweep race safety, expiry retry contract, offline no-submit, and metadata-only timing.');
} finally {
  Date.now=realNow;
  if(performanceDescriptor) Object.defineProperty(performance,'now',performanceDescriptor);
  else delete performance.now;
  console.info=originalInfo;console.warn=originalWarn;
  await mf.dispose();
}
