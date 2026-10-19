import test from 'node:test';
import assert from 'node:assert/strict';
import {visibleBriefing} from '../src/project_watch/static/monitor_briefing.mjs';
const fixture=()=>({current:{id:'p'},sources:[{id:'s',project_id:'p',content_sha256:'hash'}],observations:[{id:'o',project_id:'p',source_id:'s',source_locator:'line 1',status:'confirmed',version:2}],briefing:{project_id:'p',items:[{project_id:'p',evidence_refs:[{source_id:'s',source_sha256:'hash',observation_id:'o',observation_version:2,locator:'line 1'}]}]}});
test('only the active project and matching reviewed source versions are shown',()=>{
  const state=fixture();assert.equal(visibleBriefing(state).length,1);
  state.current.id='other';assert.equal(visibleBriefing(state).length,0);
  for(const patch of [{status:'rejected'},{version:3},{source_id:'other'},{source_locator:'line 2'}]){
    const s=fixture();Object.assign(s.observations[0],patch);assert.equal(visibleBriefing(s).length,0);
  }
  const s=fixture();s.sources[0].content_sha256='changed';assert.equal(visibleBriefing(s).length,0);
  const failed=fixture();failed.briefingError=new Error('failed');assert.equal(visibleBriefing(failed).length,0);
});
