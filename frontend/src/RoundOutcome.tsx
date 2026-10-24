import type { MonitoringPlanView } from "./api";
import { relTime } from "./ui";
import "./roundOutcome.css";

export function RoundOutcome({ round, stale = false }: { round: MonitoringPlanView["last_round"]; stale?: boolean }) {
  if (!round || !round.completed_at || !Number.isFinite(Date.parse(round.completed_at))) return null;
  const direction = { first: "已形成首次判断", changed: "判断方向有调整", unchanged: "判断方向未变", unavailable: "判断待核对" }[round.judgment_change] ?? "判断待核对";
  const n = (value: number) => Number.isInteger(value) && value >= 0 ? value : "待核对";
  return <section className="round-outcome" aria-label="最近已收口轮次结果">
    <header><strong>最近一轮结果</strong><span>{stale ? "状态暂不可读 · 上次记录" : relTime(round.completed_at)}
      {round.coverage === "partial" && " · 部分完成，资料仍有缺口"}</span></header>
    <div className="round-outcome-grid">
      <div><small>本轮信息</small><b>评估 {n(round.assessed_events)} 条关联信息</b>
        <p>{n(round.evidence_only)} 项仅补充研究依据 · {n(round.object_changes)} 项对象实质变化</p>
        {round.outputs && <p>本轮保留 {n(round.outputs.saved_entries)} 条记录<span className="round-outcome-caption">保存条目，不等同于新增事件</span></p>}</div>
      <div><small>判断变化</small><b>{direction}</b><p>{round.headline || "暂无可展示的判断摘要"}</p></div>
      <div><small>后续关注</small><b>{n(round.standing)} 个长期关联继续保留</b>
        <p>研究分支当前：已结束 {n(round.stopped)} · 活跃 {n(round.active)} · 等待 {n(round.waiting)}</p>
        {round.outputs && <p>修订：已执行 {n(round.outputs.revision_applied)} · 新增分支 {n(round.outputs.revision_added)}
          {(round.outputs.revision_proposed > 0 || round.outputs.revision_dropped > 0) && <span className="round-outcome-caption">待确认 {n(round.outputs.revision_proposed)} · 未执行 {n(round.outputs.revision_dropped)}</span>}</p>}
        {(round.unfulfilled > 0 || round.pending > 0) && <p className="round-outcome-warning">{n(round.unfulfilled)} 项结束修订未落实 · {n(round.pending)} 项仍待评估</p>}</div>
    </div>
    <div className="round-outcome-closeout"><strong>本轮为何结束</strong><p>{round.stop_reason || "旧回执未记录具体收口原因；不能据此认定所有分支已自然停止。"}</p>
      {!!round.stop_details?.length && <p className="round-outcome-caption">已停止分支：{round.stop_details.map(s => `${s.reason} ${n(s.count)} 个`).join(" · ")}</p>}</div>
    {!!round.gaps?.length && <div className="round-outcome-gaps">
      <p className="round-outcome-warning"><strong>仍待补齐</strong>{round.gaps[0]}</p>
      {round.gaps.length > 1 && <details><summary>其余缺口（{round.gaps.length - 1}）</summary>
        <ul>{round.gaps.slice(1).map((gap, i) => <li key={i}>{gap}</li>)}</ul></details>}
    </div>}
  </section>;
}
