import assert from 'node:assert/strict';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
const require=createRequire(new URL('../package.json',import.meta.url));
const {buildSync}=require('esbuild'), React=require('react'), {renderToStaticMarkup}=require('react-dom/server');
const root=fileURLToPath(new URL('../',import.meta.url));
const mod=new Module(root+'research-source-tests.cjs'); mod.filename=root+'research-source-tests.cjs'; mod.paths=require.resolve.paths('react');
mod._compile(buildSync({stdin:{contents:"export * from './src/researchSources'; export {ApiError} from './src/api'; export {CategoryStrip} from './src/pages/Project';",resolveDir:root,loader:'tsx'},
  bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty','.svg':'text'}}).outputFiles[0].text,mod.filename);
const {loadResearchSources,ResearchSourceList,ApiError,CategoryStrip}=mod.exports;
const row=(id)=>({id,project_id:'synthetic',category:'patent',title:'Synthetic <script>source</script>',url:'https://example.invalid/patent',source:'Synthetic source',snippet:'A source excerpt',published_at:null,collected_at:'2026-10-29',extra:{assignee_query:'Synthetic company',applicants:['Independent company'],applicant_quote:'Current Assignee: Independent company'},processing_status:'pending',read_scope:'summary'});
const page=(offset,revision='one',total=241)=>({project_id:'synthetic',category:'patent',revision,total,offset,next_offset:offset+40<total?offset+40:null,items:Array.from({length:Math.min(40,total-offset)},(_,n)=>row(String(offset+n)))});

test('loads beyond the previous 200 cap with a stable revision and unique identities',async()=>{
  const calls=[];
  const data=await loadResearchSources(async(p,c,offset=0,rev)=>{calls.push([p,c,offset,rev]);return page(offset);},'synthetic','patent',280);
  assert.equal(data.items.length,241); assert.equal(data.next_offset,null); assert.equal(new Set(data.items.map(i=>i.id)).size,241);
  assert.ok(calls.slice(1).every(c=>c[3]==='one'));
});
test('source change between pages discards the old prefix and restarts only once',async()=>{
  let generation=0;const offsets=[];
  const data=await loadResearchSources(async(p,c,offset=0)=>{offsets.push(offset);if(offset===40 && generation++===0){const e=new ApiError('changed');e.code='research_sources_changed';throw e;}return page(offset,generation?'two':'one',80);},'synthetic','patent',80);
  assert.deepEqual(offsets,[0,40,0,40]); assert.equal(data.revision,'two'); assert.equal(data.items.length,80);
});
test('persistent change and ordinary errors never loop indefinitely',async()=>{
  let count=0;
  await assert.rejects(loadResearchSources(async()=>{count++;const e=new ApiError('changed');e.code='research_sources_changed';throw e;},'synthetic','patent',40));
  assert.equal(count,2);
  count=0;await assert.rejects(loadResearchSources(async()=>{count++;throw new Error('offline');},'synthetic','patent',40));assert.equal(count,1);
});
test('source rows are escaped, attribution stays a clue and no event mutation controls are rendered',()=>{
  const html=renderToStaticMarkup(React.createElement(ResearchSourceList,{items:[{...row('a'),url:'javascript:alert(1)'}]}));
  assert.match(html,/检索线索/);assert.match(html,/申请人摘录/);assert.match(html,/Independent company/);assert.match(html,/发表日期未提供/);
  assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/javascript:|href=|标记重要|关联与依据 →|本公司专利布局/);
  assert.match(html,/<details/);assert.doesNotMatch(html,/<details[^>]*open/);
  assert.match(html,/相关性待核/);
});
test('professional navigation counts sources even when no model event exists',()=>{
  const detail={project:{id:'synthetic'},counts:{paper:0,trial:0,patent:0},research_source_counts:{paper:4,trial:1,patent:10},jobs:[],trials:[]};
  const html=renderToStaticMarkup(React.createElement(CategoryStrip,{detail,page:'patents',go:()=>{}}));
  assert.match(html,/论文<\/span><b>4/);assert.match(html,/临床<\/span><b>1/);assert.match(html,/专利与注册<\/span><b>10/);
});
