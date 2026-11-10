import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire,Module} from 'node:module';
import {fileURLToPath} from 'node:url';
const require=createRequire(new URL('../package.json',import.meta.url));
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),{StaticRouter}=require('react-router-dom');
const entry=fileURLToPath(new URL('../src/questionAnalysis.tsx',import.meta.url)), mod=new Module(entry);mod.paths=require.resolve.paths('react');
mod._compile(require('esbuild').buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);
const base={project:{id:'synthetic'},jobs:[],capabilities:{insight_projections:false}};
function render(detail,view,question='position') {return renderToStaticMarkup(React.createElement(StaticRouter,{location:'/?question='+question},React.createElement(mod.exports.QuestionAnalysis,{detail,view,error:null,goto(){}})));}
test('an independent summary is accessible before a cross-analysis verdict exists',()=>{
 const html=render({...base,page_summaries:{competition:{text:'合成同行已完成产品验证。',date:'2026-11-09',refs:[]}}},{record:null,generation_available:true});
 assert.match(html,/合成同行已完成产品验证/);assert.match(html,/综合判断尚未形成/);assert.doesNotMatch(html,/disabled=""/);
 assert.match(html,/role="tabpanel"[^>]*aria-label="竞争位置"/);
});
test('all missing sections remain selectable and preserve the requested empty section',()=>{
 const html=render(base,{record:null,generation_available:true,section_states:{chain:'pending'}},'chain');
 assert.equal((html.match(/role="tab"/g)||[]).length,5);assert.doesNotMatch(html,/disabled=""/);assert.match(html,/role="tabpanel"[^>]*aria-label="产业链"/);assert.match(html,/本轮分析进行中/);
});
test('historical verdict keeps its own summary rather than a newer independent narrative',()=>{
 const html=render({...base,page_summaries:{competition:{text:'独立摘要与旧评级不同',date:'2026-11-09',refs:[]}}},{record:{analysis:{position:{verdict:'behind',summary:'旧判断与其证据一致',for:[],against:[],change_if:[]}}},labels:{position:{behind:'落后'}},generation_available:true});
 assert.match(html,/旧判断与其证据一致/);assert.doesNotMatch(html,/独立摘要与旧评级不同/);
});
test('read-only capability still exposes a saved independent summary without analysis',()=>{
 const html=render({...base,page_summaries:{competition:{text:'已保存且有依据的摘要',date:'2026-11-09',refs:[]}}},{record:null,generation_available:false});
 assert.match(html,/已保存且有依据的摘要/);assert.doesNotMatch(html,/开始 AI 交叉分析/);
});
