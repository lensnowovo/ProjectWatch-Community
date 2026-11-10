import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source=readFileSync(new URL('../src/externalNavigation.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {hasClinicalScope}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
const detail={counts:{},plan:null,trials:[]};
test('general and not-yet-classified companies have no permanent clinical entry',()=>{
  assert.equal(hasClinicalScope(detail),false);
  assert.equal(hasClinicalScope({...detail,plan:{is_biomed:false,trial_sponsors:['  ']}}),false);
});
test('medical scope stays visible with no collected trials, including device projects',()=>{
  assert.equal(hasClinicalScope({...detail,plan:{is_biomed:true,trial_sponsors:[]}}),true);
});
test('trial sponsors retain the entry when industry classification is not available',()=>{
  assert.equal(hasClinicalScope({...detail,plan:{trial_sponsors:['Synthetic sponsor']}}),true);
});
test('existing clinical news and registry records remain reachable without a plan',()=>{
  assert.equal(hasClinicalScope({...detail,counts:{trial:1}}),true);
  assert.equal(hasClinicalScope({...detail,trials:[{id:'synthetic'}]}),true);
});

test('legacy self-name sponsor defaults do not imply clinical relevance',()=>{
  assert.equal(hasClinicalScope({...detail,project:{name:'Synthetic',legal_name:'Synthetic Ltd'},plan:{is_biomed:false,trial_sponsors:['Synthetic','Synthetic Ltd']},research_source_counts:{trial:0},counts:{trial:1}}),false);
});
test('non-biomedical label does not hide real records or hospital-led clinical scope',()=>{
  assert.equal(hasClinicalScope({...detail,plan:{is_biomed:false},research_source_counts:{trial:2}}),true);
  assert.equal(hasClinicalScope({...detail,plan:{is_biomed:false,trial_terms:['device study']}}),true);
  assert.equal(hasClinicalScope({...detail,project:{name:'Synthetic'},plan:{is_biomed:false,trial_sponsors:['Synthetic hospital']}}),true);
});
