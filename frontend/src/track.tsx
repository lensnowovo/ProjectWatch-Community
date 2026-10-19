import { useEffect, useState } from "react";
import { api, type TrackCell, type TrackView } from "./api";
import { Reveal } from "./motion";
import { Grade } from "./race";
import { relTime, useAsync } from "./ui";
import { Figure, Skeleton, VerdictBand } from "./frame";
import { SignalListDrawer } from "./signalList";

const SERIES_NAME: Record<TrackCell["key"], string> = { demand: "需求增长报道", capital: "融资事件", price: "价格报道", policy: "重要政策" };

/** Quarterly counts as thin bars: one series, so no legend; each bar names its quarter and count on hover. */
function Bars({ cell }: { cell: TrackCell }) {
  const max = Math.max(1, ...cell.series.map((s) => s.value));
  const w = 240, h = 72, gap = 12, bw = (w - gap * (cell.series.length - 1)) / cell.series.length;
  return (
    <figure className="track-bars">
      <svg viewBox={`-8 0 ${w + 16} ${h + 14}`} role="img" aria-label={`${SERIES_NAME[cell.key]}，按季度：${cell.series.map((s) => `${s.label} ${s.value}`).join("，")}`}>
        <line x1="0" x2={w} y1={h} y2={h} className="track-axis" />
        {cell.series.map((s, i) => {
          const bh = s.value ? Math.max(3, (s.value / max) * (h - 4)) : 0;
          const x = i * (bw + gap);
          return (
            <g key={s.label}>
              <rect x={x} y={0} width={bw} height={h} className="track-hit"><title>{`${s.label}：${s.value} 条`}</title></rect>
              {bh > 0 && <path className="track-bar" d={`M${x},${h} v${-(bh - 2)} q0,-2 2,-2 h${bw - 4} q2,0 2,2 v${bh - 2} z`} />}
              <text x={x + bw / 2} y={h + 12} className="track-tick">{s.label}</text>
            </g>
          );
        })}
      </svg>
      <figcaption className="muted small">{SERIES_NAME[cell.key]} · 按季度</figcaption>
    </figure>
  );
}

function Cell({ cell, onOpen }: { cell: TrackCell; onOpen: (cell: TrackCell) => void }) {
  return (
    <article className="track-cell">
      <div className="track-cell-head">
        <span className="track-label">{cell.label}</span>
        <span className={`track-dir track-${cell.tone}`}>{cell.direction}</span>
      </div>
      <div className="track-value">{cell.value}{cell.sub && <span className="track-sub">{cell.sub}</span>}</div>
      {cell.series.some((s) => s.value) && <Bars cell={cell} />}
      {cell.for_us && <p className="track-forus">对公司：{cell.for_us}</p>}
      {cell.evidence.length > 0 ? <ul className="track-ev">{cell.evidence.map((e, i) => (
        <li key={i}>
          {e.url ? <a href={e.url} target="_blank" rel="noreferrer noopener">{e.title}</a> : e.title}
          <span className="muted small"> {e.date}{e.source && ` · ${e.source}`}</span> <Grade grade={e.grade} />
        </li>))}</ul>
        : <p className="muted small">已收录信息中还没有这类记录。</p>}
      <button className="link small track-more" onClick={() => onOpen(cell)}>看全部{cell.label}记录 →</button>
    </article>
  );
}

/** 赛道四格: demand, capital, price, policy — rule-read from stored, cited records. */
export function TrackBoard({ projectId, revision, onSettled }: { projectId: string; revision: string; onSettled?: () => void }) {
  const { data, error } = useAsync<TrackView>(() => api.track(projectId), [projectId, revision]);
  const [open, setOpen] = useState<TrackCell | null>(null);
  const settled = !!data || !!error;
  useEffect(() => { if (settled) onSettled?.(); }, [settled]); // eslint-disable-line react-hooks/exhaustive-deps
  // Hold the band's and the cells' place while loading, so the judgments below never jump down when it arrives.
  if (!data) return error ? null : <div role="status" aria-label="读取赛道信息"><section className="tf-band"><Skeleton lines={1} className="tf-skel-eyebrow" />
    <Skeleton lines={2} className="tf-skel-title" /><div className="tf-figures">{[0, 1, 2, 3].map((i) => <Skeleton key={i} lines={2} className="tf-skel-fig" />)}</div></section>
    <section className="track-board track-skel">{[0, 1, 2].map((i) => <Skeleton key={i} lines={6} className="card card-pad" />)}</section></div>;
  const blank = (c: TrackCell) => !c.evidence.length && c.series.every((s) => !s.value);
  if (data.cells.length && data.cells.every(blank)) {
    // No records at all: one quiet line instead of four empty charts.
    return (
      <Reveal as="section" className="card card-pad track-board track-empty">
        <div className="card-head"><h3>赛道在变大还是变挤</h3><span className="muted small">{data.industry && `${data.industry} · `}近 {data.quarters.length} 个季度 · 规则整理</span></div>
        <p className="muted small">{data.cells.map((c) => c.label).join("、")}：已收录信息中还没有这类记录。未收录不代表没有发生。</p>
      </Reveal>
    );
  }
  const shown = data.cells.filter((c) => !blank(c));
  return (
    <>
      <VerdictBand eyebrow={<>行业环境{data.balance && <span className="rj-verdict small-verdict">{data.balance}</span>}</>}
        meta={`${data.industry ? `${data.industry} · ` : ""}近 ${data.quarters.length} 个季度 · 规则整理 · ${relTime(data.generated_at)}`}
        title={data.summary || "赛道在变大还是变挤"}
        figures={<>{data.cells.map((c) => <Figure key={c.key} label={c.label} value={c.value || "—"} note={c.direction} />)}</>} />
      <SignalListDrawer projectId={projectId} cell={open} open={!!open} onClose={() => setOpen(null)} />
      <section className="track-board" aria-label="赛道在变大还是变挤">
        <div className={`track-grid track-n${shown.length}`}>{shown.map((c) => <Cell key={c.key} cell={c} onOpen={setOpen} />)}</div>
        {data.cells.some(blank) && <p className="muted small track-blank">{data.cells.filter(blank).map((c) => c.label).join("、")}：已收录信息中还没有这类记录，未收录不代表没有发生。</p>}
      </section>
    </>
  );
}
