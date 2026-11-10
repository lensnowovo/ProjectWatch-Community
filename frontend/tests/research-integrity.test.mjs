import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire,Module} from 'node:module';
import {fileURLToPath} from 'node:url';
const require=createRequire(new URL('../package.json',import.meta.url));
const entry=fileURLToPath(new URL('../src/researchIntegrity.ts',import.meta.url));
const m=new Module(entry);m.paths=require.resolve.paths('react');
m._compile(require('esbuild').buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs'}).outputFiles[0].text,entry);
const {primaryPatent,relation,presentSource,reviewed}=m.exports;
const detail={project:{id:'p',name:'合成科技',legal_name:'合成科技有限公司',aliases:[]}};
const row=(extra={})=>({id:'1',project_id:'p',category:'patent',title:'Primary invention - CN999999A (en) | 合成科技有限公司',url:'https://patents.google.com/patent/CN123456A/en',snippet:'引用：合成科技有限公司 CN999999A',extra,processing_status:'organized',review:'confirmed',published_at:'2010-01-01'});
test('citation mention and model subject tag cannot become applicant, primary number or date',()=>{
 const r=row({research_relation:'subject',patent_number:'CN999999A',applicants:['合成科技有限公司']});
 assert.equal(primaryPatent(r),false);assert.equal(relation(r,detail),'other');
 assert.equal(presentSource(r).title,'Primary invention');assert.equal(presentSource(r).published_at,null);
 assert.equal(r.published_at,'2010-01-01');assert.match(r.title,/CN999999A/);
});
test('primary document requires matching identity and exact applicant, not substring',()=>{
 const r=row({provider_kind:'third_party_index',patent_number:'CN123456A',applicants:['合成科技有限公司']});
 assert.equal(primaryPatent(r),true);assert.equal(relation(r,detail),'subject');
 assert.equal(relation({...r,extra:{...r.extra,applicants:['另一家合成科技有限公司']}},detail),'other');
 assert.equal(primaryPatent({...r,url:'https://example.invalid/patent/CN123456A'}),false);
});
test('unreviewed and unrelated sources remain outside reviewed analysis',()=>{
 const base={...row(),category:'paper',extra:{research_relation:'field'}};
 assert.equal(reviewed(base),true);
 assert.equal(reviewed({...base,review:'unreviewed'}),false);
 assert.equal(reviewed({...base,extra:{research_relation:'unrelated'}}),false);
});
test('clinical trial sponsor is not inferred from a company subject tag',()=>{
 const r={...row(),category:'trial',extra:{research_relation:'subject',nct_id:'NCT01234567',sponsor:'独立大学',status:'RECRUITING'}};
 assert.equal(relation(r,detail),'other');
 assert.equal(relation({...r,extra:{...r.extra,collaborators:['合成科技有限公司']}},detail),'subject');
});
