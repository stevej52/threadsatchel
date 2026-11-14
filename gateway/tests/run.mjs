import { mkdirSync, readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

// Each isolated suite supplies its synthetic database, bucket and credentials.
mkdirSync('.test-build', {recursive: true});
for (const name of readdirSync('tests').filter(name => name.endsWith('.test.mjs')).sort()) {
  const result = spawnSync(process.execPath, ['tests/' + name], {stdio: 'inherit'});
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
