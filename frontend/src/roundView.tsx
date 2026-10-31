import { useState } from "react";
import { api, type ProjectRound, type RoundStage } from "./api";
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
  const counts = (Object.keys(CATEGORY) as (keyof typeof CATEGORY)[])
    .filter((key) => (data.materials[key] ?? 0) > 0)
    .map((key) => `${CATEGORY[key]} ${data.materials[key]}`);
  const finished = data.report?.finished_at ?? data.analyze?.finished_at ?? data.collect?.finished_at;
  return <section className="round-view" aria-label="最近一次更新">
    <dl>
      <div><dt>收录</dt><dd>{collectLine(data.collect)}{counts.length > 0 && <span className="muted"> · 已收录 {counts.join(" · ")}</span>}</dd></div>
      <div><dt>分析</dt><dd>{analyzeLine(data.analyze, data.collect)}</dd></div>
      {finished && !data.running && <div><dt>完成</dt><dd className="muted">{relTime(finished)}</dd></div>}
    </dl>
    {data.can_reanalyze && <Reanalyze projectId={projectId} onQueued={() => { reload(); onReload(); }} />}
  </section>;
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
