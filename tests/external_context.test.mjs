import test from 'node:test';
import assert from 'node:assert/strict';
import {visibleInternalContext} from '../src/project_watch/static/external_context.mjs';
const fixture=()=>({state:{current:{id:'p'},sources:[{id:'s',project_id:'p',content_sha256:'hash'}]},item:{match_term:'Synthetic Peer',internal_context:{project_id:'p',term:'Synthetic Peer',status:'shared_term_only',relationship_verified:false,impact:null,materials:[{project_id:'p',source_id:'s',version:'hash',quote:'Synthetic Peer',locator:'line:1',fragment_id:'f'}]}}});
test('internal links are scoped and invalidated when originals or context change',()=>{
  const good=fixture();assert.equal(visibleInternalContext(good.item,good.state).length,1);
  for(const mutate of [x=>x.state.current.id='other',x=>x.state.sources=[],x=>x.state.sources[0].content_sha256='new',x=>x.item.internal_context.term='different',x=>x.item.internal_context.materials[0].project_id='other',x=>x.item.internal_context.relationship_verified=true,x=>x.item.internal_context.impact='invented',x=>x.state.externalError=Error('offline')]){
    const x=fixture();mutate(x);assert.equal(visibleInternalContext(x.item,x.state).length,0);
  }
});
