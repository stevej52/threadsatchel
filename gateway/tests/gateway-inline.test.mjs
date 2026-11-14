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
const {handleAgent,handleMcp,digest,INLINE_TRANSFER_MAX_BYTES}=await import('../.test-build/gateway-test.mjs');
const mf=new Miniflare({modules:true,script:'export default {fetch(){return new Response("test")}}',
  compatibilityDate:'2026-05-22',d1Databases:['DB'],r2Buckets:['BUCKET']});
const originalInfo=console.info,originalWarn=console.warn,logs=[];
console.info=value=>logs.push(JSON.parse(value));
console.warn=value=>logs.push(JSON.parse(value));
const owner='synthetic-owner',secret='synthetic-inline-secret-not-for-production';
const bytes=value=>Buffer.byteLength(value,'utf8');
const packet=text=>({format:'threadsatchel/1',kind:'note',messages:[{text}]});
const saveArgs=(key,text)=>({request_key:key,packet:packet(text)});
const serialize=args=>JSON.stringify({action:'save_memory',args});
function inputAtBytes(key,size,prefix='') {
  const args=saveArgs(key,prefix);
  args.packet.messages[0].text+='x'.repeat(size-bytes(serialize(args)));
  assert.equal(bytes(serialize(args)),size);
  return args;
}
function resultAtBytes(size,prefix='') {
  const value={ok:true,result:{text:prefix}};
  value.result.text+='x'.repeat(size-bytes(JSON.stringify(value)));
  assert.equal(bytes(JSON.stringify(value)),size);
  return value;
}
function barrier() {
  let reached,release;
  const arrival=new Promise(resolve=>{reached=resolve;});
  const released=new Promise(resolve=>{release=resolve;});
  return {reached,release,arrival,released};
}

