import { Skeleton } from "./frame";
import { type EvidenceGrade, type QuantityView, type RaceExtra, type RaceRow, type RaceStage } from "./api";
import { Reveal } from "./motion";
import type { ReactNode } from "react";
import { ErrorNote, relTime } from "./ui";
import { useRace } from './raceData';

const PROVIDER: Record<string, string> = { xiniu: "烯牛", pharmcube: "医药魔方", wind: "Wind" };
const BASIS: Record<string, string> = { storyline: "事件线索", attributed_fact: "已归属的事件", title: "公开报道" };
const RELATION: Record<string, string> = { direct: "直接竞品", substitute: "替代方案", adjacent: "产业关联", unknown: "关注同行" };
const REGION: Record<string, string> = { CN: "中国", US: "美国", EU: "欧洲", GLOBAL: "全球" };

export function StageScope({ stage }: { stage: RaceStage }) {
  const parts = [stage.scope || stage.title, ...(stage.context?.indication ?? []),
    ...(stage.context?.region ?? []).map(r => REGION[r] ?? r)].filter(Boolean);
  return parts.length ? <div className="muted small">{parts.join(" · ")}</div> : null;
}

export function PeerRelation({ row }: { row: Pick<RaceRow, "is_self" | "relation_type"> }) {
  return row.is_self ? null : <div className="muted small">{RELATION[row.relation_type ?? "unknown"] ?? "关注同行"}</div>;
}

/** Where a step comes from, stated as evidence (who reported it), never as a task for the reader. */
function basisText(s: NonNullable<RaceRow["stage"]>) {
  const outlets = s.outlets ?? [];
  if (s.basis === "title" && outlets.length > 1) return `${outlets.length} 家来源报道：${outlets.slice(0, 3).join("、")}`;
  if (s.basis === "title" && outlets.length === 1) return `来源：${outlets[0]}`;
  return BASIS[s.basis];
}

export function Grade({ grade }: { grade: EvidenceGrade }) {
  return <span className={`grade grade-${{ 公示: "public", 数据商: "vendor", 媒体: "media", 公司宣传: "pr" }[grade]}`}>{grade}</span>;
}

function Ladder({ steps, at }: { steps: string[]; at: number | null }) {
  return (
    <ol className="ladder" aria-label={at === null ? "阶段未知" : `已到 ${steps[at]}`}>
      {steps.map((s, i) => (
        <li key={s} className={at === null ? "" : i < at ? "done" : i === at ? "now" : ""} title={s}><span className="sr-only">{s}</span></li>
      ))}
    </ol>
  );
}

function since(months: number | null) {
  if (months === null) return null;
  if (months < 1) return { text: "本月", note: "刚完成", tone: "ok" };
  return { text: `${months} 个月`, note: "", tone: "" };
}

