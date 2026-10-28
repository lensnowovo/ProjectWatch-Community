// Synthetic fixture. Every API method is blocked except the two local read stubs.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { MotionConfig } from 'motion/react';
import { api } from '../src/api';
import { PlanView } from '../src/plan';
import '../src/styles.css';

const plan = {
  items: Array.from({length: 7}, (_, n) => ({id: `synthetic-${n}`, status: 'active', section: 'product', tier: 'core',
    text: `合成产品 ${n + 1} 的注册与商业化进展是否达到已披露里程碑？`, reason: '相同技术路线的产品进展影响竞争位置',
    label: '产品进展', objects: ['合成主体'], queries: [], cadence: 'medium', plan_origin: 'system', view: 'evidence',
    created_at: '2026-10-01T00:00:00Z'})),
  framework: [{key: 'product', label: '资产与产品', ring: 'company'}], rings: [{key: 'company', label: '公司自身'}],
  modules: {}, module_labels: {}, templates: [], latest: {}, proposals: [], changes: [],
  labels: {origin: {system: '系统'}, tier: {core: '核心'}, cadence: {medium: '常规', low: '低频'}},
  meta: {templates: [], understanding: '', method: '合成验收，不连接实际实例'},
  coverage: {product: Array.from({length: 14}, (_, n) => ({key: `c${n}`, state: 'on', label: '合成来源'}))},
  state: {phase: 'growing', label: '生长中', revisions: 1}, week: {fresh: 3},
  health: {items: 7, ran_14d: 7, hit_30d: 4, never_hit: 3, system_added: 7, system_reverted: 0,
    proposals: {accepted: 0, rejected: 0}},
};
for (const key of Object.keys(api)) api[key] = async () => { throw new Error('Preview blocks actual API calls'); };
api.monitoringPlan = async () => plan;
api.signals = async () => ({cadence: {hours: 12}});
const detail = {project: {id: 'synthetic', name: '合成技术', status: 'active', execution_mode: 'api', activated: true},
  jobs: [], documents: [], counts: {company: 100, industry: 67}, observations: {items: []}, event_states: {items: []},
  monitoring_progress: {state: 'observed', round: 2, completed_at: '2026-10-01T00:00:00Z'}};

createRoot(document.getElementById('root')!).render(<BrowserRouter><MotionConfig reducedMotion="always">
  <div style={{display: 'flex', minHeight: '100vh'}}>
    <aside className="sidebar"><h2>ProjectWatch</h2><p className="muted">合成数据 · 隔离预览</p></aside>
    <main className="main"><div className="page page-project">
      <header><p className="muted small">不连接实际实例</p><h1>合成技术</h1></header>
      <div className="tabs-row"><nav className="tabs"><button>当前判断</button><button>项目总览</button>
        <button>内部信息</button><button>外部信息</button><button className="active">监控范围</button></nav></div>
      <PlanView projectId="synthetic" company="合成技术" detail={detail} />
    </div></main>
  </div>
</MotionConfig></BrowserRouter>);
