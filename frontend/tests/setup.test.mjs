import assert from 'node:assert/strict';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import {readFileSync} from 'node:fs';
import test from 'node:test';
const require = createRequire(new URL('../package.json', import.meta.url));
const {buildSync} = require('esbuild');
const entry = fileURLToPath(new URL('../src/pages/Setup.tsx', import.meta.url));
const mod = new Module(entry); mod.paths = require.resolve.paths('react');
mod._compile(buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text, entry);
const {setupPassed} = mod.exports;
test('setup requires both actual connection results, never saved flags',()=>{
 assert.equal(setupPassed(null,'bocha'),false);
 assert.equal(setupPassed({model:{ok:true},search_bocha:{ok:null}},'bocha'),false);
 assert.equal(setupPassed({model:{ok:true},search_bocha:{ok:false}},'bocha'),false);
 assert.equal(setupPassed({model:{ok:true},search_tavily:{ok:true}},'bocha'),false);
 assert.equal(setupPassed({model:{ok:true},search_bocha:{ok:true}},'bocha'),true);
});
test('mount reads config only; explicit consent and one-shot test remain',()=>{
 const text=readFileSync(entry,'utf8'), effect=text.slice(text.indexOf('useEffect(()'),text.indexOf('const changed'));
 assert.doesNotMatch(effect,/testSetup|saveSettings|resolve|setInterval/);
 assert.match(text,/!consent \|\| attempted/);
 assert.match(text,/未自动重试/);
 assert.match(text,/providers: \[provider\]/);
 assert.doesNotMatch(text,/localStorage|sessionStorage/);
});