function Row({ row, steps }: { row: RaceRow; steps: string[] }) {
  const s = row.stage;
  const gap = since(row.months_since_funding);
  return (
    <tr className={row.is_self ? "race-self" : ""}>
      <th scope="row">
        <div className="race-name">{row.name}</div>
        <PeerRelation row={row} />
        {row.note && <div className="muted small">{row.note}</div>}
      </th>
      <td className={row.recent.stage ? "race-recent" : ""}>
        <Ladder steps={steps} at={s ? s.index : null} />
        {s ? <>
          <div className="race-line"><strong>{s.label}</strong><span className="muted small">{s.date}</span><Grade grade={s.grade} /></div>
          <StageScope stage={s} />
          <div className="muted small">
            {s.previous && <>上一级（{s.previous.label}）在 {s.previous.date.slice(0, 7)}{row.pace_months !== null && `，用时 ${row.pace_months} 个月`}<br /></>}
            {s.url ? <a href={s.url} target="_blank" rel="noreferrer noopener" className="race-src">{basisText(s)}</a> : basisText(s)}
          </div>
        </> : <div className="muted small race-line">阶段未知</div>}
      </td>
      <td className={row.recent.funding ? "race-recent" : ""}>
        {row.funding ? <>
          <div><strong>{row.funding.round}</strong>{row.funding.amount && ` · ${row.funding.amount}`}</div>
          <div className="muted small">{row.funding.date || "日期未披露"}{row.funding.lead && ` · ${row.funding.lead}`}</div>
          <div className="race-line">
            <Grade grade={row.funding.grade} />
            {row.funding.providers.length > 0 && <span className="muted small">{row.funding.providers.map((p) => PROVIDER[p] ?? p).join("、")}</span>}
            {row.funding.stale && <span className="muted small">历史结果</span>}
          </div>
        </> : <span className="muted small">暂无记录</span>}
      </td>
      <td>
        {gap ? <><div className="race-num">{gap.text}</div>{gap.note && <div className={`small race-${gap.tone}`}>{gap.note}</div>}</>
          : <span className="muted small">—</span>}
      </td>
      <td>
        {row.rd.collected ? <div className="race-num">{row.rd.patents ?? "—"}<span className="muted small"> · {row.rd.papers ?? "—"}</span></div>
          : <span className="muted small">未采集</span>}
      </td>
      <td className={row.recent.move ? "race-recent" : ""}>
        {row.move ? <>
          <div>{row.move.url ? <a href={row.move.url} target="_blank" rel="noreferrer noopener">{row.move.title}</a> : row.move.title}</div>
          <div className="race-line"><span className="muted small">{row.move.date}</span><Grade grade={row.move.grade} />
            {row.move.sources > 1 && <span className="muted small">{row.move.sources} 篇报道</span>}</div>
          {row.move.impact && <div className="race-sowhat">所以呢：{row.move.impact}</div>}
        </> : <span className="muted small">近 90 天无报道</span>}
      </td>
    </tr>
  );
}

function ExtraLine({ x, lead }: { x: RaceExtra; lead?: string }) {
  return <li>
    {lead && <strong>{lead} </strong>}
    {x.url ? <a href={x.url} target="_blank" rel="noreferrer noopener">{x.text}</a> : x.text}
    <span className="race-line muted small">{x.date && <span>{x.date}</span>}{x.source && <span>{x.source}</span>}<Grade grade={x.grade} /></span>
  </li>;
}

/** 拓展信息: team background and customers / partners per company, from public records only. */
function Extras({ rows }: { rows: RaceRow[] }) {
  const total = rows.reduce((n, r) => n + (r.extras?.team.length ?? 0) + (r.extras?.customers.length ?? 0), 0);
  return (
    <details className="race-extras">
      <summary>团队与客户 <span className="muted small">{total ? `${total} 条公开记录` : "暂无公开记录"}</span></summary>
      <div className="race-extras-grid">
        {rows.map((r) => (
          <article key={r.name} className={r.is_self ? "race-extra race-extra-self" : "race-extra"}>
            <h4>{r.name}</h4>
            <h5>团队</h5>
            {r.extras?.team.length ? <ul>{r.extras.team.map((x, i) => <ExtraLine key={i} x={x} />)}</ul>
              : <p className="muted small">暂无公开记录</p>}
            <h5>客户与合作方</h5>
            {r.extras?.customers.length ? <ul>{r.extras.customers.map((x, i) => <ExtraLine key={i} x={x} lead={x.what && x.who && !x.text.includes(x.who) ? `${x.who}（${x.what}）` : undefined} />)}</ul>
              : <p className="muted small">暂无公开记录</p>}
          </article>
        ))}
      </div>
    </details>
  );
}

