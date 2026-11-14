import assert from 'node:assert/strict';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import ts from 'typescript';

mkdirSync('.test-build', {recursive: true});
for (const source of ['lib/packet-validation', 'lib/tool-contract', 'lib/gateway', 'worker']) {
  const name = source.split('/').at(-1);
  const output = ts.transpileModule(readFileSync(source + '.ts', 'utf8'), {
    compilerOptions:{target:ts.ScriptTarget.ES2022, module:ts.ModuleKind.ES2022}
  }).outputText.replace(/(['"])\.\/(?:lib\/)?(packet-validation|tool-contract|gateway)\1/g, "'./ingress-$2.mjs'");
  writeFileSync('.test-build/ingress-' + name + '.mjs', output);
}
const {default: worker} = await import('../.test-build/ingress-worker.mjs');
const env = {DB:{}, BUCKET:{}, OWNER_ID:'test-owner', OWNER_TOKEN:'m'.repeat(40),
  CONNECTOR_TOKEN:'t'.repeat(40), CONNECTOR_KEY:'k'.repeat(40)};
function call(path, headers, override = {}) {
  return worker.fetch(new Request('https://example.test' + path, {method:'POST', headers,
    body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/list'})}), {...env,...override}, {});
}
assert.equal((await call('/mcp', {'X-ThreadSatchel-Owner':env.OWNER_ID})).status, 401);
assert.equal((await call('/mcp', {Authorization:'Bearer ' + env.CONNECTOR_TOKEN})).status, 401);
assert.equal((await call('/mcp', {Authorization:'Bearer ' + env.OWNER_TOKEN})).status, 200);
assert.equal((await call('/api/agent', {'X-ThreadSatchel-Key':env.CONNECTOR_KEY})).status, 401);
assert.equal((await call('/api/agent', {Authorization:'Bearer ' + env.CONNECTOR_TOKEN})).status, 401);
assert.equal((await call('/api/agent', {Authorization:'Bearer ' + env.OWNER_TOKEN,
  'X-ThreadSatchel-Key':env.CONNECTOR_KEY})).status, 401);
assert.equal((await call('/api/agent', {Authorization:'Bearer ' + env.CONNECTOR_TOKEN,
  'X-ThreadSatchel-Key':env.CONNECTOR_KEY})).status, 400); // Auth passes, unsupported synthetic action.
assert.equal((await call('/mcp', {}, {OWNER_TOKEN:env.CONNECTOR_KEY})).status, 503);
assert.equal((await call('/mcp', {}, {OWNER_TOKEN:undefined})).status, 503);
console.log('PASS: owner and connector ingress authentication and fail-closed configuration.');
