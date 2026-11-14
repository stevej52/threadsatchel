import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import ts from 'typescript';

mkdirSync('.test-build', {recursive:true});
for(const name of ['packet-validation','tool-contract','gateway']) {
  const code=ts.transpileModule(readFileSync('lib/'+name+'.ts','utf8'),{
    compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}
  }).outputText.replaceAll('"./tool-contract"','"./tool-contract-test.mjs"')
    .replaceAll('"./packet-validation"','"./packet-validation-test.mjs"');
  writeFileSync('.test-build/'+name+'-test.mjs',code);
}
const {handleMcp}=await import('../.test-build/gateway-test.mjs');

// Exercise the public request handler with controlled storage latency and time.
// These fixtures perform no network I/O and keep every queued input in memory.
async function scenario(options, check) {
  let elapsed=0, wallOffset=0, job;
  const owner='synthetic-owner';
  const objects=new Map();
  const reads=[], sleeps=[];
  let heartbeatReads=0, resultReads=0, jobInserts=0;
  const originalNow=Date.now;
  const originalTimeout=globalThis.setTimeout;
  const nowDescriptor=Object.getOwnPropertyDescriptor(performance,'now');
  const wall=originalNow();
  Date.now=()=>wall+wallOffset;
  Object.defineProperty(performance,'now',{configurable:true,value:()=>elapsed});
  globalThis.setTimeout=(callback,ms)=>{
    sleeps.push(ms);
    elapsed+=ms;
    wallOffset+=options.wallJumpPerSleep??ms;
    queueMicrotask(callback);
    return 0;
  };
  const db={async batch(statements){
    const result=[];
    for(const statement of statements) {
      if(statement.sql.startsWith('SELECT')) {
        const row=await statement.first();
        result.push({results:row?[row]:[]});
      } else { await statement.run(); result.push({results:[]}); }
    }
    return result;
  },prepare(sql){
    let values=[];
    return {
      sql,
      bind(...args){values=args;return this;},
      async run(){
        if(sql.startsWith("INSERT OR IGNORE INTO state")) return {success:true};
        if(sql.startsWith('INSERT OR IGNORE INTO jobs')) {
          jobInserts++;
          const [id,user,action,digest,created,expires]=values;
          job={id,owner:user,action,digest,created,expires,status:'pending',result_key:null};
          return {success:true};
        }
        throw new Error('Unexpected write: '+sql);
      },
      async all(){
        assert.ok(sql.startsWith('SELECT id,digest,created,result_key FROM jobs WHERE expires <'));
        return {results:[]};
      },
      async first(){
        if(sql==="SELECT value FROM state WHERE key='owner'") return {value:owner};
        if(sql==="SELECT value FROM state WHERE key='heartbeat'") {
          heartbeatReads++;
          const age=options.offlineAtEnd&&elapsed>=12000?91:0;
          return {value:String(Math.floor(Date.now()/1000)-age)};
        }
        if(sql.startsWith('SELECT count(*) AS n FROM jobs')) return {n:0};
        if(sql==='SELECT digest,result_key,lease FROM jobs WHERE id=?') return job&&job.id===values[0]?{...job}:null;
        if(sql==='SELECT digest,created,input_json IS NOT NULL AS input_inline FROM jobs WHERE id=?') return {digest:job.digest,created:job.created,input_inline:0};
        if(sql==='SELECT status,expires,result_key,result_json FROM jobs WHERE id=? AND owner=?') {
          reads.push(elapsed);
          elapsed+=options.readLatency??0;
          if(!job||job.id!==values[0]||job.owner!==values[1]) return null;
          if(elapsed>=(options.readyAt??Infinity)) {
            job.status='done';
            job.result_key=job.id+'/synthetic-result';
          }
          return {...job};
        }
        throw new Error('Unexpected read: '+sql);
      }
    };
  }};
  const services={db,inlineWrites:false,secret:'synthetic-secret-not-used-by-mcp',bucket:{
    async put(key,value){objects.set(key,value);},
    async get(key){
      resultReads++;
      assert.equal(key,job.result_key);
      return {async json(){return {ok:true,result:{saved:true,marker:'synthetic'}};}};
    },
    async delete(){throw new Error('Unexpected object deletion');}
  }};
  const call=async(name,args,user=owner)=>{
    const response=await handleMcp(new Request('https://example.test/mcp',{
      method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}})
    }),services,user);
    const rpc=await response.json();
    return {status:response.status,rpc,value:rpc.result?JSON.parse(rpc.result.content[0].text):null};
  };
  try {
    const result=await call('save_memory',{request_key:'synthetic-wait',packet:{
      format:'threadsatchel/1',kind:'note',messages:[{text:'Synthetic wait regression'}]
    }});
    await check({result,call,reads,sleeps,objects,job,
      snapshot:()=>({elapsed,heartbeatReads,resultReads,jobInserts})});
  } finally {
    Date.now=originalNow;
    globalThis.setTimeout=originalTimeout;
    if(nowDescriptor) Object.defineProperty(performance,'now',nowDescriptor);
    else delete performance.now;
  }
}

