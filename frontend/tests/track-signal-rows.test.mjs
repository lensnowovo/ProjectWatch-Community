import assert from 'node:assert/strict';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
const require=createRequire(new URL('../package.json',import.meta.url));
const {buildSync}=require('esbuild');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const entry=fileURLToPath(new URL('../src/TrackSignalRows.tsx',import.meta.url));
const m=new Module(entry);m.paths=require.resolve.paths('react');
m._compile(buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);
const {TrackSignalRows}=m.exports;
const cell={key:'capital',label:'资本',tone:'unknown',value:'近180天0起',sub:'截至2026-10-20',direction:'方向未明',series:[{label:'2025Q3',value:2},{label:'2025Q4',value:0}],for_us:'',sources:2,evidence:[]};
const render=(cells)=>renderToStaticMarkup(React.createElement(TrackSignalRows,{data:{industry:'合成赛道',quarters:['2025Q3','2025Q4'],cells},onOpen(){}}));
test('zero value and different reporting windows remain explicit; bars are counts, not growth',()=>{
 const html=render([cell]);
 assert.match(html,/近180天0起/);assert.match(html,/截至2026-10-20/);assert.match(html,/2025Q3/);
 assert.match(html,/0 条记录/);assert.match(html,/不代表市场规模或景气变化/);
 assert.match(html,/height:0%/);assert.doesNotMatch(html,/<button[^>]*>[\s\S]*data-chart-hint[\s\S]*<\/button>/);
});
test('missing metrics have one placeholder, no fabricated chart or empty impact boilerplate',()=>{
 const html=render([{...cell,value:'—',sub:'信息不足',series:[]}]);
 assert.equal((html.match(/信息不足/g)||[]).length,1);
 assert.doesNotMatch(html,/tsr-bar-target|无分季度记录|对公司的影响未整理|<details/);
});
test('invalid, single-point and all-zero series do not create a trend',()=>{
 for(const series of [[{label:'a',value:NaN},{label:'b',value:1}],[{label:'a',value:-1},{label:'b',value:2}],[{label:'a',value:2}],[{label:'a',value:0},{label:'b',value:0}]]) assert.doesNotMatch(render([{...cell,series}]),/tsr-bar-target/);
});
test('actual company implications remain available on expansion',()=>{
 const html=render([{...cell,for_us:'试点支持不代表获批。'}]);
 assert.match(html,/<details class="tsr-impact">/);assert.match(html,/试点支持不代表获批/);assert.doesNotMatch(html,/<details[^>]* open/);
});
