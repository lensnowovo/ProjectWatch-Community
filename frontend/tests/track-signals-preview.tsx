// Synthetic fixture only. All API methods are replaced; no live instance is accessed.
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QuestionAnalysis } from '../src/questionAnalysis';
import { ToastProvider } from '../src/motion';
import { api, type InsightView, type ProjectDetail, type TrackView } from '../src/api';
import '../src/styles.css';
import './company-dossier-integration.css';

const quarters = ['2025Q3','2025Q4','2026Q1','2026Q2','2026Q3','2026Q4'];
const series = (values: number[]) => quarters.map((label, i) => ({ label, value: values[i] }));
const track: TrackView = { industry: '神经接口医疗器械 · 模拟赛道', quarters, summary: '政策支持，商业化仍在推进。', balance: 'good', generated_at: '2026-10-20', cells: [
  { key: 'demand', label: '需求', value: '36亿元', sub: '中国市场规模 · 2025', direction: '方向未明', tone: 'unknown', series: series([0,0,0,0,0,0]), evidence: [], for_us: '', sources: 1 },
  { key: 'capital', label: '资本', value: '近180天0起', sub: '收录的融资事件 · 截至2026-10-20', direction: '方向未明', tone: 'unknown', series: series([2,2,0,0,0,0]), evidence: [], for_us: '', sources: 4 },
  { key: 'price', label: '价格', value: '—', sub: '信息不足', direction: '方向未明', tone: 'unknown', series: series([0,0,0,0,0,0]), evidence: [], for_us: '', sources: 0 },
  { key: 'policy', label: '政策', value: '18条重要政策', sub: '最近：2026-10-19', direction: '顺风', tone: 'good', series: series([0,1,2,1,10,4]), evidence: [{ title: '合成政策记录', url: '', source: '设计样本', date: '2026-10-19', grade: '公示' }], for_us: '科研与临床试点获得政策支持，有助于研究合作；尚不代表产品获批或形成收入。', sources: 18 },
] };
let scenario = 'sparse';
for (const key of Object.keys(api)) (api as any)[key] = async () => {
  if (key === 'track') return scenario === 'empty' ? { ...track, cells: track.cells.map(cell => ({ ...cell, value: '', sub: '', tone: 'unknown', series: [], for_us: '', evidence: [], sources: 0 })) } : track;
  if (key === 'feed') return [];
  if (key === 'race') return { peers: [], dimensions: [], sources: [] };
  throw new Error('Synthetic preview blocks API calls');
};
const point = (text: string) => ({ text, refs: [] });
const base = { confidence: 'medium' as const, for: [point('合成正向依据')], against: [point('合成相反依据')], change_if: [] };
const view: InsightView = { job: null, history: [], model_ready: false, sources: {}, generation_available: false,
  labels: { operations: { unclear:'无法判断' }, position: { ahead:'靠前' }, maturity: { advanced:'较先进' }, consensus: {}, chain: { favorable:'有利' }, industry: { cooling:'降温', consolidating:'整合', stable:'平稳', growing:'增长', expanding:'快速扩张' } },
  record: { generated_at: '2026-10-20', counts: {}, total: 0, model: 'synthetic', analysis: {
    headline: '合成设计样本', operations: { ...base, verdict:'unclear', summary:'收入确认情况待核实，暂无法判断整体经营趋势。' },
    position: { ...base, verdict:'ahead', peers: [], summary:'研究系统推进临床验证，在同路线样本中进展靠前。' },
    technology: { route:'神经接口', maturity:'advanced', consensus:'forming', confidence:'medium', summary:'软硬件一体化路线持续推进，规模应用仍需验证。', academic:[point('合成学术依据')], industry:[], risks:[] },
    chain: { ...base, verdict:'favorable', summary:'科研机构和临床合作网络提供支持，商业交付仍在推进。' },
    industry: { verdict:'expanding', confidence:'medium', summary:'政策支持与研究投入带动赛道扩张；商业化与支付路径仍处早期，市场规模不能直接视为公司收入。', drivers:[point('政策试点扩大'), point('临床研究增加')], headwinds:[point('规模交付仍待验证')] }, gaps:[], cited:[],
  } },
};
if (!new URLSearchParams(location.search).has('question')) history.replaceState(null, '', `${location.pathname}?question=industry`);
function App() {
  const [empty,setEmpty] = useState(false), [dark,setDark] = useState(false);
  const detail = { project: { id:'synthetic-track-preview', name:'澄序医疗', last_external_at: empty ? 'empty':'sparse' }, documents:[], jobs:[], models:[], capabilities:{ insight_projections:true } } as unknown as ProjectDetail;
  return <ToastProvider><div className="integration-tools"><span>赛道信号局部预览 · 合成数据 · 不连接当前实例</span><label><input type="checkbox" checked={empty} onChange={event => { scenario = event.target.checked ? 'empty':'sparse'; setEmpty(event.target.checked); }}/>资料不足样本</label><button onClick={()=>{setDark(!dark);document.documentElement.setAttribute('data-theme',dark?'light':'dark');}}>切换深浅色</button></div><main className="integration-page" style={{maxWidth:1460,width:'calc(100% - 40px)',paddingTop:36}}><QuestionAnalysis detail={detail} view={view} error={null} goto={()=>{}}/></main></ToastProvider>;
}
createRoot(document.getElementById('root')!).render(<BrowserRouter><App/></BrowserRouter>);
