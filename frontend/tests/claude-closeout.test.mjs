import assert from 'node:assert/strict';
import {createRequire,Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
const require=createRequire(new URL('../package.json',import.meta.url));
const {buildSync}=require('esbuild'),React=require('react'),{renderToStaticMarkup}=require('react-dom/server');
function load(path){const entry=fileURLToPath(new URL(path,import.meta.url));const mod=new Module(entry);mod.paths=require.resolve.paths('react');mod._compile(buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);return mod.exports}
const {financeSeries,qualityChecks,periodKind}=load('../src/financeAnalysis.tsx');
const {NewsStats}=load('../src/landscape.tsx');
const {internalCounts,OperationsView,operatingHighlights}=load('../src/internalPages.tsx');
const {ChartHint}=load('../src/chartHint.tsx');
const {Scale,TechMap}=load('../src/judgment.tsx');
const metric=(id,name,value,more={})=>({id,name,value,unit:'万元',period:'2025',basis:'actual',scope:'公司整体',currency:'CNY',...more});
const detail=metrics=>({metrics,finance:{sections:Object.fromEntries(metrics.map(m=>[m.id,'income']))}});
test('empty financial pages do not advertise available analyses',()=>{const counts=internalCounts({...detail([]),documents:[]});assert.equal(counts.find(c=>c.page==='fin').n,0)});
test('financial series use normalized units, canonical periods and latest source',()=>{
 const rows=[metric('old','收入',999,{std_name:'营业收入',source_created_at:'2026-01-01'}),metric('new','收入',1200000,{std_name:'营业收入',unit:'元',value_norm:120,norm_unit:'万元',period:'2025年度',period_key:'2025',source_created_at:'2026-02-01'})];
 const s=financeSeries(detail(rows));assert.equal(s.length,1);assert.equal(s[0].points.length,1);assert.equal(s[0].points[0].value,120);assert.equal(s[0].points[0].metric.id,'new');
});
test('scope and currency cannot silently merge into company totals',()=>{const s=financeSeries(detail([metric('a','营业收入',100),metric('b','营业收入',20,{scope:'母公司'}),metric('c','营业收入',30,{currency:'USD'})]));assert.equal(s.length,2);assert.equal(s.reduce((n,x)=>n+x.points.length,0),2)});
test('ratios reject mixed currency or units and accept normalized equivalents',()=>{
 for(const extra of [{currency:'USD'},{unit:'亿元'}]){const checks=qualityChecks(financeSeries(detail([metric('a','营业收入',100),metric('b','销售商品、提供劳务收到的现金',90,extra)])));assert.equal(checks.find(x=>x.title==='收入现金含量').tone,'unknown')}
 const checks=qualityChecks(financeSeries(detail([metric('a','营业收入',100),metric('b','销售商品、提供劳务收到的现金',900000,{unit:'元',value_norm:90,norm_unit:'万元'})])));assert.equal(checks.find(x=>x.title==='收入现金含量').value,'90.0%');
});
test('annual and half-year figures do not fabricate a widening loss trend',()=>{const rows=[metric('a','净利润',-100,{period:'2024'}),metric('b','净利润',-60,{period:'2025H1'})];assert.ok(!qualityChecks(financeSeries(detail(rows))).some(x=>x.title==='盈利趋势'));assert.notEqual(periodKind('2025'),periodKind('2025H1'))});
test('positive cash flow during a loss is not described as a cash outflow',()=>{const c=qualityChecks(financeSeries(detail([metric('a','净利润',-100),metric('b','经营活动现金流净额',50)]))).find(x=>x.title==='利润现金含量');assert.match(c.why,/经营现金流为正/)});
test('research expenses do not claim to explain loss causation',()=>{const c=qualityChecks(financeSeries(detail([metric('a','净利润',-100),metric('b','研发费用',80)]))).find(x=>x.title==='研发与亏损规模');assert.equal(c.tone,'watch');assert.match(c.why,/不是亏损原因分解/)});
test('news statistics exclude future dates and count single sources',()=>{
 const now=new Date(),future=new Date(now.getTime()+86400000*365).toISOString();
 const items=[{id:'a',source:'公告',importance:'routine',published_at:now.toISOString(),collected_at:now.toISOString()},{id:'b',source:'期刊',importance:'major',published_at:future,collected_at:now.toISOString()}];
 const html=renderToStaticMarkup(React.createElement(NewsStats,{items,counts:{company:2}}));assert.match(html,/近 90 天<\/dt><dd><b[^>]*>1<\/b>/);assert.match(html,/共 2 家来源/);assert.doesNotMatch(html,/多数月份只有|互相印证/);
});

const internalRef={source_id:'synthetic-material',source_name:'合成经营月报',locator:'p.2'};
const opsDetail=()=>({...detail([]),documents:[{id:'synthetic-material',name:'合成经营月报.pdf',created_at:'2026-09-01',status:'ready'}],doc_changes:[],profile:{sections:[{title:'客户与订单',points:[{text:'完成一项样品交付。',refs:[internalRef]},{text:'第二项仍处于计划阶段。',refs:[internalRef]}]}],milestones:[],risks:[],questions:[]}});
const renderOps=d=>renderToStaticMarkup(React.createElement(OperationsView,{detail:d,go:()=>{}}));
test('operating overview shows sourced material even without extracted metrics',()=>{
 const d=opsDetail(),html=renderOps(d);assert.match(html,/完成一项样品交付/);assert.match(html,/<summary>另 1 条/);assert.match(html,/查看来源：合成经营月报/);assert.match(html,/经营数字尚未结构化/);assert.doesNotMatch(html,/资料中还没有可比的经营指标|op-grid/);assert.equal(internalCounts(d).find(x=>x.page==='ops').n,1);
});
test('operating overview excludes public-only and irrelevant excerpts',()=>{
 const d=opsDetail();d.profile.sections.push({title:'行业与规则',points:[{text:'不属于经营摘要',refs:[internalRef]}]},{title:'产品',points:[{text:'公开新闻不能充当内部材料',refs:[{url:'https://example.org/news',source_id:'',source_name:'公开新闻',locator:''}]}]});assert.equal(operatingHighlights(d).length,1);assert.doesNotMatch(renderOps(d),/公开新闻不能|不属于经营摘要/);
});
test('empty operating overview has one intake prompt and no missing-data card stack',()=>{
 const html=renderOps({...detail([]),documents:[],doc_changes:[],profile:null});assert.match(html,/从一份经营材料开始/);assert.doesNotMatch(html,/经营数字尚未结构化|版本对比|经营摘要尚未形成/);
});
test('operating metrics and actual document changes remain reachable',()=>{
 const d=opsDetail();d.metrics=[metric('volume','交付量',28,{category:'operating',unit:'件'})];d.finance.sections={volume:'operating'};d.doc_changes=[{created_at:'2026-09-02',documents:['合成月报'],metrics:[],texts:[{kind:'新进展',text:'样品验收完成',refs:[internalRef]}]}];const html=renderOps(d);assert.match(html,/经营数字/);assert.match(html,/交付量/);assert.match(html,/样品验收完成/);assert.doesNotMatch(html,/加入新版材料后/);
});

test('chart hints preserve mark geometry and expose name value period to keyboard users',()=>{
 const html=renderToStaticMarkup(React.createElement(ChartHint,{title:'净利润',value:'−386 万元',meta:'2025 · 实际',note:'实心柱为实际'},React.createElement('rect',{x:10,width:20,height:50})));
 assert.match(html,/<rect/);assert.match(html,/x="10"/);assert.match(html,/tabindex="0"/);assert.match(html,/aria-label="净利润；−386 万元；2025 · 实际；实心柱为实际"/);assert.doesNotMatch(html,/<span|role="tooltip"/);
});
test('judgment bars describe qualitative steps rather than numeric scores',()=>{
 const html=renderToStaticMarkup(React.createElement(Scale,{scale:'operations',value:'steady',labels:{pressured:'承压',steady:'平稳',improving:'改善'}}));assert.equal((html.match(/data-chart-hint/g)||[]).length,3);assert.match(html,/● 当前判断/);assert.match(html,/定性档位，不是分数或百分比/);assert.match(html,/role="group"/);
});
test('unknown technology position does not invent a chart point or tooltip',()=>{
 const html=renderToStaticMarkup(React.createElement(TechMap,{maturity:'unknown',consensus:'unknown',labels:{maturity:{},consensus:{}}}));assert.match(html,/依据不足，暂不定位/);assert.doesNotMatch(html,/data-chart-hint/);
});
