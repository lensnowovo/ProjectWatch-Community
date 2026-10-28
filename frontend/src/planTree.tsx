import { useId, useMemo, useState, type CSSProperties } from 'react';
import type { MonitoringPlanView, PlanItem } from './api';
import { layoutTree, polar, ringArc, tangentArc, wedge } from './planTreeLayout';
import { treeModel } from './planTreeModel';
import { publicLink } from './relationScope';
import './planTree.css';

const short = (v: string, n = 10) => v.length > n ? `${v.slice(0,n)}…` : v;
const ROLES = {relation:'长期关联',question:'临时研究',legacy:'原有关注点'};
const BASIS: Record<string,string> = {summary:'标题 / 摘要关联',full_body:'正文关联',partial_body:'部分正文关联',search_summary:'搜索摘要关联',accepted_hit:'已收录匹配'};
const DENSE = 5;

/** Display the saved wording verbatim; expanding never generates a new summary. */
function SavedCopy({text}: {text: string}) {
  const [expanded,setExpanded] = useState(false);
  return <><div className={!expanded&&text.length>110?'pt-copy limited':'pt-copy'}>{text}</div>
    {text.length>110&&<button className="link small pt-copy-toggle" aria-expanded={expanded} onClick={()=>setExpanded(!expanded)}>{expanded?'收起':'查看全文'}</button>}</>;
}

