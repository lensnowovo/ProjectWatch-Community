import assert from 'node:assert/strict';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
const require=createRequire(new URL('../package.json',import.meta.url));
const {buildSync}=require('esbuild');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const {StaticRouter}=require('react-router-dom');
function load(path){const entry=fileURLToPath(new URL(path,import.meta.url));const m=new Module(entry);m.paths=require.resolve.paths('react');m._compile(buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty'}}).outputFiles[0].text,entry);return m.exports;}
const {businessIntro,dossierDate,dossierEntries,headerChanges,milestonesFor,safeWebsite,stageLabel}=load('../src/companyDossier.ts');
const {DossierOverview,DossierSource}=load('../src/CompanyDossierView.tsx');
const Header=load('../src/ProjectHeader.tsx').default;
const base={project:{id:'synthetic',name:'合成公司',description:'面向科研单位提供采集设备。',activated:true},documents:[],jobs:[]};
const fact=(kind,label,text,date='',published='')=>({kind,label,text,date,refs:[{url:'https://example.invalid/source',source:'合成来源',published_at:published,quote:text}]});
const render=(Component,props)=>renderToStaticMarkup(React.createElement(StaticRouter,null,React.createElement(Component,props)));
test('intro uses business identity, not a ranking, registry or empty extracted claim',()=>{
 const detail={...base,public_profile:{facts:[fact('business','市场排名','某产品排名第一'),fact('business','经营范围','登记项目')]}};
 assert.equal(businessIntro(detail),base.project.description);
 detail.public_profile.facts.push(fact('business','主营业务','新的业务介绍','','2026-10-01'));
 assert.equal(businessIntro(detail),'新的业务介绍');
 assert.equal(businessIntro({...detail,project:{...base.project,entity_source:'manual'}}),base.project.description);
 assert.equal(dossierEntries(detail).find(x=>x.label==='经营范围').group,'registry');
});
test('date precision and planned/completed/reported states remain distinct',()=>{
 for(const d of ['2026','2026-10','2024-02-29'])assert.equal(dossierDate(d),d);
 for(const d of ['2026-02-29','2026-13','2026-10-40','最近','2026-10-01T00:00:00Z'])assert.equal(dossierDate(d),'');
 const detail={...base,profile:{milestones:[{text:'计划节点',date:'2027-01',status:'planned',refs:[]},{text:'完成节点',date:'2025',status:'done',refs:[]}]},public_profile:{facts:[fact('milestone','公告节点','公告记录','2026-09'),fact('milestone','未知日期','事件日期缺失','','2026-10-01')]}};
 const nodes=milestonesFor(detail);
 assert.equal(nodes.find(x=>x.label==='公告节点').state,'reported');
 assert.equal(nodes.find(x=>x.text==='完成节点').state,'completed');
 assert.equal(nodes.find(x=>x.label==='未知日期').date,'');
 assert.ok(headerChanges(detail).every(x=>x.state!=='planned' && x.date));
 assert.equal(stageLabel('IPO已受理'),'上市进程');
 assert.equal(stageLabel('A轮'),'融资阶段');
});

test('application profile labels keep qualifications and citations without inferring customers from news',()=>{
 const application=fact('business','应用场景','拟用于科研机构试用，尚未形成采购。','','2026-10-01');
 const detail={...base,profile:{sections:[{title:'应用领域',points:[{text:'仅供合成研究用途',refs:[{source_name:'合成材料'}]}]}]},public_profile:{facts:[application,fact('product','适用范围','用于合成试验'),fact('financing','客户企业投资方','仅为投资方')]},observations:{items:[{kind:'business_relation',role:'partner',text:'合成医院临床合作'}]}};
 const entries=dossierEntries(detail),customers=entries.filter(x=>x.group==='customers');
 assert.equal(customers.length,2);
 assert.equal(customers[0].refs[0].source_name,'合成材料');
 assert.equal(customers[1].fact,application);
 assert.equal(entries.find(x=>x.label==='适用范围').group,'business');
 assert.equal(entries.find(x=>x.label==='客户企业投资方').group,'capital');
 const html=render(DossierOverview,{detail,sources:null,registry:null});
 assert.match(html,/拟用于科研机构试用，尚未形成采购/);
 const newsOnly={...base,observations:detail.observations,public_profile:{facts:[]},external:[{title:'合成医院临床合作',category:'company'}]};
 assert.equal(dossierEntries(newsOnly).length,0);
 assert.match(render(DossierOverview,{detail:newsOnly,sources:null,registry:null}),/尚未整理出带来源的客户与应用资料/);
});
test('overview preserves intent qualifiers, full source text, all highlights and undated records',()=>{
 const text='2025年意向订单，并非已签合同；截至报告期尚未确认到账。';
 const detail={...base,profile:{highlights:[{text:'有依据的要点',refs:[{source_name:'合成材料'}]},{text:'另一个资料要点',refs:[]}]},public_profile:{facts:[fact('product','采集系统',text),fact('milestone','未定日期记录','尚未确认节点日期')]}};
 const html=render(DossierOverview,{detail,sources:null,registry:null});
 assert.ok(html.includes(text));assert.match(html,/另一个资料要点/);assert.match(html,/1 条记录日期待补充/);
 assert.doesNotMatch(html,/未披露|cd-node-completed/);
 const entry=dossierEntries(detail)[0];
 const source=render(DossierSource,{entry,onClose(){}});
 assert.ok(source.includes(text));assert.match(source,/<dialog/);assert.match(source,/合成来源/);
});
test('collapsed runtime still shows an ignored unresolved partial task',()=>{
 const detail={...base,jobs:[{id:'synthetic-job',kind:'commercial',status:'failed',dismissed:true,created_at:'2026-10-01',runtime:{state:'partial',label:'部分来源已更新'}}]};
 const html=render(Header,{detail,actions:null,status:React.createElement('div',null,'展开后的任务记录'),goto(){}});
 const summary=html.split('<summary>')[1].split('</summary>')[0];
 assert.match(summary,/部分来源已更新/);assert.match(summary,/提醒已收起，任务仍未完成/);
 assert.match(html,/<details class="company-hero-runtime">/);
});
test('empty and unsafe source values cannot fabricate data or executable links',()=>{
 const html=render(DossierOverview,{detail:base,sources:null,registry:null});
 assert.match(html,/尚未取得产品与业务资料/);assert.match(html,/尚未取得核心团队资料/);
 for(const value of ['javascript:alert(1)','data:text/html,test','file:///secret'])assert.equal(safeWebsite(value),null);
 assert.equal(safeWebsite('example.invalid'),'https://example.invalid/');
});
