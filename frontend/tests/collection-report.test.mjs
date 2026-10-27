import assert from 'node:assert/strict';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
const require = createRequire(new URL('../package.json', import.meta.url));
const {buildSync} = require('esbuild'), React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const root = fileURLToPath(new URL('../', import.meta.url));
const mod = new Module(root+'collection-report-test.cjs');
mod.filename = root+'collection-report-test.cjs'; mod.paths = require.resolve.paths('react');
mod._compile(buildSync({stdin:{contents:"export {collectionExecutionLabel, externalCollection} from './src/externalCollection'; export {Coverage} from './src/pages/Project'; export {RoutineChannels} from './src/plan';", resolveDir:root, loader:'tsx'},
 bundle:true, write:false, platform:'node', format:'cjs', packages:'external', loader:{'.css':'empty','.svg':'text'}}).outputFiles[0].text, mod.filename);
const {Coverage, RoutineChannels, collectionExecutionLabel, externalCollection} = mod.exports;
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props));

test('receipt state takes precedence over clinical heuristic and current configuration', () => {
 for (const [execution, label] of Object.entries({empty:'已检索，暂无结果',failed:'检索失败',unknown:'结果待确认',
   partial:'部分完成',not_due:'未到更新时间',not_configured:'未配置',needs_scope:'查询范围待确定',
   not_integrated:'未接入',disabled:'暂不采集',not_applicable:'不适用',budget_deferred:'预算用尽，已延后'})) {
  const detail = {project:{id:'synthetic'}, search_ready:true, registry_ready:true,
   plan:{is_biomed:false, has_search:false, round_id:'r1', collection_report:{schema_version:1,job_id:'r1'},
    last_report:{trial:{execution,checked:0,added:0,errors:[]}}}};
  const html = render(Coverage, {detail});
  assert.equal(collectionExecutionLabel(execution), label);
  assert.match(html, new RegExp(label));
  assert.ok(html.includes('本轮未纳入'));
  assert.doesNotMatch(html, /新增 0|需配置搜索服务/);
 }
});

test('routine source badges display receipt state, not configured means on', () => {
 const html = render(RoutineChannels, {framework:[{key:'science',label:'技术'}],
  coverage:{science:[{key:'technology',label:'技术动态',count:null,unit:'条来源',window:'本轮新增收录',
   state:'pending',execution:'budget_deferred'}]}});
 assert.match(html, /预算用尽，已延后/);
 assert.doesNotMatch(html, /待首轮|null|未开启/);
});

test('host reports name source records instead of pretending each is a new event', () => {
 const html = render(Coverage, {detail:{project:{id:'synthetic'},plan:{round_id:'r1',
  collection_report:{schema_version:1,job_id:'r1'},
  last_report:{chain:{execution:'success',added:3,checked:1,errors:[]}}}}});
 assert.match(html, /产业链动态/);
 assert.match(html, /新收录 3 条来源/);
 assert.match(html, /技术动态/);
 assert.doesNotMatch(html, /新增事件/);
});

test('empty source pages use only their own bound round and do not hide live or paused states', () => {
 const detail = {project:{id:'synthetic',status:'active'},jobs:[],plan:{round_id:'r1',
  collection_report:{schema_version:1,job_id:'r1'},last_report:{paper:{execution:'not_configured'},trial:{execution:'empty'}}}};
 assert.equal(externalCollection(detail,['paper']).label,'未配置');
 assert.equal(externalCollection(detail,['trial']).label,'已检索，暂无结果');
 assert.equal(externalCollection(detail,['patent']).label,'本轮未纳入');
 assert.equal(externalCollection({...detail,plan:{...detail.plan,round_id:'r2'}},['paper']).label,'暂无收录');
 assert.equal(externalCollection({...detail,project:{...detail.project,status:'paused'}},['paper']).label,'监控已暂停');
 assert.equal(externalCollection({...detail,jobs:[{id:'new',kind:'external',status:'running',runtime:{state:'running',phase:'collect'}}]},['paper']).label,'正在收集');
});

test('current receipt updates empty pages and coverage before a model finishes', () => {
 const report = {schema_version:1,job_id:'live',job_status:'running',active:true,view_state:'receipts',channels:{
  paper:{execution:'running',checked:0,added:0,errors:[]},trial:{execution:'empty',checked:1,added:0,errors:[]}}};
 const detail = {project:{id:'synthetic',status:'active'},collection_report:report,
  jobs:[{id:'live',kind:'external',status:'running',runtime:{state:'running',phase:'collect'}}]};
 assert.equal(externalCollection(detail,['paper']).label,'正在收集');
 assert.equal(externalCollection(detail,['trial']).label,'已检索，暂无结果');
 assert.equal(externalCollection({...detail,collection_report:{...report,active:false}},['paper']).label,'尚未收口');
 const html = render(Coverage,{detail});
 assert.match(html,/正在收集/); assert.match(html,/已检索，暂无结果/);
 assert.doesNotMatch(html,/第一次检索完成后显示/);
 const queued = {...detail,collection_report:{...report,view_state:'awaiting_plan',channels:{},active:false}};
 assert.equal(externalCollection(queued,['trial']).label,'尚未执行');
 const corrupt = {...detail,collection_report:{...report,view_state:'unavailable',channels:{}}};
 assert.equal(externalCollection(corrupt,['paper']).label,'回执无法核验');
 assert.match(render(Coverage,{detail:corrupt}),/回执无法核验/);
});
