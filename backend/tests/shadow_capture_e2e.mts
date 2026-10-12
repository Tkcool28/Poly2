// Disposable integration driver, never a production launch or synthetic override.
import assert from 'node:assert/strict';
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
const [root, dir] = process.argv.slice(2);
const git = spawnSync('git', ['rev-parse', 'HEAD'], {cwd:root, encoding:'utf8'});
assert.equal(git.stdout.trim(), 'fc8eb3b755c22695179c75968a84721be7590232');
const authorityModule = await import(pathToFileURL(join(root, 'src/compare/production-authority.ts')).href);
const archiveModule = await import(pathToFileURL(join(root, 'src/compare/poly2-fenced-archive.ts')).href);
const external = authorityModule.loadAuthority(join(dir,'authority.json'),readFileSync(join(dir,'pin'),'utf8'));
const binding = external.authority.binding;
// Prefixes must not become archives even when every retained signature is valid.
const prefix = join(dir,'fence-prefix.ndjson');
const records = readFileSync(join(dir,'fence.ndjson'),'utf8').trim().split('\n');
assert.equal(JSON.parse(records.at(-1)!).kind,'ARCHIVE');
writeFileSync(prefix,records.slice(0,-1).join('\n')+'\n');
assert.throws(() => archiveModule.sealFencedArchiveFiles(binding,prefix,join(dir,'drain.ndjson'),join(dir,'prefix-invalid.json'),external));
const archivePath = join(dir,'archive.json');
archiveModule.sealFencedArchiveFiles(binding,join(dir,'fence.ndjson'),join(dir,'drain.ndjson'),archivePath,external);
const archive = archiveModule.validateFencedArchiveFile(archivePath,binding.cohort,binding.window,false,external);
assert.equal(archive.rows.length, 1);
assert.equal(archive.rows[0].normalizedUtc, null);
assert.equal(archive.rows[0].rejectionReason, 'stale_signal');
mkdirSync(join(dir,'shadow'));
writeFileSync(join(dir,'cohorts.json'),JSON.stringify({window:binding.window,controlled:binding.cohort}));
const p = spawnSync('node',['--import','tsx','src/compare/cli.ts',join(dir,'shadow'),archivePath,
    join(dir,'cohorts.json'),join(dir,'out'),join(dir,'authority.json'),external.pin],
    {cwd:root,encoding:'utf8',env:process.env});
assert.equal(p.status,0,p.stdout+p.stderr);
const result=JSON.parse(readFileSync(join(dir,'out/comparison.json'),'utf8'));
assert.equal(result.coverage.poly2Only,1);
console.log(JSON.stringify({kind:'DISPOSABLE_POLY2_CANONICAL_NATIVE',rowCount:archive.rows.length,
    productionCLIExit:p.status,consumerSha:git.stdout.trim(),coverage:result.coverage}));
