// Synthetic fixture: header round line and 当前判断 reading order. Every API call is blocked except local stubs.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { MotionConfig } from 'motion/react';
import { api } from '../src/api';
import { CurrentBrief } from '../src/overview';
import { Progress } from '../src/pages/Project';
import '../src/styles.css';

const failedRound = new URLSearchParams(location.search).has('failed');
const at = (h: number) => new Date(Date.now() - h * 3600_000).toISOString();
for (const key of Object.keys(api)) (api as Record<string, unknown>)[key] = async () => { throw new Error('Preview blocks actual API calls'); };
const stage = (status: string, result: object | null) => ({job_id: 'j', status, stage: '', started_at: at(3), finished_at: at(2), error: '', result});
api.projectRound = async () => ({
  collect: stage('done', {status: 'partial', outcomes: {success: 18, failed: 1}}),
  analyze: stage(failedRound ? 'failed' : 'done', failedRound ? null : {status: 'complete', published: ['position', 'industry'],
    coverage: {candidates: 40, selected_materials: 31, fragments: 52, processed_fragments: 52, excluded: [{reason: 'irrelevant'}]}}),
  report: failedRound ? null : stage('done', {status: 'complete'}),
  materials: {paper: 4, patent: 2}, running: false, can_reanalyze: failedRound,
  quality: {current: {collect_job_id: 'j', analyze_job_id: 'a', finished_at: at(2), items: 38, date_coverage: .9, screened: 38,
    relevant_share: .6, matched_share: 1, sections: {published: 2, considered: 5, dropped_points: 1, rejected: []},
    requests: 64, unclear_requests: 0, model_input_tokens: 120000}, previous: null, warnings: [], stale_queries: []},
}) as never;
api.feed = async () => ([
  {id: 'e1', category: 'company', importance: 'major', title: '合成公司与区域医院签署三年供货协议', summary: '合成公司与区域医院签署三年供货协议',
    impact: '首个院端长期订单，验证了临床渠道。', published_at: at(30), url: 'https://example.invalid/a', source: '合成日报', source_count: 2},
  {id: 'e2', category: 'company', importance: 'notable', title: '合成公司完成产线二期设备安装', summary: '合成公司完成产线二期设备安装',
    impact: '产能爬坡的前置条件已具备。', published_at: at(200), url: 'https://example.invalid/b', source: '合成财经', source_count: 1},
]) as never;
api.insight = async () => ({record: null, generation_available: true, model_ready: true, section_states: {}, labels: {}, sources: {}}) as never;
api.questions = async () => ({questions: [], statuses: {}}) as never;
const detail = {
  project: {id: 'synthetic', name: '合成医疗', short_name: '合成医疗', status: 'active', execution_mode: 'api', activated: true,
    last_external_at: at(2), note: ''},
  jobs: [], documents: [], counts: {company: 12}, capabilities: {insight_generation: true, insight_projections: false},
  brief: {summary: '合成医疗处于商业化早期，院端订单开始落地，产能建设同步推进。', tone: 'positive', updated_at: at(2), watch: [{text: '二期产线投产时间'}]},
  page_summaries: {external: {text: '合成医疗签下首个院端三年供货协议，二期产线设备已安装。订单与产能同时推进，商业化进入验证期。',
    date: at(2), refs: [], historical: false}},
  observations: {items: []},
} as never;

createRoot(document.getElementById('root')!).render(<BrowserRouter><MotionConfig reducedMotion="always">
  <main className="main"><div className="page page-wide page-project">
    <header><p className="muted small">合成数据 · 不连接实际实例{failedRound ? ' · 分析失败' : ''}</p><h1>合成医疗</h1>
      <Progress detail={detail} onReload={() => {}} /></header>
    <CurrentBrief detail={detail} goto={() => {}} onReload={() => {}} />
  </div></main>
</MotionConfig></BrowserRouter>);
