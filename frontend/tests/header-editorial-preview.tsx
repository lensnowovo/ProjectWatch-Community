// Entirely synthetic design fixture. No API imports, persistence, or provider calls.
import React, { useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { useReducedMotion } from 'motion/react';
import Aurora from '../src/bits/Aurora';
import '../src/styles.css';
import './header-editorial-preview.css';
import OverviewEditorial from './overview-editorial-preview';

const overviewPreview = location.pathname.endsWith('/overview-editorial-preview.html');

type Scene = 'partial' | 'running' | 'sparse';
const news = [
  {type: '产品进展', date: '2026.10.18', title: '植入式系统完成首例临床入组', note: '进入临床验证阶段，尚未取得上市许可。', source: '公司公告 · 模拟来源', quote: '公司宣布植入式神经接口系统完成首例临床研究受试者入组。该产品处于临床研究阶段，尚未获得上市许可。'},
  {type: '融资进展', date: '2026.09.26', title: '完成新一轮融资，用于临床与研发', note: '轮次及金额未在这份公告中披露。', source: '融资公告 · 模拟来源', quote: '本轮融资已完成，资金将用于临床研究及产品研发。本公告未披露具体融资轮次与金额。'},
];

function Preview() {
  const [scene, setScene] = useState<Scene>('partial');
  const [dismissed, setDismissed] = useState(false);
  const [selected, setSelected] = useState(0);
  const dialog = useRef<HTMLDialogElement>(null);
  const reduced = useReducedMotion();
  const sparse = scene === 'sparse';
  const running = scene === 'running';
  const openSource = (index: number) => { setSelected(index); dialog.current?.showModal(); };
  return <div className="hp-shell">
    <aside className="hp-side">
      <div className="hp-brand"><span aria-hidden="true">P</span><div>ProjectWatch<small>项目监控</small></div></div>
      <button className="hp-add" disabled>＋ 添加项目</button>
      <nav aria-label="全站导航"><span>今日动态</span><span>项目组合</span><span>周报</span></nav>
      <p className="hp-side-label">关注的公司</p><div className="hp-side-selected">澄序医疗 <i /></div>
      <div className="hp-side-bottom">设计预览<small>无真实项目连接</small></div>
    </aside>
    <main className="hp-main">
      <div className="hp-preview-tools"><span><i /> 设计预览 · 全部为虚构示例</span><label>场景 <select aria-label="预览场景" value={scene} onChange={e => {setScene(e.target.value as Scene); setDismissed(false);}}><option value="partial">部分完成</option><option value="running">正在更新</option><option value="sparse">初建项目</option></select></label></div>
      <header className="hp-hero">
        <div className="hp-light" aria-hidden="true">{!reduced && <Aurora colorStops={['#e9e2d6','#c9d1d0','#e5dccb']} amplitude={.5} blend={.84} speed={.16} lightMode />}</div>
        <div className="hp-top"><span>‹ &nbsp; 项目组合</span><div className="hp-actions"><button disabled title="仅预览，不上传资料">＋ 上传资料</button><button disabled title="仅预览，不触发更新">立即更新</button><button disabled title="仅预览，不改变暂停状态">暂停监控</button></div></div>
        <div className="hp-content">
          <section className="hp-identity" aria-label="项目速览">
            <div className="hp-name-row"><h1>澄序医疗</h1><span>医疗器械</span></div>
            <p className="hp-legal">澄序神经科技（上海）有限公司 · 虚构主体</p>
            <p className="hp-description">面向神经功能重建，研发脑机接口系统<br className="hp-break"/>与临床神经信号采集设备。</p>
            <div className="hp-meta"><span>上海</span><span>脑机接口 / 神经工程</span><button className="hp-text-link" onClick={() => openSource(2)}>公司档案 ↗</button></div>
            {!sparse && <dl className="hp-facts">
              <div><dt>核心产品</dt><dd>神经接口系统</dd><small>植入式研发 · 非植入式采集</small></div>
              <div><dt>关键人物</dt><dd>林知远 <span>创始人</span></dd><small>神经工程背景 · 模拟履历</small></div>
              <div><dt>产品阶段</dt><dd>临床研究</dd><small>植入式系统 · 截至 2026.10</small></div>
            </dl>}
            {sparse && <p className="hp-pending">已确认主体，产品与团队资料正在补充。</p>}
          </section>
          <section className="hp-news" aria-label="最近变化">
            <div className="hp-news-heading"><h2>最近变化</h2><span>公司自身</span></div>
            {sparse ? <div className="hp-news-empty"><span className="hp-pulse"/> 正在整理首轮公开信息<p>取得可用来源后，进展会出现在这里。</p></div> : news.map((item,index) => <button className="hp-news-item" key={item.title} onClick={() => openSource(index)}>
              <div className="hp-news-meta"><span>{item.type}</span><time>{item.date}</time></div>
              <h3>{item.title}<span aria-hidden="true">↗</span></h3><p>{item.note}</p><small>{item.source}</small>
            </button>)}
          </section>
        </div>
        <details className={`hp-status ${running || sparse ? 'is-running' : ''}`} key={scene}>
          <summary><span className="hp-status-copy"><i className={running || sparse ? 'hp-pulse' : 'hp-dot'} /><strong>{running ? '正在更新竞争信息' : sparse ? '正在补全外部信息' : '部分完成'}</strong><span>{running ? '已保留上一轮结果' : sparse ? '已确认公司主体' : '商业来源暂不可用 · 链路尚未完成'}</span></span><span className="hp-status-right">{!sparse && <span>内容更新于 10:24</span>}<span className="hp-status-link">运行详情 <b>⌄</b></span></span></summary>
          <div className="hp-status-body">
            <ol className="hp-steps">{['采集','解析','判断','修订方案'].map((step,index) => <li key={step} className={(running || sparse) && index === 0 ? 'current' : !sparse && index < 2 ? 'done' : ''}><span>0{index+1}</span>{step}<small>{(running || sparse) && index === 0 ? '进行中' : !sparse && index < 2 ? '已有结果' : '等待本轮更新'}</small></li>)}</ol>
            <div className="hp-status-note">{running || sparse ? '当前为模拟运行状态；本预览不会调用模型或搜索服务。' : <><p>公开信息已整理；商业来源连接失败，本轮判断与方案修订尚未完成。</p><p>已保存结果可继续查看。真实环境中的恢复操作仍需单独确认。</p><button className="hp-text-link" disabled={dismissed} onClick={() => setDismissed(true)}>{dismissed ? '已忽略提醒 · 未恢复任务' : '忽略本次提醒'}</button></>}</div>
          </div>
        </details>
      </header>
      <nav className="hp-tabs" aria-label="项目页签">{['当前判断','项目总览','内部信息','外部信息','监控范围'].map((tab,index) => <button key={tab} aria-current={index === 1 ? 'page' : undefined} onClick={() => document.getElementById('hp-context')?.scrollIntoView({behavior: reduced ? 'auto' : 'smooth',block:'nearest'})}><small>0{index+1}</small>{tab}</button>)}</nav>
      {overviewPreview ? <OverviewEditorial sparse={sparse} /> : <section className="hp-context" id="hp-context"><div><p className="hp-eyebrow">公司档案</p><h2>从研发产品，到临床应用</h2><p>神经信号采集设备面向科研与临床研究；植入式产品仍处于验证阶段。<br/>不同产品的进展与商业化状态分别跟踪。</p></div><div className="hp-context-aside"><span>主要应用</span><strong>神经康复 · 临床研究</strong><span>行业与赛道</span><strong>医疗器械 / 脑机接口</strong></div></section>}
      <p className="hp-footnote">本页仅评审{overviewPreview ? '项目总览与 header' : ' header'}布局。公司、人物、产品、事件与运行状态均为模拟，不构成真实公司资料。</p>
    </main>
    <dialog ref={dialog} className="hp-dialog" aria-labelledby="hp-dialog-title" onClick={e => {if(e.target === dialog.current) dialog.current.close();}}>
      <div className="hp-dialog-top"><span>模拟来源</span><button autoFocus aria-label="关闭来源" onClick={() => dialog.current?.close()}>×</button></div>
      <h2 id="hp-dialog-title">{news[selected]?.title ?? '澄序医疗 · 公司档案'}</h2><p>{news[selected]?.quote ?? '虚构公司用于展示业务介绍、核心产品、关键人物与产品阶段的排版。本预览不读取真实项目。'}</p>
      <small>{news[selected]?.date ?? '2026.10'} · 合成设计样本，没有外部原文链接</small>
    </dialog>
  </div>;
}
createRoot(document.getElementById('root')!).render(<Preview />);
