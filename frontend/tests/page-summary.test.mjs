import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire, Module } from 'node:module';
import { fileURLToPath } from 'node:url';
const require=createRequire(new URL('../package.json',import.meta.url));
const entry=fileURLToPath(new URL('../src/pageSummary.tsx',import.meta.url));
const mod=new Module(entry);mod.paths=require.resolve.paths('react');
mod._compile(require('esbuild').buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);
const {selectSummary,summaryStatus}=mod.exports;
const detail={project:{id:'a'},brief:null};

test('automatic summary failure is not displayed as missing material after reload',()=>{
  assert.equal(summaryStatus({...detail,jobs:[{id:'auto',kind:'page_summary',status:'failed'}]},'papers'),
    '本轮摘要未完成，已有结果保留。');
});

test('a queued automatic continuation stays active and retains the published page',()=>{
  const value={text:'已经发布的具体进展。',refs:[],date:'2026-11-07'};
  const d={...detail,page_summaries:{papers:value},jobs:[{id:'auto',kind:'page_summary',status:'queued',stage:'稍后自动继续'}]};
  assert.equal(selectSummary(d,'papers'),value);
  assert.match(summaryStatus(d,'papers'),/正在整理/);
});
test('no page borrows another page, a brief, titles or count templates',()=>{
  const other={...detail,brief:{summary:'旧简报'},profile:{summary:'内部摘要'},project:{id:'a',description:'公司描述'}};
  for(const key of ['brief','overview','external','trials','papers','patents','industry','policy','technology','chain','competition','ops','business','numbers','fin','risks'])
    assert.equal(selectSummary(other,key),null);
});
test('published page preserves exact text, evidence and date',()=>{
  const value={text:'团队完成了具体方法的验证。',refs:[{code:'S1',label:'研究论文'}],date:'2026-11-05',historical:true};
  const d={...detail,page_summaries:{papers:value}};
  assert.deepEqual(selectSummary(d,'papers'),value);
  assert.equal(selectSummary(d,'trials'),null);
  assert.equal(selectSummary(d,'patents'),null);
});
test('newer publication replaces older reviewed copy, not the reverse',()=>{
  const newer={text:'新的逐页分析',refs:[],date:'2026-11-06',historical:false};
  const older={text:'原已审稿',refs:[],date:'2026-11-05',historical:true,editorial:true};
  const d={...detail,page_summaries:{papers:newer},editorial_summaries:{papers:older}};
  assert.equal(selectSummary(d,'papers').text,newer.text);
  d.editorial_summaries.papers={...older,date:'2026-11-07'};
  assert.equal(selectSummary(d,'papers').text,older.text);
});