export function RaceBoard({ projectId, revision }: { projectId: string; revision: string }) {
  const { data, error, loading, reload } = useRace(projectId, revision);
  if (loading && !data) return <div className="card card-pad" role="status" aria-label="整理同行进展"><Skeleton lines={6} /></div>;
  if (!data) return <div role="status"><ErrorNote error={error} /><button className="link small" onClick={() => void reload()}>重新读取</button></div>;
  if (data.rows.length < 2) {
    return <p className="muted small race-empty">暂无同行记录</p>;
  }
  // Peers with nothing to compare would only repeat "阶段未知 · 暂无记录"; they fold into one line.
  const isBlank = (r: RaceRow) => !r.is_self && !r.stage && !r.funding && !r.move && !(r.rd.collected && (r.rd.patents || r.rd.papers));
  const shown = data.rows.filter((r) => !isBlank(r));
  const blank = data.rows.filter(isBlank);
  return (
    <Reveal as="section" className="card card-pad race-board">
      {error ? <div role="status"><span>更新读取失败，保留上次内容。</span> <button className="link small" onClick={() => void reload()}>重试</button></div> : null}
      <div className="card-head race-head">
        <h3>同行进展</h3>
        <span className="muted small">各公司已披露的产品与进展</span>
        <span className="race-legend muted small">证据：<Grade grade="公示" /><Grade grade="数据商" /><Grade grade="媒体" /><Grade grade="公司宣传" /></span>
      </div>
      <div className="table-scroll">
        <table className="race" aria-label="同行进展">
          <thead><tr>
            <th scope="col">公司</th>
            <th scope="col">已披露阶段</th>
            <th scope="col">最近融资</th>
            <th scope="col">距上轮</th>
            <th scope="col">专利 · 论文<br /><span className="muted">近 12 个月</span></th>
            <th scope="col">最近关键动作</th>
          </tr></thead>
          <tbody>{shown.map((r) => <Row key={r.name} row={r} steps={data.steps} />)}</tbody>
        </table>
      </div>
      {blank.length > 0 && <p className="muted small race-blank">另有 {blank.length} 家暂无阶段、融资或近期动作记录：{blank.map((r) => r.name).join("、")}</p>}
      <Extras rows={data.rows} />
      <QuantityRecords data={data.quantities} />
      <p className="muted small race-foot" title="计划和预计不算已披露阶段；融资金额保留来源原文">
        {data.template_label} · 浅底为近 30 天变化 · 视图生成于 {relTime(data.generated_at)}
      </p>
    </Reveal>
  );
}

/** Original quantity records; no interpolated ranks or unqualified averages. */
export function QuantityRecords({ data }: { data?: QuantityView }) {
  if (!data?.records.length) return null;
  const scopeText = (scope: Record<string, string[]>) => ["period", "product", "segment", "region"]
    .flatMap(k => scope[k] ?? []).map(x => REGION[x] ?? x).join(" · ");
  return <details className="race-extras quantity-records">
    <summary>公开数字 <span className="muted small">{data.records.length} 条记录{data.groups.length ? ` · ${data.groups.length} 组同口径` : ""}</span></summary>
    {data.groups.map((g, i) => <section key={i}>
      <h4>{g.metric} · {scopeText(g.scope)}</h4>
      <div className="table-scroll"><table className="quantity-table" aria-label="同口径数字">
        <thead><tr><th scope="col">公司</th><th scope="col">披露数字</th><th scope="col">来源</th></tr></thead>
        <tbody>{g.records.map(r => <tr key={r.company}><th scope="row">{r.company}</th>
          <td>{r.raw_value}</td><td><a href={r.url} target="_blank" rel="noreferrer noopener">{r.source || "原文"}</a> · {r.date}</td></tr>)}</tbody>
      </table></div>
    </section>)}
    <h4>来源记录</h4>
    <ul>{data.records.map((r, i) => <li key={i}>
      <a href={r.url} target="_blank" rel="noreferrer noopener">{r.title}</a>
      <span className="race-line muted small">{r.date} · {r.source || "公开报道"}</span>
    </li>)}</ul>
  </details>;
}

/** One rule-written sentence plus figures, above the AI brief. */
export function PositionLine({ projectId, revision, more }: { projectId: string; revision: string; more?: ReactNode }) {
  const { data } = useRace(projectId, revision);
  const p = data?.position;
  if (!p || !p.sentence || data.rows.length < 2) return null;
  // Time since the last round alone says little; show the line only with a disclosed stage or peer movement.
  if (!p.parts.some((part) => part.kind === "position" || part.kind === "threat" || part.kind === "opening")) return null;
  return (
    <Reveal as="section" className="position-line">
      <div className="eyebrow">公开位置<span className="muted">规则整理 · 只用公开信息与专业数据源 · {relTime(data.generated_at)}</span></div>
      <p className="position-sentence">{p.sentence}</p>
      {p.chips.length > 0 && <dl className="position-chips">
        {p.chips.map((c) => <div key={c.label}><dt>{c.label}</dt><dd>{c.value}</dd>
          {c.note && <dd className={`position-note position-${c.tone || "flat"}`}>{c.note}</dd>}</div>)}
      </dl>}
      {more}
    </Reveal>
  );
}
