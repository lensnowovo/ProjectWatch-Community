import { motion, useReducedMotion } from "motion/react";
import { api, type MonitoringPlanView, type ProjectDetail, type Signals } from "./api";
import { Block } from "./frame";
import { relTime, useAsync } from "./ui";

/**
 * 监控循环: the four stages this project actually runs — collect, read, judge, revise the plan — with the real
 * numbers from the last round. While work is running the current stage is lit and a pulse travels the loop;
 * when idle it reads as the last completed round and when the next one is due. Nothing here is a forecast.
 */

export type Stage = "collect" | "read" | "judge" | "revise";
const ORDER: Stage[] = ["collect", "read", "judge", "revise"];
const STAGE: Record<Stage, { no: string; label: string; what: string }> = {
  collect: { no: "01", label: "采集", what: "按方案里的来源和检索词取回公开信息与官网变化" },
  read: { no: "02", label: "解析", what: "读正文与内部资料，抽出事件、关系、数字与出处" },
  judge: { no: "03", label: "判断", what: "对照每个关注点，判断有没有新进展，并更新四圈判断" },
  revise: { no: "04", label: "修订方案", what: "把新冒出的对象与问题补进监控方案，下一轮就开始盯" },
};
/** Which stage a running job belongs to; unknown kinds count as reading. */
const JOB_STAGE: Record<string, Stage> = {
  external: "collect", external_followup: "collect", competition: "collect", commercial: "collect",
  internal: "read", details: "read", business: "read", public_profile: "read", landscape: "read",
  brief: "judge", insight: "judge",
};

export interface LoopCounts { collect: string; read: string; judge: string; revise: string }

export function MonitorLoop({ detail, plan, inline = false }: { detail: ProjectDetail; plan?: MonitoringPlanView | null; inline?: boolean }) {
  const reduced = useReducedMotion();
  const signals = useAsync<Signals | null>(() => api.signals(detail.project.id).catch(() => null), [detail.project.id, detail.project.last_external_at]);
  const running = detail.jobs.filter((j) => j.status === "running" || j.status === "queued");
  const parsing = detail.documents.filter((d) => d.status === "queued" || d.status === "processing").length;
  const activeStage: Stage | null = parsing ? "read" : running.length ? JOB_STAGE[running[0].kind] ?? "read" : null;
  const paused = detail.project.status === "paused";

  const channels = Object.values(plan?.coverage ?? {}).flat();
  const live = (plan?.items ?? []).filter((i) => i.status !== "retired");
  const collected = (["company", "competitor", "industry", "policy", "paper", "trial", "patent"] as const)
    .reduce((n, c) => n + (detail.counts[c] ?? 0), 0);
  const facts = (detail.observations?.items.length ?? 0) + (detail.event_states?.items.length ?? 0);
  const counts: LoopCounts = {
    collect: channels.length ? `${channels.filter((c) => c.state === "on" || c.state === "error").length} 个来源在跑` : "按方案取回",
    read: collected ? `已收录 ${collected.toLocaleString()} 条` : "等待首轮",
    judge: plan?.week ? `本周 ${plan.week.fresh} 个关注点有新信息` : `${live.length} 个关注点`,
    revise: plan?.state ? `已修订 ${plan.state.revisions} 轮` : "首版待生成",
  };
  const notes: Record<Stage, string> = {
    collect: channels.length ? `${detail.documents.length} 份内部资料 · 官网与公开渠道` : "先在监控方案里确认来源",
    read: `${facts} 条带出处的事实与事件状态`,
    judge: plan?.health ? `近 30 天 ${plan.health.hit_30d}/${plan.health.items} 个关注点命中过` : "等待首轮判断",
    revise: plan?.health?.system_added ? `系统累计新增 ${plan.health.system_added} 个关注点` : "暂无新增",
  };

  const cadence = signals.data?.cadence;
  const last = detail.monitoring_progress?.completed_at ?? plan?.state?.last?.at;
  const added = plan?.state?.last;
  const statusLine = paused ? "自动监控已暂停，循环不会自己往前走。"
    : activeStage ? `正在${STAGE[activeStage].label}：${parsing ? `解析 ${parsing} 份资料` : running[0]?.stage || running.map((j) => JOB_LABEL[j.kind] ?? j.kind).join("、")}`
    : last ? `上一轮完成于 ${relTime(last)}${cadence ? ` · 每 ${cadence.hours} 小时检查一次` : ""}${added && (added.added || added.noted) ? ` · 当轮新增 ${added.added} 个关注点、更新 ${added.noted} 条认识` : ""}`
    : "还没有跑完第一轮。";

  const body = <div className={`ml${inline ? " ml-inline" : ""}${activeStage ? " ml-live" : ""}`}>
    <div className="ml-track" role="list">
      {ORDER.map((key, i) => {
        const active = activeStage === key;
        return <div key={key} className={`ml-stage${active ? " on" : ""}`} role="listitem">
          <div className="ml-node">
            <span className="ml-no">{STAGE[key].no}</span>
            {active && !reduced && <motion.i className="ml-halo" aria-hidden="true" animate={{ opacity: [.25, .6, .25], scale: [1, 1.12, 1] }}
              transition={{ duration: 2.2, repeat: Infinity, ease: "easeInOut" }} />}
          </div>
          <div className="ml-copy">
            <h4>{STAGE[key].label}{active && <em>进行中</em>}</h4>
            <b>{counts[key]}</b>
            <p>{STAGE[key].what}</p>
            <small>{notes[key]}</small>
          </div>
          {i < ORDER.length - 1 && <span className="ml-link" aria-hidden="true">
            {!reduced && <motion.i animate={{ x: ["-60%", "160%"], opacity: [0, 1, 0] }}
              transition={{ duration: 2.4, repeat: Infinity, ease: "easeInOut", delay: i * .6 }} />}
          </span>}
        </div>;
      })}
      <span className="ml-return" aria-hidden="true">
        <span className="ml-return-label">新对象与新问题回到采集</span>
{!reduced && <motion.i animate={{ left: ["calc(100% - 6px)", "0%"], opacity: [0, 1, 1, 0] }}
          transition={{ duration: 3.4, repeat: Infinity, ease: "easeInOut", delay: 1.2, times: [0, .1, .9, 1] }} />}
      </span>
    </div>
    <p className={`ml-status${activeStage ? " live" : ""}${paused ? " paused" : ""}`}>
      {activeStage && !reduced && <motion.i className="ml-dot" animate={{ opacity: [1, .25, 1] }} transition={{ duration: 1.4, repeat: Infinity }} />}
      {statusLine}
    </p>
  </div>;

  if (inline) return body;
  return <Block card title="监控循环" note="这个项目实际在跑的四步；数字来自已收录与已判断的记录">{body}</Block>;
}

const JOB_LABEL: Record<string, string> = { internal: "分析内部资料", external: "整理首轮信息", external_followup: "补全外部信息",
  brief: "生成简报", public_profile: "补充公开档案", landscape: "整理竞争与行业", business: "整理产品与用途",
  competition: "更新竞对资料", details: "整理商业关系与政策条件", commercial: "更新专业来源", insight: "深度分析" };