await scenario({readyAt:500},async({result,reads,sleeps,snapshot})=>{
  assert.equal(result.value.status,'completed');
  assert.deepEqual(result.value.result,{saved:true,marker:'synthetic'});
  assert.deepEqual(reads,[0,250,500]);
  assert.deepEqual(sleeps,[250,250]);
  assert.deepEqual(snapshot(),{elapsed:500,heartbeatReads:1,resultReads:1,jobInserts:1});
});

await scenario({readyAt:0},async({result,sleeps,snapshot})=>{
  assert.equal(result.value.status,'completed');
  assert.deepEqual(sleeps,[]);
  assert.equal(snapshot().elapsed,0);
});

await scenario({readLatency:1500,offlineAtEnd:true},async({result,call,reads,objects,job,snapshot})=>{
  assert.equal(result.value.status,'pending');
  assert.equal(result.value.operation_id,job.id);
  assert.equal(result.value.online,false);
  assert.ok(result.value.last_seen);
  assert.equal(result.value.archive,'local archive');
  assert.equal(result.value.transport,'outbound HTTPS');
  assert.match(result.value.message,/Use get_operation/);
  assert.equal(snapshot().elapsed,12000);
  assert.equal(snapshot().heartbeatReads,2); // Submit availability + final public status.
  assert.equal(snapshot().resultReads,0);
  assert.equal(reads.length,7);
  assert.ok(reads.every(start=>start<12000));
  assert.equal(objects.size,1); // Timeout preserves the durable input and job.
  const before=snapshot();
  const inspect=await call('get_operation',{operation_id:job.id});
  assert.equal(inspect.value.status,'pending');
  assert.equal(inspect.value.online,false);
  assert.equal(snapshot().heartbeatReads,before.heartbeatReads+1);
  const denied=await call('get_operation',{operation_id:job.id},'another-owner');
  assert.equal(denied.status,403);
  assert.equal(snapshot().jobInserts,1);
});

await scenario({readLatency:6000},async({result,reads,sleeps,snapshot})=>{
  assert.equal(result.value.status,'pending');
  assert.deepEqual(reads,[0,6250]);
  assert.deepEqual(sleeps,[250]);
  // Storage calls cannot be canceled, but their time consumes the shared budget.
  assert.equal(snapshot().elapsed,12250);
  assert.equal(snapshot().heartbeatReads,2);
});

await scenario({wallJumpPerSleep:-600_000},async({result,reads,snapshot})=>{
  assert.equal(result.value.status,'pending');
  assert.equal(snapshot().elapsed,12000);
  assert.equal(reads.length,48);
  assert.equal(reads.at(-1),11750);
  assert.equal(snapshot().heartbeatReads,2);
});

console.log('PASS: prompt completion, single-read pending checks, monotonic shared wait deadline, slow storage, public offline status, durable input, and owner isolation.');