try {
  const db=await mf.getD1Database('DB'),bucket=await mf.getR2Bucket('BUCKET');
  const background=[],bucketCalls=[];
  const s={db,secret,defer:task=>background.push(task),bucket:{
    async put(...args){bucketCalls.push({kind:'put',key:args[0]});return bucket.put(...args);},
    async get(key){bucketCalls.push({kind:'get',key});return bucket.get(key);},
    async delete(key){bucketCalls.push({kind:'delete',key});return bucket.delete(key);}
  }};
  const rollback={...s,inlineWrites:false};
  const agent=async(payload,services=s)=>{
    const response=await handleAgent(new Request('https://example.test/api/agent',{
      method:'POST',headers:{'X-ThreadSatchel-Key':secret},body:JSON.stringify(payload)
    }),services);
    return {status:response.status,...await response.json()};
  };
  const call=async(name,args={},services=s,user=owner)=>{
    const response=await handleMcp(new Request('https://example.test/mcp',{
      method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}})
    }),services,user);
    const rpc=await response.json();
    return {status:response.status,rpc,value:rpc.result?JSON.parse(rpc.result.content[0].text):null};
  };
  const idFor=key=>digest(owner+'\nsave_memory\n'+key);
  const rowFor=id=>db.prepare('SELECT * FROM jobs WHERE id=?').bind(id).first();
  async function waitRow(id) {
    for(let i=0;i<200;i++) {
      const row=await rowFor(id);if(row) return row;
      await new Promise(resolve=>setTimeout(resolve,10));
    }
    throw new Error('Fixture did not queue');
  }
  const inputKey=row=>`${row.id}/input-${row.created}-${row.digest}`;
  async function start(args,services=s) {
    const id=await idFor(args.request_key),pending=call('save_memory',args,services);
    const row=await waitRow(id);
    return {id,pending,row};
  }
  async function finish(queued,result,services=s) {
    const job=(await agent({action:'poll'},services)).job;
    assert.equal(job.id,queued.id);
    const ack=await agent({action:'complete',id:job.id,lease:job.lease,result},services);
    assert.equal(ack.acknowledged,true);
    return {response:await queued.pending,job,row:await rowFor(job.id)};
  }

  // Additive migration must preserve a job created by the previous binary.
  const migrations=readdirSync('migrations').filter(x=>x.endsWith('.sql')).sort();
  async function migrate(filename) {
    for(const statement of readFileSync('migrations/'+filename,'utf8').split('--> statement-breakpoint').map(x=>x.trim()).filter(Boolean)) await db.prepare(statement).run();
  }
  await migrate(migrations[0]);
  const legacyId=await digest('before-inline-migration'),legacyPayload=JSON.stringify({action:'get_memory',args:{id:'legacy'}});
  const legacyHash=await digest(legacyPayload),epoch=Math.floor(Date.now()/1000);
  await db.prepare("INSERT INTO jobs(id,owner,action,digest,status,created,expires) VALUES(?,?,?,?,'pending',?,?)")
    .bind(legacyId,owner,'get_memory',legacyHash,epoch-1,epoch+86400).run();
  await bucket.put(`${legacyId}/input-${legacyHash}`,legacyPayload);
  for(const migration of migrations.slice(1)) await migrate(migration);
  assert.equal((await rowFor(legacyId)).input_json,null);
  assert.equal((await rowFor(legacyId)).result_json,null);
  const legacy=(await agent({action:'poll'},rollback)).job;
  assert.equal(legacy.payload,legacyPayload);
  assert.equal(legacy.sha256,legacyHash);
  await agent({action:'complete',id:legacy.id,lease:legacy.lease,result:{ok:true,result:{legacy:true}}},rollback);
  assert.equal((await call('get_operation',{operation_id:legacy.id})).value.result.legacy,true);
  await background.shift()();

  // Threshold is serialized UTF-8 bytes, including JSON escaping and envelope.
  // Use both ASCII and multibyte strings at exactly 64 KiB and one byte above.
  for(const prefix of ['', 'ðŸ™‚æ¼¢Ã©\r\n"\\\u0000']) {
    for(const extra of [0,1]) {
      const key=`boundary-${prefix?'unicode':'ascii'}-${extra}`;
      const args=inputAtBytes(key,INLINE_TRANSFER_MAX_BYTES+extra,prefix);
      const result=resultAtBytes(INLINE_TRANSFER_MAX_BYTES+extra,prefix);
      const before=bucketCalls.length;
      const queued=await start(args),expected=serialize(args);
      assert.equal(queued.row.input_json,extra?null:expected);
      assert.equal(queued.row.digest,await digest(expected));
      const finished=await finish(queued,result);
      assert.equal(finished.job.payload,expected);
      assert.equal(finished.job.sha256,await digest(expected));
      assert.deepEqual(JSON.parse(finished.job.payload).args.packet,args.packet);
      // Storage remains exact at the old transport boundary, while public reads
      // no longer echo an unmetered 64KiB legacy result into model context.
      assert.equal(finished.response.value.ok,false);
      assert.equal(finished.response.value.error_detail.code,'result_too_large');
      assert.equal(finished.row.result_json,extra?null:JSON.stringify(result));
      assert.equal(finished.row.result_key===null,!extra);
      const io=bucketCalls.slice(before);
      if(!extra) assert.deepEqual(io,[]); // No transfer object calls for small values.
      else {
        assert.ok(io.some(x=>x.kind==='put'&&x.key===inputKey(finished.row)));
        assert.ok(await bucket.get(finished.row.result_key));
      }
      // Compatible rollback code continues reading both inline fields.
      assert.equal((await call('get_operation',{operation_id:queued.id},rollback)).value.error_detail.code,'result_too_large');
    }
  }

  const exact='PRIVATE-INLINE-FIXTURE: cafÃ© e\u0301 æ¼¢å­— ðŸ™‚\r\nline two\n"quoted"\\path\u0000';
  const exactArgs=saveArgs('exact-and-rollback',exact),exactQueued=await start(exactArgs);
  // Disabled inline writers can claim an already-inline job and store an R2 receipt.
  const exactResult={ok:true,result:{text:exact}};
  const exactFinished=await finish(exactQueued,exactResult,rollback);
  assert.equal(JSON.parse(exactFinished.job.payload).args.packet.messages[0].text,exact);
  assert.equal(exactFinished.row.result_json,null);
  assert.ok(exactFinished.row.result_key);
  assert.equal(exactFinished.response.value.result.text,exact);
  const repeated=await call('save_memory',exactArgs);
  assert.equal(repeated.value.operation_id,exactQueued.id);
  assert.equal(repeated.value.result.text,exact);
  const conflicted=await call('save_memory',saveArgs('exact-and-rollback',exact+'changed'));
  assert.equal(conflicted.value.error_detail.code,'request_conflict');
  assert.equal((await call('get_operation',{operation_id:exactQueued.id},s,'other-owner')).status,403);
  const smallFallback=await start(saveArgs('rollback-new-write','small'),rollback);
  assert.equal(smallFallback.row.input_json,null);
  assert.ok(await bucket.get(inputKey(smallFallback.row)));
  await finish(smallFallback,{ok:true,result:{saved:true}},rollback);

  // A failed inline receipt is cleared before an explicit same-key retry.
  const retryArgs=saveArgs('retry-inline-failure','retry fixture'),retry=await start(retryArgs);
  await finish(retry,{ok:false,error:'synthetic failure'});
  assert.ok((await rowFor(retry.id)).result_json);
  const retried=await call('save_memory',retryArgs);
  assert.equal(retried.value.status,'pending');
  const retryRow=await rowFor(retry.id);
  assert.equal(retryRow.result_json,null);assert.equal(retryRow.result_key,null);
  assert.equal(retryRow.input_json,serialize(retryArgs));
  const retryJob=(await agent({action:'poll'})).job;
  await agent({action:'complete',id:retryJob.id,lease:retryJob.lease,result:{ok:true,result:{saved:true}}});
  assert.equal((await call('get_operation',{operation_id:retry.id})).value.ok,true);

  // A delayed second retry observed the old failure before the first retry ran.
  // It must not clear a successful receipt produced under the newer lease.
  const raceArgs=saveArgs('failed-retry-race','retry race'),race=await start(raceArgs);
  await finish(race,{ok:false,error:'old failure'});
  const resetGate=barrier();
  const delayedResetDb={batch:items=>db.batch(items),prepare(sql){
    if(!sql.startsWith("UPDATE jobs SET status='pending'")) return db.prepare(sql);
    return {bind(...values){return {async first(){resetGate.reached();await resetGate.released;return db.prepare(sql).bind(...values).first();}};}};
  }};
  const delayedRetry=call('save_memory',raceArgs,{...s,db:delayedResetDb});
  await resetGate.arrival;
  assert.equal((await call('save_memory',raceArgs)).value.status,'pending');
  const racingJob=(await agent({action:'poll'})).job;
  await agent({action:'complete',id:racingJob.id,lease:racingJob.lease,result:{ok:true,result:{latest:true}}});
  resetGate.release();
  assert.equal((await delayedRetry).value.result.latest,true);
  assert.equal((await rowFor(race.id)).status,'done');
  assert.equal(JSON.parse((await rowFor(race.id)).result_json).result.latest,true);

  // Mixed writers can both pass preflight. Pause one before INSERT, allowing the
  // other representation to win, in the same second as well as a later one.
  for(const winnerInline of [true,false]) {
    const gate=barrier(),args=saveArgs('mixed-input-'+winnerInline,'same candidate'),id=await idFor(args.request_key);
    const raw=new WeakMap();
    function wrap(statement,sql) {
      const proxy={bind(...values){return wrap(statement.bind(...values),sql);},first:(...v)=>statement.first(...v),all:(...v)=>statement.all(...v),run:(...v)=>statement.run(...v)};
      raw.set(proxy,{statement,sql});return proxy;
    }
    const delayedDb={prepare:sql=>wrap(db.prepare(sql),sql),async batch(statements){
      if(statements.some(st=>raw.get(st).sql.startsWith('INSERT OR IGNORE INTO jobs'))) {gate.reached();await gate.released;}
      return db.batch(statements.map(st=>raw.get(st).statement));
    }};
    const losing=call('save_memory',args,{...s,db:delayedDb,inlineWrites:!winnerInline});
    await gate.arrival;
    const winner=await start(args,{...s,inlineWrites:winnerInline});
    const won=await finish(winner,{ok:true,result:{winnerInline}});
    gate.release();
    assert.equal((await losing).value.operation_id,id);
    const row=await rowFor(id);
    assert.equal(row.input_json!==null,winnerInline);
    assert.equal(Boolean(await bucket.get(inputKey(row))),!winnerInline);
    assert.equal(won.response.value.status,'completed');
  }

  // Duplicate completion races: losing attempts must not remove the committed
  // receipt, whether the winner stores inline or in its own fallback object.
  for(const winnerInline of [true,false]) {
    const queued=await start(saveArgs('completion-race-'+winnerInline,'receipt race'));
    const job=(await agent({action:'poll'})).job,gate=barrier();
    let candidateKey;
    const slow={...rollback,bucket:{...s.bucket,async put(key,...values){
      candidateKey=key;const result=await bucket.put(key,...values);gate.reached();await gate.released;return result;
    }}};
    const losing=agent({action:'complete',id:job.id,lease:job.lease,result:{ok:true,result:{winner:false}}},slow);
    await gate.arrival;
    const won=await agent({action:'complete',id:job.id,lease:job.lease,result:{ok:true,result:{winner:true}}},{...s,inlineWrites:winnerInline});
    assert.equal(won.acknowledged,true);gate.release();
    assert.equal((await losing).acknowledged,true);
    assert.equal(await bucket.get(candidateKey),null);
    const row=await rowFor(job.id);
    assert.equal(row.result_json!==null,winnerInline);
    if(!winnerInline) assert.ok(await bucket.get(row.result_key));
    assert.equal((await queued.pending).value.result.winner,true);
    assert.equal((await call('get_operation',{operation_id:job.id})).value.result.winner,true);
  }

  // A fallback PUT can outlive its lease. Revalidate at the SQL commit boundary.
  const expiring=await start(saveArgs('completion-expired-lease','lease fixture'));
  const expiringJob=(await agent({action:'poll'})).job,gate=barrier();
  let staleKey;
  const late=agent({action:'complete',id:expiringJob.id,lease:expiringJob.lease,result:{ok:true}},
    {...rollback,bucket:{...s.bucket,async put(key,...values){staleKey=key;const result=await bucket.put(key,...values);gate.reached();await gate.released;return result;}}});
  await gate.arrival;
  await db.prepare('UPDATE jobs SET lease_until=? WHERE id=?').bind(epoch-1,expiring.id).run();
  gate.release();assert.equal((await late).status,409);
  assert.equal(await bucket.get(staleKey),null);
  const newLease=(await agent({action:'poll'})).job;
  assert.notEqual(newLease.lease,expiringJob.lease);
  await agent({action:'complete',id:newLease.id,lease:newLease.lease,result:{ok:true,result:{saved:true}}});
  await expiring.pending;

  // Expired inline content stays unavailable while deferred cleanup is pending,
  // then the old row can disappear without changing the next submission's text.
  const expiredArgs=saveArgs('expired-inline','expired body'),expired=await start(expiredArgs);
  await finish(expired,{ok:true,result:{text:'expired result'}});
  await db.prepare('UPDATE jobs SET created=?,expires=? WHERE id=?').bind(epoch-86401,epoch-1,expired.id).run();
  const cleanupTasks=[],cleanupSql=[];
  const cleanupDb={batch:items=>db.batch(items),prepare(sql){cleanupSql.push(sql);return db.prepare(sql);}};
  const cleanupServices={...s,db:cleanupDb,defer:task=>cleanupTasks.push(task)};
  assert.equal((await call('get_operation',{operation_id:expired.id},cleanupServices)).value.error_detail.code,'operation_unavailable');
  assert.equal((await call('save_memory',expiredArgs,cleanupServices)).value.error_detail.code,'operation_unavailable');
  await cleanupTasks.shift()();
  assert.equal(await rowFor(expired.id),null);
  assert.ok(cleanupSql.includes('SELECT id,digest,created,result_key FROM jobs WHERE expires < ? LIMIT 50'));
  const replacement=await start(expiredArgs);
  await finish(replacement,{ok:true,result:{saved:true}});

  await db.prepare("UPDATE state SET value='0' WHERE key='heartbeat'").run();
  const offlineArgs=saveArgs('inline-offline','must not submit');
  const offline=await call('save_memory',offlineArgs);
  assert.equal(offline.value.error_detail.code,'temporarily_unavailable');
  assert.equal(await rowFor(await idFor(offlineArgs.request_key)),null);
  const logText=JSON.stringify(logs);
  for(const privateValue of [exact,'PRIVATE-INLINE-FIXTURE',secret,expired.id]) assert.ok(!logText.includes(privateValue));
  originalInfo('PASS: additive migration; inline UTF-8 byte boundaries; exact text/hash; zero small-transfer R2 calls; fallback and legacy readers; compatible rollback; same-key retry; mixed-writer and completion races; lease expiry; owner isolation; deferred expiry; offline no-submit; metadata-only logs.');
} finally {
  console.info=originalInfo;console.warn=originalWarn;
  await mf.dispose();
}
