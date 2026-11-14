import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import ts from 'typescript';
mkdirSync('.test-build',{recursive:true});
for(const name of ['packet-validation','tool-contract','gateway']) {
  const code=ts.transpileModule(readFileSync('lib/'+name+'.ts','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll('"./tool-contract"','"./tool-contract-bounds-test.mjs"').replaceAll('"./packet-validation"','"./packet-validation-bounds-test.mjs"');
  writeFileSync('.test-build/'+name+'-bounds-test.mjs',code);
}
const {validate,tools,instructions}=await import('../.test-build/tool-contract-bounds-test.mjs');
const {boundPublicResult}=await import('../.test-build/gateway-bounds-test.mjs');
const revision='a'.repeat(64);
assert.deepEqual(validate('search_memory',{query:'deadband'}),{query:'deadband',limit:10,full:false,budget_tokens:2000});
assert.equal(validate('search_memory',{query:'deadband',limit:20}).limit,20);
for(const args of [{query:'a',limit:21},{query:'a',budget_tokens:2001},{query:'a',budget_tokens:1}]) assert.throws(()=>validate('search_memory',args));
assert.throws(()=>validate('get_memory',{id:'one',start_char:0,end_char:100}));
assert.throws(()=>validate('get_memory',{id:'one',revision,start_char:100,end_char:100}));
assert.throws(()=>validate('get_memory',{id:'one',revision,block_id:'b',start_char:0}));
assert.equal(validate('get_memory',{id:'one',revision,start_char:0,end_char:100,lookup_id:'b'.repeat(32)}).lookup_id,'b'.repeat(32));
assert.throws(()=>validate('get_memory',{id:'one',lookup_id:'invalid'}));
assert.throws(()=>validate('peek_memory',{id:'one',revision}));
assert.throws(()=>validate('peek_memory',{id:'one',revision,match_id:'m',start_char:0}));
assert.equal(validate('peek_memory',{id:'one',revision,match_id:'m'}).before,500);
const sourceRef={ref:'r'+'a'.repeat(12),lookup_id:'b'.repeat(32)};
for(const action of ['get_memory','peek_memory','timeline_memory']) {
  assert.equal(validate(action,sourceRef).ref,sourceRef.ref);
  assert.equal(validate(action,{id:sourceRef.ref,lookup_id:sourceRef.lookup_id}).id,sourceRef.ref);
  assert.throws(()=>validate(action,{}));
  assert.throws(()=>validate(action,{ref:sourceRef.ref}));
  assert.throws(()=>validate(action,{...sourceRef,id:'one'}));
}
assert.equal(validate('get_memory',{...sourceRef,start_char:0,end_char:100}).end_char,100);
assert.equal(validate('timeline_memory',{id:'one',revision}).before,3);
assert.equal(validate('timeline_memory',{id:'one'}).before,3);
assert.equal(validate('timeline_memory',{...sourceRef,before:5,after:0}).after,0);
for(const bad of [{...sourceRef,before:6},{...sourceRef,after:-1},{...sourceRef,ref:'r0'}])
  assert.throws(()=>validate('timeline_memory',bad));
assert.equal(validate('read_handoff_file',{path:'test/file.txt'}).length,512);
assert.throws(()=>validate('read_handoff_file',{path:'test/file.txt',length:1025}));
assert.ok(tools.find(t=>t.name==='peek_memory').annotations.readOnlyHint);
assert.ok(tools.find(t=>t.name==='timeline_memory').annotations.readOnlyHint);
assert.equal(tools.find(t=>t.name==='search_memory').inputSchema.properties.limit.maximum,20);
assert.match(instructions,/lookup_id/);
const old={operation_id:'b'.repeat(64),status:'completed',ok:true,result:{text:'PRIVATE'.repeat(10000)}};
const refused=boundPublicResult(old);
assert.equal(refused.ok,false);assert.equal(refused.error_detail.code,'result_too_large');
assert.equal(refused.operation_id,old.operation_id);assert.ok(!JSON.stringify(refused).includes('PRIVATE'));
const metered={...old,result:{text:'x'.repeat(3000)},response_budget:{v:1,max_tokens:2000,used_tokens:1600}};
assert.equal(boundPublicResult(metered),metered);
assert.equal(boundPublicResult({...metered,response_budget:{v:1,max_tokens:2000,used_tokens:2500}}).ok,false);
assert.equal(boundPublicResult({...metered,result:{text:'x'.repeat(40000)}}).ok,false);
const saved=boundPublicResult({...old,result:{saved:true,import_status:'imported_with_warnings',import_result:{warnings:['warning'],added_revisions:1},text:'PRIVATE'.repeat(10000)}});
assert.equal(saved.ok,true);assert.equal(saved.result.saved,true);assert.equal(saved.result.warning_count,1);
assert.equal(saved.result.added_revisions,1);assert.ok(!JSON.stringify(saved).includes('PRIVATE'));
console.log('retrieval contract and legacy-operation bounds passed');
