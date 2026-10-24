import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire, Module } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
const require = createRequire(new URL('../package.json', import.meta.url));
const { buildSync } = require('esbuild');
function load(path) {
  const entry = fileURLToPath(new URL(path, import.meta.url));
  const mod = new Module(entry); mod.paths = require.resolve.paths('react');
  mod._compile(buildSync({entryPoints:[entry], bundle:true, write:false, platform:'node', format:'cjs', packages:'external', loader:{'.css':'empty'}}).outputFiles[0].text, entry);
  return mod.exports;
}
const settings = readFileSync(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8');

test('preserved upgrade executor has a human-readable source label', () => {
  assert.match(settings, /upgrade_preserved: "沿用升级前的执行方式"/);
});

test('an unavailable engine reads as held research, never as a raw code or a quiet fallback', () => {
  const { errorMessage } = load('../src/ui.tsx');
  const text = errorMessage(new Error('dsh_unavailable'));
  assert.match(text, /自动研究已暂停/); assert.doesNotMatch(text, /dsh_unavailable/);
  assert.match(settings, /不会改用经典流程/);
});

test('the engine switch is saved for the next start and is hidden when the environment decides', () => {
  assert.match(settings, /id: "executor", label: "研究引擎"/);
  assert.match(settings, /重启 ProjectWatch 后生效/);
  assert.match(settings, /data\.configurable\s*\n?\s*\?/);
  assert.match(settings, /由环境变量决定/);
});
