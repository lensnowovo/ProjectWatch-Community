import test from 'node:test';
import assert from 'node:assert/strict';
import {profileEditPayload,saveProfile,visibleProfileFocus} from '../src/project_watch/static/monitor_profile.mjs';
const values={business:'Business',stage:'',products:'Product',customers:'',thesis:''};
const profile={version:3,fields:Object.fromEntries(Object.entries(values).map(([k,text])=>[k,{text}])),focus:[{text:'Customer acceptance'}]};
test('unchanged suggestion stays automatic, clearing is explicit',()=>{
  assert.deepEqual(profileEditPayload(profile,values,'Customer acceptance').fields,{});
  const edit=profileEditPayload(profile,{...values,products:''},'');
  assert.deepEqual(edit.fields,{products:''});assert.deepEqual(edit.focus,[]);assert.equal(edit.expected_version,3);
});
test('restoring suggestion releases human override without replacing other fields',()=>{
  const edit=profileEditPayload(profile,{...values,products:'Suggested'},'Suggested focus',new Set(['products']),true);
  assert.deepEqual(edit.restore_fields,['products']);assert.deepEqual(edit.fields,{});assert.equal(edit.restore_focus,true);assert.equal(edit.focus,undefined);
});
test('save uses JSON content type with the existing fetch bridge',async()=>{
  const payload={expected_version:3,fields:{business:'A'}};
  const result=await saveProfile(async(path,options)=>{
    assert.equal(path,'/api/workspace/projects/p/profile');assert.equal(options.method,'PATCH');
    assert.equal(options.headers['Content-Type'],'application/json');assert.deepEqual(JSON.parse(options.body),payload);
    return {version:4};
  },'p',payload);
  assert.equal(result.version,4);
});

test('AI focus keeps live same-project evidence and human notes untouched',()=>{
  const note={text:'我的关注',origin:'human',source_refs:[]};
  const item={text:'客户验收待核实',origin:'ai_synthesis',review_status:'unreviewed',source_refs:[{source_id:'s',version:'v',locator:'line 1'}]};
  const p={project_id:'p',focus:[note,item]},s=[{id:'s',project_id:'p',content_sha256:'v'}];
  assert.deepEqual(visibleProfileFocus(p,s),[note,item]);
  assert.deepEqual(visibleProfileFocus(p,[{...s[0],content_sha256:'changed'}]),[note]);
  assert.deepEqual(visibleProfileFocus(p,[{...s[0],project_id:'other'}]),[note]);
  assert.deepEqual(visibleProfileFocus({...p,focus:[{...item,source_refs:[]}]},s),[]);
});
