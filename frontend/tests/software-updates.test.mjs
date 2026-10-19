import assert from 'node:assert/strict';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import {readFileSync} from 'node:fs';
import test from 'node:test';
const require = createRequire(new URL('../package.json', import.meta.url));
const {buildSync} = require('esbuild'), React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const entry = fileURLToPath(new URL('../src/pages/Settings.tsx', import.meta.url));
const mod = new Module(entry); mod.paths = require.resolve.paths('react');
mod._compile(buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text, entry);
const {UpdateSummary} = mod.exports;
const render = state => renderToStaticMarkup(React.createElement(UpdateSummary, {value:{state,message:'本地状态',running_version:'1.0.0',current_commit:'a'.repeat(40),target_commit:'b'.repeat(40),target_version:'v1.1.0',handoff:'不含凭据'}}));
test('clean source offers pinned offline upgrade instructions',()=>{
 const html=render('available'); assert.match(html,/停机升级指引/); assert.match(html,/--current a{40}/); assert.match(html,/--target b{40}/); assert.match(html,/--confirm/);
});
test('custom source never offers apply instructions',()=>{
 for(const state of ['agent_required','check_failed','up_to_date','not_configured']) assert.doesNotMatch(render(state),/--confirm|停机升级指引/);
});
test('installed packages get manual non-overwriting instructions',()=>{assert.match(render('manual_install'),/不要将新版直接覆盖/)});
test('page mount only reads status; check and handoff are explicit',()=>{
 const text=readFileSync(entry,'utf8'); assert.match(text,/api\.updates\(\)/); assert.match(text,/复制 Agent 交接说明/);
 assert.doesNotMatch(text,/api\.applyUpdate|setInterval.*checkUpdates/);
});
