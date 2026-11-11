import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire,Module} from 'node:module';
import {fileURLToPath} from 'node:url';
const require=createRequire(new URL('../package.json',import.meta.url));
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),{StaticRouter}=require('react-router-dom');
const entry=fileURLToPath(new URL('../src/questionAnalysis.tsx',import.meta.url)), mod=new Module(entry);mod.paths=require.resolve.paths('react');
mod._compile(require('esbuild').buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);
const base={project:{id:'synthetic'},jobs:[],capabilities:{insight_projections:false}};
for (const state of ['absent','undetermined']) test(`saved summary remains visible with ${state} judgment`,()=>{
 const html=render({...base,page_summaries:{competition:{text:'独立摘要已有可读内容',date:'2026-11-11',refs:[]}}},{record:{analysis:{position:{summary:'',state}}},generation_available:true});
 assert.match(html,/独立摘要已有可读内容/);assert.doesNotMatch(html,/材料尚未到达|没有通过核验的依据/);
});
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


test('company progress uses public reading without financial judgment or empty chart',()=>{
 const detail={...base,page_summaries:{external:{text:'合成公司已完成客户验证，启动小批量交付。融资用于产线建设。',date:'2026-11-11',refs:[],details:[{text:'详细订单口径',refs:[]}]}}};
 const view={record:{analysis:{operations:{verdict:'unclear',summary:'缺少现金数据，无法判断',for:[],against:[],change_if:[]}}},labels:{operations:{unclear:'无法判断'}},generation_available:true};
 const html=render(detail,view,'operations');
 assert.match(html,/role="tabpanel" aria-label="公司进展"/);
 assert.match(html,/合成公司已完成客户验证/);assert.match(html,/查看依据/);assert.match(html,/查看公司动态/);
 assert.doesNotMatch(html,/缺少现金数据|无法判断|经营与资金|qa-verdict|qa-groups|详细订单口径/);
 assert.equal((html.match(/role="tab"/g)||[]).length,5);
});

test('company progress remains available with no cross-analysis or financial statements',()=>{
 const html=render(base,{record:null,generation_available:true},'operations');
 assert.match(html,/aria-label="公司进展"/);assert.match(html,/生成摘要/);
 assert.doesNotMatch(html,/上传财务|经营与资金|资金撑得住|qa-verdict/);
});
test('the period changes are read under the company progress narrative, not repeated elsewhere',()=>{
 const changes=React.createElement('ol',{className:'top3'},React.createElement('li',null,'合成订单落地'));
 const html=renderToStaticMarkup(React.createElement(StaticRouter,{location:'/?question=operations'},
  React.createElement(mod.exports.QuestionAnalysis,{detail:base,view:{record:null,generation_available:true},error:null,goto(){},changes})));
 const panel=html.slice(html.indexOf('aria-label="公司进展" class="qa-panel'));
 assert.match(panel,/近 60 天重要变化[\s\S]*合成订单落地[\s\S]*查看公司动态/);
 const other=renderToStaticMarkup(React.createElement(StaticRouter,{location:'/?question=chain'},
  React.createElement(mod.exports.QuestionAnalysis,{detail:base,view:{record:null,generation_available:true},error:null,goto(){},changes})));
 assert.doesNotMatch(other,/合成订单落地/);
});
test('the page opens on company progress even when another question has the only verdict',()=>{
 const view={record:{analysis:{position:{verdict:'behind',summary:'仅竞争位置已发布',for:[],against:[],change_if:[]}}},labels:{position:{behind:'落后'}},generation_available:true};
 const html=renderToStaticMarkup(React.createElement(StaticRouter,{location:'/'},
  React.createElement(mod.exports.QuestionAnalysis,{detail:base,view,error:null,goto(){},changes:React.createElement('p',null,'合成关键事件')})));
 assert.match(html,/role="tabpanel" aria-label="公司进展"[\s\S]*合成关键事件/);
});
test('without an analysis extension the period changes are still shown',()=>{
 const html=renderToStaticMarkup(React.createElement(StaticRouter,{location:'/'},
  React.createElement(mod.exports.QuestionAnalysis,{detail:base,view:{record:null,generation_available:false},error:null,goto(){},changes:React.createElement('p',null,'合成关键事件')})));
 assert.match(html,/暂无判断记录[\s\S]*近 60 天重要变化[\s\S]*合成关键事件/);
});
