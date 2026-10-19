import test from 'node:test';
import assert from 'node:assert/strict';
import {dashboardSummary,judgmentPatch,materialReadiness} from '../src/project_watch/static/monitor_dashboard.mjs';
test('dashboard isolates current project and live confirmed facts',()=>{
 const state={current:{id:'a'},sources:[{id:'s',project_id:'a',content_sha256:'v',created_at:'2026-01-01'},{id:'t',project_id:'b',created_at:'2026-02-01'}],observations:[{id:'ok',project_id:'a',status:'confirmed',source_id:'s',source_sha256:'v'},{id:'stale',project_id:'a',status:'confirmed',source_id:'s',source_sha256:'old'},{id:'candidate',project_id:'a',status:'candidate',source_id:'s'},{id:'foreign',project_id:'b',status:'confirmed',source_id:'s'}],profile:{project_id:'b'}};
 const data=dashboardSummary(state);assert.equal(data.sources.length,1);assert.deepEqual(data.confirmed.map(o=>o.id),['ok']);assert.equal(data.profile,null);
});
test('empty dashboard never supplies example records',()=>{assert.deepEqual(dashboardSummary({}),{sources:[],confirmed:[],receipts:[],profile:null});});
test('human judgment patches only thesis with optimistic version',()=>{assert.deepEqual(judgmentPatch({project_id:'a',version:7},' 我的看法 '),{expected_version:7,fields:{thesis:'我的看法'},restore_fields:[],restore_focus:false});assert.throws(()=>judgmentPatch(null,'x'));});
test('material readiness follows current-project parser job, not source metadata',()=>{
 const source={id:'s',project_id:'a'};
 assert.equal(materialReadiness(source,[{project_id:'b',source_id:'s',status:'completed'}]),'查看原件');
 assert.equal(materialReadiness(source,[{project_id:'a',source_id:'s',status:'failed'}]),'解析未完成');
 assert.equal(materialReadiness(source,[{project_id:'a',source_id:'s',status:'completed',created_at:'2026-01-01'},{project_id:'a',source_id:'s',status:'queued',created_at:'2026-01-02'}]),'准备中');
});
