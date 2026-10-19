import test from 'node:test';
import assert from 'node:assert/strict';
import {visibleUpdates} from '../src/project_watch/static/reading_updates.mjs';

const fixture=()=>({current:{id:'p'},sources:[{id:'s',project_id:'p',content_sha256:'hash'}],reading:{updates:{project_id:'p',items:[{project_id:'p',status:'unreviewed',business_change_verified:false,current_source:{id:'s',version:'hash'},previous_source:null,previous:[],current:[{review_status:'unreviewed',source_refs:[{source_id:'s',version:'hash',locator:'line 1',quote:'synthetic quote'}]}]}]}}});
test('material comparisons never cross projects or show changed sources',()=>{
  assert.equal(visibleUpdates(fixture()).length,1);
  const other=fixture();other.current.id='other';assert.equal(visibleUpdates(other).length,0);
  const gone=fixture();gone.sources=[];assert.equal(visibleUpdates(gone).length,0);
  const changed=fixture();changed.sources[0].content_sha256='new';assert.equal(visibleUpdates(changed).length,0);
  const failed=fixture();failed.readingError=Error('read failed');assert.equal(visibleUpdates(failed).length,0);
  const promoted=fixture();promoted.reading.updates.items[0].business_change_verified=true;assert.equal(visibleUpdates(promoted).length,0);
});