export function PlanChainTree({company, plan, paused = false, onOpen}: {
  company: string; plan: MonitoringPlanView; paused?: boolean; onOpen?: (id: string) => void;
}) {
  const [showClosed,setShowClosed] = useState(false);
  const [selected,setSelected] = useState<string | null>(null);
  const [hover,setHover] = useState<string | null>(null);
  const [direction,setDirection] = useState<string | null>(null);
  const [graphExpanded,setGraphExpanded] = useState(false);
  const uid = useId().replace(/:/g,'');
  const model = useMemo(()=>treeModel(plan.items,plan.explorations,plan.tree_signals,showClosed),[plan,showClosed]);
  const byId = new Map(model.items.map(i=>[i.id,i]));
  const latest = Object.fromEntries(model.items.map(i=>[i.id,{kind:i.evidence?'hit' as const:'none' as const,fresh:i.fresh}]));
  const layout = useMemo(()=>layoutTree({rings:plan.rings ?? [], framework:plan.framework,items:model.items,
    latest,maxHop:Math.max(2,...model.items.map(i=>Number(i.hop)))}),[plan,model]);
  const dense = new Set(layout.dims.filter(d=>d.count>DENSE).map(d=>d.key));
  const focus = selected ? byId.get(selected) : undefined;
  const hovered = hover ? byId.get(hover) : undefined;
  const highlight = hovered ?? focus;
  const effectiveDirection = direction && plan.framework.some(d=>d.key===direction) ? direction : null;
  const focusIds = new Set<string>();
  if (highlight) {
    let cur: PlanItem | undefined = highlight;
    while(cur && !focusIds.has(cur.id)) {focusIds.add(cur.id);cur=byId.get(cur.parent_id ?? '');}
  }
  const litDims = new Set(model.items.filter(i=>focusIds.has(i.id)).map(i=>i.section));
  if (!highlight && effectiveDirection) litDims.add(effectiveDirection);
  const litRings = new Set(plan.framework.filter(f=>litDims.has(f.key)).map(f=>f.ring));
  if (!highlight && effectiveDirection) for (const i of model.items) if(i.section===effectiveDirection) focusIds.add(i.id);
  const on = (id: string) => !(highlight || effectiveDirection) || focusIds.has(id) || litDims.has(id) || litRings.has(id) || id==='center' ? '' : ' off';
  const signal = focus && plan.tree_signals?.[focus.id];
  const parent = focus?.parent_id ? byId.get(focus.parent_id) : undefined;
  const exploration = focus && plan.explorations?.[focus.id];
  const directionItems = effectiveDirection ? model.items.filter(i=>i.section===effectiveDirection) : [];
  const choose = (id: string) => {setSelected(id);setDirection(null);setHover(null);};
  const chooseDirection = (key: string) => {setDirection(key);setSelected(null);setHover(null);};
  const reset = () => {setSelected(null);setDirection(null);setHover(null);};
  const delay = (ms:number)=>({'--d':`${ms}ms`}) as CSSProperties;
  const closedCount = plan.items.filter(i=>i.status==='retired').length;
  const graphIds = new Set(layout.items.map(i=>i.id));
  const missing = model.items.filter(i=>!graphIds.has(i.id));
  return <section className="scope plan-tree" aria-label="关联监控树" onKeyDown={e=>{if(e.key==='Escape'){reset();}}}>
    <header className="scope-head">
      <h3>关联监控</h3><span className="muted small">{model.items.filter(i=>i.role==='relation'&&!i.contextOnly).length} 个对象 · {model.items.filter(i=>i.role==='question'&&!i.contextOnly).length} 个研究分支{model.items.some(i=>i.role==='legacy') ? ` · ${model.items.filter(i=>i.role==='legacy').length} 个原有关注点` : ''}</span>
      {closedCount>0 && <label className="pt-closed small"><input type="checkbox" checked={showClosed} onChange={e=>setShowClosed(e.target.checked)}/>包含已关闭 {closedCount}</label>}
    </header>
    <div className="pt-toolbar">
      <details className="pt-help small"><summary>图例与说明</summary><p>圆点是长期关联，菱形是临时研究，方点是原有关注点。研究分支不增加对象关联阶数；分类扇区不是事实关系。</p><p>金色表示有已保存的匹配证据，不表示利好或结论已证实；绿色外圈表示近 7 日收录。关注状态不代表此刻正在执行任务。</p></details>
      <div className="pt-toolbar-actions"><button className="link small pt-mobile-toggle" aria-expanded={graphExpanded} aria-controls={`${uid}-graph`} onClick={()=>setGraphExpanded(!graphExpanded)}>{graphExpanded?'收起关联图':'展开关联图'}</button>
      {(focus||effectiveDirection)&&<button className="link small" onClick={reset}>返回全部</button>}</div>
    </div>
    {paused&&<p className="pt-warning small">项目已暂停，以下为已保存的范围与结果。</p>}
    {model.warnings.map(w=><p className="pt-warning small" key={w}>{w}</p>)}
    {!plan.tree_signals && <p className="pt-warning small">当前接口尚未提供证据统计，不以历史命中数代替。</p>}
    <div className="pt-body">
      <figure id={`${uid}-graph`} className={`pt-figure${graphExpanded?' expanded':''}`}>
        {hovered&&hovered.id!==selected&&<div className="pt-hover-note" role="tooltip"><strong>{hovered.name}</strong><span>{ROLES[hovered.role]} · {hovered.stateLabel}</span><small>点击固定详情</small></div>}
        <div className="pt-viewport">
        <svg className={`pt-svg${highlight||effectiveDirection?' focusing':''}`} viewBox={`${-layout.width/2} ${-layout.height/2} ${layout.width} ${layout.height}`}
          role="group" aria-label={`${company}的关联与研究分支`} onMouseLeave={()=>setHover(null)}>
          <g aria-hidden="true">
            {layout.rings.map(r=><g key={r.key} className={on(r.key)}>
              <path className="pt-territory" d={wedge(r.span[0],r.span[1],layout.radii.ring+14,layout.radii.territory)}/>
              <path className="pt-bracket" d={ringArc(r.span[0]+1,r.span[1]-1,layout.radii.territory)}/>
              <circle className="pt-ring" cx={r.at.x} cy={r.at.y} r={7}/>
              <text className="pt-ring-label" x={r.label_at.x} y={r.label_at.y} textAnchor={r.anchor}>{r.label}</text>
            </g>)}
            {layout.radii.levels.map((r,h)=><g key={r}><circle className="pt-guide" r={r}/>
              <path id={`${uid}-level-${h}`} d={tangentArc(layout.levelLabelDeg,r,30)} fill="none"/>
              <text className="pt-level-label" dy={-6}><textPath href={`#${uid}-level-${h}`} startOffset="50%" textAnchor="middle">{h===0?'原有关注':h===1?'直接关联':`${h} 阶关联`}</textPath></text>
            </g>)}
            {layout.dims.map(d=><g key={d.key} className={on(d.key)}>
              {d.planned_r!==null && <path className="pt-planned" d={ringArc(d.span[0]+2,d.span[1]-2,d.planned_r)}/>}
              {d.reach_r!==null && <><path className="pt-petal" d={wedge(d.span[0]+2,d.span[1]-2,layout.radii.dim+10,d.reach_r)}/>
                <path className="pt-frontier" d={ringArc(d.span[0]+2,d.span[1]-2,d.reach_r)}/></>}
            </g>)}
            {layout.edges.filter(e=>!dense.has(byId.get(e.to)?.section ?? '')&&!dense.has(byId.get(e.from)?.section ?? '')).map(e=><path key={e.key} d={e.d} pathLength={1} style={delay(e.kind==='ring'?0:e.kind==='dim'?100:200)}
              className={`pt-edge ${e.kind}${e.lit?' lit':''}${byId.get(e.to)?.role==='question'?' research':''}${on(e.to)}`}/>)}
          </g>
          <g className="pt-center-control" role="button" tabIndex={0} aria-label="返回全部关联" onClick={reset} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();reset();}}}>
            <circle className="pt-hit" r={29}/><circle className="pt-center-ring" r={21}/><circle className="pt-center" r={14}/>
            <text className="pt-center-label" y={42} textAnchor="middle">{short(company||'公司',8)}</text>
          </g>
          {layout.dims.map(d=>{
            const named = model.items.filter(i=>i.section===d.key && i.role==='relation');
            return <g key={d.key} role="button" tabIndex={0} className={`pt-direction${effectiveDirection===d.key?' selected':''}${on(d.key)}`} aria-label={`${d.label}，${d.count} 个节点，查看列表`} aria-pressed={effectiveDirection===d.key}
              onClick={()=>chooseDirection(d.key)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();chooseDirection(d.key);}}}>
              <circle className="pt-hit" cx={d.at.x} cy={d.at.y} r={15}/><circle className={`pt-dim${d.reached!==null?' lit':''}`} cx={d.at.x} cy={d.at.y} r={5.5}/>
              <text className="pt-dim-label" x={d.label_at.x} y={d.label_at.y} textAnchor={d.anchor}>{d.label}{d.count?` ${d.count}`:''}</text>
              {named[0]&&<text className="pt-object-label" x={d.label_at.x} y={d.label_at.y+21} textAnchor={d.anchor}>{short(named[0].name,10)}</text>}
              {named.length>1&&<text className="pt-more-label" x={d.label_at.x} y={d.label_at.y+40} textAnchor={d.anchor}>查看另 {named.length-1} 个对象 →</text>}
              {dense.has(d.key)&&(()=>{const p=polar(d.deg,layout.radii.levels[1]);return <g>
                <path className="pt-bundle-edge" d={`M ${d.at.x} ${d.at.y} L ${p.x} ${p.y}`}/><circle className="pt-bundle" cx={p.x} cy={p.y} r={19}/>
                <text className="pt-bundle-label" x={p.x} y={p.y+5} textAnchor="middle">{d.count}</text></g>})()}
            </g>;
          })}
          {layout.items.filter(n=>!dense.has(n.section)).map(n=>{
            const i=byId.get(n.id)!; const state=plan.explorations?.[n.id]?.state;
            const r=i.role==='question'?6:i.evidence?7:5;
            return <g key={n.id} className={`pt-node ${i.role}${i.evidence?' evidence':''}${i.contextOnly?' context':''}${i.status==='retired'?' closed':''}${state==='stopped'?' stopped':''}${on(n.id)}`}
              role="button" tabIndex={0} aria-pressed={selected===n.id} aria-label={`${ROLES[i.role]}：${i.name}，${i.stateLabel}${i.fresh?'，近七日有收录':''}`}
              onMouseEnter={()=>setHover(n.id)} onMouseLeave={()=>setHover(null)} onFocus={()=>setHover(n.id)} onBlur={()=>setHover(null)}
              onClick={()=>choose(n.id)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();choose(n.id);}}}>
              <circle className="pt-hit" cx={n.at.x} cy={n.at.y} r={13}/>
              {i.fresh&&<circle className="pt-halo" cx={n.at.x} cy={n.at.y} r={r+5}/>}
              {i.role==='question'?<path className="pt-dot" d={`M ${n.at.x} ${n.at.y-r} l ${r} ${r} l ${-r} ${r} l ${-r} ${-r} Z`}/>:
                i.role==='legacy'?<rect className="pt-dot" x={n.at.x-r} y={n.at.y-r} width={r*2} height={r*2} rx={2}/>:<circle className="pt-dot" cx={n.at.x} cy={n.at.y} r={r}/>}
            </g>;
          })}
        </svg></div>
        <figcaption className="pt-legend small"><span>● 长期对象</span><span>◇ 临时研究</span><span>□ 原有关注</span><span className="pt-new">◎ 近 7 日收录</span></figcaption>
        {dense.size>0&&<p className="pt-footnote small muted">数字节点已聚合，点击查看全部条目。</p>}
      </figure>
      <aside className="pt-detail" aria-label="关联详情" aria-live="polite">
        {focus ? <>
          <div className="pt-detail-meta"><span>{ROLES[focus.role]}</span><span className={`pt-state ${exploration?.state ?? ''}`}>{focus.stateLabel}</span></div>
          <h3>{focus.name}</h3>
          {parent&&<p className="small muted">源于 <button className="link" onClick={()=>choose(parent.id)}>{parent.name}</button>{parent.status==='retired'?'（已关闭，保留关联来源）':''}</p>}
          <dl key={focus.id}>
            {focus.current_state&&<div><dt>当前认识</dt><dd><SavedCopy text={focus.current_state}/></dd></div>}
            <div><dt>纳入原因</dt><dd><SavedCopy text={focus.reason||'尚未记录纳入原因'}/></dd></div>
            <div><dt>最近收录</dt><dd>{signal?.count ? <><a href={publicLink(signal.url)} target="_blank" rel="noreferrer">{signal.title||'已有匹配记录，日期未确认'}</a>
              <small>{signal.recorded_at?.slice(0,10)||'日期未记录'} · {signal.count} 条匹配记录{signal.basis?` · ${BASIS[signal.basis]??signal.basis}`:''}</small></>:'暂无已保存的匹配证据'}</dd></div>
          </dl>
          {(exploration||focus.role==='question')&&<details key={`research-${focus.id}`} className="pt-research-details"><summary>研究说明与结束条件</summary>{exploration?.label&&<p>{exploration.label}</p>}<p>{focus.exit_rule||'尚未记录结束条件'}</p></details>}
          {model.items.some(i=>i.parent_id===focus.id)&&<div className="pt-related"><h4>关联与研究分支</h4>{model.items.filter(i=>i.parent_id===focus.id).map(i=><button key={i.id} onClick={()=>choose(i.id)}><span className="pt-list-heading"><span>{i.role==='question'?'◇':'●'} {i.name}</span><small>{i.stateLabel}</small></span><span className="pt-list-reason">{i.reason||'尚未记录纳入原因'}</span></button>)}</div>}
          {onOpen&&focus.status!=='retired'&&<button className="link small" onClick={()=>onOpen(focus.id)}>在表格中查看依据与操作 →</button>}
        </> : <>
          <div className="pt-detail-meta">{effectiveDirection?'方向内的全部节点':'关注范围'}</div>
          <h3>{plan.framework.find(d=>d.key===effectiveDirection)?.label||'由公司，向外延伸'}</h3>
          {effectiveDirection&&!directionItems.length?<p className="pt-empty">这个方向尚未纳入关联对象。常规采集是否覆盖该方向，请查看表格中的采集配置。</p>:
            <p className="muted small">{effectiveDirection?'以下为这个方向的全部条目。':'关注对象及其纳入原因'}</p>}
          <div className="pt-related">{(effectiveDirection?directionItems:model.items.filter(i=>!i.parent_id)).map(i=><button key={i.id} onClick={()=>choose(i.id)}><span className="pt-list-heading"><span>{i.role==='question'?'◇':i.role==='legacy'?'□':'●'} {i.name}</span><small>{i.stateLabel}{i.contextOnly?' · 来源节点':''}</small></span><span className="pt-list-reason">{i.reason||'尚未记录纳入原因'}</span></button>)}</div>
          {!model.items.length&&<p className="muted">还没有关联对象，常规采集配置仍保留在表格中。</p>}
        </>}
        {missing.length>0&&<details><summary>未归类 {missing.length} 个</summary>{missing.map(i=><button key={i.id} className="link" onClick={()=>choose(i.id)}>{i.name}</button>)}</details>}
      </aside>
    </div>
  </section>;
}
