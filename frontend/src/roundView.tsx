import { useState } from "react";
import { api, type ProjectRound, type RoundFigures, type RoundQuality, type RoundStage } from "./api";
import { useToast } from "./motion";
import { useVisiblePoll } from "./Operational";
import { ErrorNote, relTime, useAsync } from "./ui";
import "./roundView.css";

const CATEGORY = { paper: "论文", patent: "专利", trial: "临床" } as const;

function collectLine(stage: RoundStage | null): string {
  if (!stage) return "尚未更新";
  if (stage.status === "queued" || stage.status === "running") return stage.stage || "正在收录";
  if (stage.status === "failed") return "收录未完成，已收录的内容保留";
  const result = stage.result;
  if (!result) return "已结束";
  const failed = result.outcomes?.failed ?? 0;
  return result.status === "complete" ? "全部来源已收录"
    : failed ? `部分完成 · ${failed} 个来源暂不可用` : "部分完成";
}

function analyzeLine(stage: RoundStage | null, collect: RoundStage | null): string {
  if (!stage) return collect && collect.status === "done" ? "等待分析" : "—";
  if (stage.status === "queued") return "等待分析";
  if (stage.status === "running") return stage.stage || "正在分析";
  if (stage.status === "failed") return "分析未完成，已收录材料保留";
  const result = stage.result;
  if (!result) return "已结束";
  if (result.status === "waiting_for_model") return "等待配置模型后分析";
  const sections = result.published?.length ?? 0;
  return result.status === "complete" ? `已完成 · ${sections} 个判断栏目已更新`
    : sections ? `部分完成 · ${sections} 个判断栏目已更新` : "部分完成 · 暂无判断栏目更新";
}

/** What the latest update produced, stage by stage; collected material counts even before analysis. */
export function RoundProgress({ projectId, onReload }: { projectId: string; onReload: () => void }) {
  const { data, reload } = useAsync<ProjectRound>(() => api.projectRound(projectId), [projectId]);
  useVisiblePoll(reload, data?.running ? 5000 : 30000);
  if (!data) return null;
  // Before analysis everything is collected but unscreened; afterwards show what review confirmed.
  const analysed = data.analyze?.status === 'done';
  const shown = analysed ? data.materials : (data.materials_collected ?? data.materials);
  const counts = (Object.keys(CATEGORY) as (keyof typeof CATEGORY)[])
    .filter((key) => (shown[key] ?? 0) > 0)
    .map((key) => `${CATEGORY[key]} ${shown[key]}`);
  const unreviewed = analysed ? (Object.keys(CATEGORY) as (keyof typeof CATEGORY)[])
    .reduce((sum, key) => sum + (data.materials_unreviewed?.[key] ?? 0), 0) : 0;
  const finished = data.report?.finished_at ?? data.analyze?.finished_at ?? data.collect?.finished_at;
  return <section className="round-view" aria-label="最近一次更新">
    <dl>
      <div><dt>收录</dt><dd>{collectLine(data.collect)}{counts.length > 0 && <span className="muted"> · {analysed ? "已确认" : "已收录"} {counts.join(" · ")}</span>}
        {unreviewed > 0 && <span className="muted"> · 另 {unreviewed} 条待核实</span>}</dd></div>
      <div><dt>分析</dt><dd>{analyzeLine(data.analyze, data.collect)}</dd></div>
      {finished && !data.running && <div><dt>完成</dt><dd className="muted">{relTime(finished)}</dd></div>}
    </dl>
    {data.can_reanalyze && <Reanalyze projectId={projectId} onQueued={() => { reload(); onReload(); }} />}
    {data.quality && <QualityReport quality={data.quality} />}
  </section>;
}

const pct = (v: number | null | undefined) => v == null ? "—" : `${Math.round(v * 100)}%`;
const ROWS: [string, (f: RoundFigures) => string][] = [
  ["新收录条目", (f) => String(f.items)],
  ["新闻有发布日期", (f) => pct(f.date_coverage)],
  ["已初筛 / 判为相关", (f) => f.screened ? `${f.screened} / ${pct(f.relevant_share)}` : "—"],
  ["初筛结果能对应原文", (f) => pct(f.matched_share)],
  ["分析板块发布", (f) => f.sections ? `${f.sections.published} / ${f.sections.considered}${f.sections.unchanged?.length ? `（另 ${f.sections.unchanged.length} 个无新材料，沿用）` : ""}` : "—"],
  ["删去的论点", (f) => f.sections ? String(f.sections.dropped_points) : "—"],
  ["请求 · 结果不明", (f) => `${f.requests} · ${f.unclear_requests}`],
  ["模型输入 token", (f) => f.model_input_tokens.toLocaleString()],
];
const SOURCE: Record<string, string> = { openalex_topic: "论文主题检索", europepmc_topic: "论文主题检索（Europe PMC）", search: "网页检索" };

/** Counts over this round's own records, against the round before; no judgment of the facts. */
function QualityReport({ quality }: { quality: RoundQuality }) {
  const { current, previous, warnings, stale_queries: stale } = quality;
  return <details className="round-quality" open={warnings.length > 0 || stale.length > 0}>
    <summary className="small">本轮质量{warnings.length ? ` · ${warnings.length} 项需注意` : ""}</summary>
    {warnings.length > 0 && <ul className="round-quality-notes">{warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
    <table className="round-quality-table"><thead><tr><th scope="col">指标</th><th scope="col">本轮</th><th scope="col">上一轮</th></tr></thead>
      <tbody>{ROWS.map(([label, value]) => <tr key={label}><th scope="row">{label}</th><td>{value(current)}</td><td className="muted">{previous ? value(previous) : "—"}</td></tr>)}</tbody>
    </table>
    {stale.length > 0 && <div className="round-quality-stale"><p className="small">最近 {stale[0].rounds} 轮没有带来相关内容的检索词（论文主题会暂停一轮后再试）：</p>
      <ul>{stale.map((s) => <li key={s.source + s.query} className="small">{SOURCE[s.source] ?? s.source} · {s.query.replace(/^"|"$/g, "")} <span className="muted">已初筛 {s.screened} 条，相关 0</span></li>)}</ul></div>}
  </details>;
}

function Reanalyze({ projectId, onQueued }: { projectId: string; onQueued: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const run = async () => {
    setError(null);
    try {
      await api.reanalyze(projectId);
      toast("已用已保存材料重新排队分析");
      setConfirming(false);
      onQueued();
    } catch (e) { setError(e); }
  };
  if (!confirming) return <button className="btn btn-sm" onClick={() => setConfirming(true)}>用已保存材料重新分析</button>;
  return <div className="round-view-confirm" role="group" aria-label="确认重新分析">
    <p className="small">只使用已收录的材料，不重新检索。上次结果不明的模型请求可能会再次发送，可能重复计费。</p>
    <button className="btn btn-sm btn-primary" onClick={run}>确认重新分析</button>
    <button className="btn btn-sm btn-ghost" onClick={() => setConfirming(false)}>取消</button>
    <ErrorNote error={error} />
  </div>;
}
