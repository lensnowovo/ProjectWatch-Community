import { useState } from "react";
import { Link } from "react-router-dom";
import type { Attention, AttentionKind, AttentionProject } from "./api";
import { Reveal } from "./motion";
import { relTime } from "./ui";

export const KIND_SHORT: Record<AttentionKind, string> = { crosscheck: "核对", silence: "沉默", watch: "命中", site: "官网" };
// Things a person acts on. A watch hit (news matching a plan question) is information, not a to-do:
// it is counted on 监控方案, never in “需要你看一眼” or a portfolio's 待处理.
const KIND_ORDER: AttentionKind[] = ["crosscheck", "silence", "site"];

/** Small count chips: 核对 2 · 沉默 1 … (only non-zero kinds). */
export function SignalChips({ counts, empty = "—" }: { counts?: Partial<Record<AttentionKind, number>>; empty?: string }) {
  const shown = KIND_ORDER.filter((k) => (counts?.[k] ?? 0) > 0);
  if (!shown.length) return <span className="muted">{empty}</span>;
  return (
    <span className="sig-chips">
      {shown.map((k) => <span key={k} className={`sig-chip sk-${k}`}>{KIND_SHORT[k]} <b className="num">{counts?.[k]}</b></span>)}
    </span>
  );
}

function ProjectRow({ p, limit }: { p: AttentionProject; limit: number }) {
  const [open, setOpen] = useState(false);
  const entries = open ? p.entries : p.entries.slice(0, limit);
  return (
    <li className="att-project">
      <div className="att-head">
        <Link to={`/projects/${p.id}`} className="att-name">{p.name}</Link>
        <SignalChips counts={p.counts} />
      </div>
      <ul className="att-entries">
        {entries.map((e, i) => (
          <li key={i} className={`att-entry al-${e.level}`}>
            <span className={`att-kind sk-${e.kind}`}>{KIND_SHORT[e.kind]}</span>
            <div className="att-text">
              <Link to={`/projects/${p.id}${e.tab === "overview" ? "" : `?tab=${e.tab}`}`} className="att-title">{e.title}</Link>
              <span className="att-detail">{e.detail}</span>
              {e.question && <span className="att-q">可以问：{e.question}</span>}
            </div>
            {e.at && <span className="muted small att-when">{relTime(e.at)}</span>}
          </li>
        ))}
      </ul>
      {p.entries.length > limit && (
        <button className="link small" onClick={() => setOpen((v) => !v)}>{open ? "收起" : `另有 ${p.entries.length - limit} 条`}</button>
      )}
    </li>
  );
}

/** 需要你看一眼: every project with open cross-checks, silence, watch hits or notable site changes. */
export function AttentionBoard({ data, title = "需要你看一眼", maxProjects = 5, perProject = 2, note }: {
  data: Attention | null | undefined; title?: string; maxProjects?: number; perProject?: number; note?: string;
}) {
  const [all, setAll] = useState(false);
  if (!data) return null;
  const actionable = data.projects.map((p) => ({ ...p, entries: p.entries.filter((e) => KIND_ORDER.includes(e.kind)) }))
    .filter((p) => p.entries.length);
  const hits = data.totals.watch ?? 0;
  const projects = all ? actionable : actionable.slice(0, maxProjects);
  return (
    <Reveal as="section" className="card card-pad attention-board">
      <div className="card-head">
        <h3>{title}</h3>
        <span className="muted small">{note ?? `近 ${data.days} 天 · 规则汇总`}</span>
      </div>
      {actionable.length === 0 ? (
        <p className="muted small att-empty">暂无待核对事项或异常信号。</p>
      ) : (
        <>
          <div className="att-totals">
            {KIND_ORDER.map((k) => (
              <div key={k} className={`att-total ${data.totals[k] ? "" : "zero"}`}>
                <b className="num">{data.totals[k]}</b><span>{data.labels[k]}</span>
              </div>
            ))}
          </div>
          <ul className="att-list">{projects.map((p) => <ProjectRow key={p.id} p={p} limit={perProject} />)}</ul>
          {actionable.length > maxProjects && (
            <button className="link small" onClick={() => setAll((v) => !v)}>{all ? "收起" : `查看全部 ${actionable.length} 个项目`}</button>
          )}
        </>
      )}
      {hits > 0 && <p className="muted small att-hits">另有 {hits} 条新信息与关注点相关，已对照问题判断，在各项目的“监控方案”里查看。</p>}
    </Reveal>
  );
}
