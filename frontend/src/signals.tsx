import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import { api, type CrossCheck, type Signals } from "./api";
import { EASE, Reveal, useToast } from "./motion";
import { RefChips } from "./ui";

const SEVERITY: Record<CrossCheck["severity"], string> = { high: "高", medium: "中", low: "低", unrated: "" };

function CrossCheckRow({ check, onDone, initiallyOpen }: { check: CrossCheck; onDone: (id: string) => void; initiallyOpen: boolean }) {
  const toast = useToast();
  const [open, setOpen] = useState(initiallyOpen);
  const close = async (status: "resolved" | "dismissed") => {
    await api.setCrossCheck(check.id, status);
    toast(status === "resolved" ? "已标记为处理完成，不再提示" : "已忽略，下次不再提示", "info");
    onDone(check.id);
  };
  return (
    <motion.li layout className={`xc xc-${check.severity}`} exit={{ opacity: 0, height: 0, transition: { duration: 0.24, ease: EASE } }}>
      <button className="xc-head" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className={`xc-kind xk-${check.kind}`}>{check.kind_label}</span>
        <strong>{check.topic}</strong>
        {!open && <span className="xc-peek">{check.public_says}</span>}
        {SEVERITY[check.severity] && <span className="xc-sev" title="重要程度">{SEVERITY[check.severity]}</span>}
        <span className="xc-caret" aria-hidden>{open ? "▴" : "▾"}</span>
      </button>
      <AnimatePresence initial={false}>
      {open && <motion.div className="xc-body" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
        exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22, ease: EASE }}>
      <div className="xc-sides">
        <div className="xc-side">
          <span className="xc-label">公开信息</span>
          <p>{check.public_says}</p>
          <div className="xc-refs">
            {check.public_refs.map((r) => (
              <a key={r.id} className="ref" href={r.url} target="_blank" rel="noreferrer noopener" title={r.title}>{r.source || "报道"} · {r.date.slice(5)} ↗</a>
            ))}
          </div>
        </div>
        <div className="xc-vs" aria-hidden>↔</div>
        <div className="xc-side">
          <span className="xc-label">内部资料</span>
          <p className={check.kind === "undisclosed" ? "muted" : ""}>{check.internal_says}</p>
          <div className="xc-refs">{check.internal_refs.map((r, i) => <RefChips key={i} refs={r.refs} />)}</div>
        </div>
      </div>
      <div className="xc-foot">
        {check.question && <span className="xc-q"><span>可以问</span>{check.question}</span>}
        <span className="spacer" />
        <button className="btn btn-sm" onClick={() => close("resolved")}>已处理</button>
        <button className="btn btn-sm btn-ghost" onClick={() => close("dismissed")}>不是问题</button>
      </div>
      </motion.div>}
      </AnimatePresence>
    </motion.li>
  );
}

/** 需要核对: public vs internal conflicts and silence signals, above the overview grid. */
export function SignalsPanel({ data: raw, onChange, hideSilence = false }: {
  data: Signals | null; onChange: (next: Signals) => void;
  /** Silence signals shown elsewhere (the 坏信号 panel lists them as "该有却没有"). */
  hideSilence?: boolean;
}) {
  const data = raw && hideSilence ? { ...raw, silence: [] } : raw;
  if (!data || (data.crosschecks.length === 0 && data.silence.length === 0)) return null;
  const done = (id: string) => onChange({ ...raw!, crosschecks: data.crosschecks.filter((c) => c.id !== id) });
  return (
    <Reveal as="section" className="card card-pad signals-panel">
      <div className="card-head">
        <h3>需要核对</h3>
      </div>
      <div className={`signals-grid ${data.crosschecks.length && data.silence.length ? "two" : ""}`}>
        {data.crosschecks.length > 0 && (
          <div>
            <h4>内外对照 <span className="count">{data.crosschecks.length}</span></h4>
            <ul className="xc-list"><AnimatePresence initial={false}>
              {data.crosschecks.map((c, i) => <CrossCheckRow key={c.id} check={c} onDone={done} initiallyOpen={i === 0} />)}
            </AnimatePresence></ul>
          </div>
        )}
        {data.silence.length > 0 && (
          <div>
            <h4>沉默信号 <span className="count">{data.silence.length}</span></h4>
            <ul className="quiet-list">
              {data.silence.map((s, i) => (
                <li key={i} className={`quiet quiet-${s.level}`}>
                  <span className="quiet-mark" aria-hidden />
                  <div><strong>{s.title}</strong><p>{s.detail}</p></div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
      <p className="muted small">{data.crosschecks.some(c => c.question)
        ? (data.silence.length ? "AI 对照仅作追问线索；沉默信号按规则识别。" : "AI 对照仅作追问线索。")
        : (data.silence.length ? "对照保留双方来源；沉默信号按规则识别。" : "对照保留双方来源。")}</p>
    </Reveal>
  );
}

const MODE_LABEL = { active: "活跃期", normal: "常规", quiet: "平静期" } as const;

export function CadenceCard({ data }: { data: Signals | null }) {
  if (!data) return null;
  const { cadence, latency } = data;
  const lat = latency.median_hours;
  return (
    <section className="card card-pad cadence">
      <div className="card-head"><h3>监控节奏</h3><span className={`cadence-mode cm-${cadence.mode}`}>{MODE_LABEL[cadence.mode]}</span></div>
      <dl className="plan">
        <dt>检查频率</dt><dd>每 {cadence.hours} 小时<span className="muted small"> · {cadence.reason}</span></dd>
        <dt>发现延迟</dt><dd>{lat === null ? <span className="muted">样本不足（{latency.n} 条）</span>
          : <>中位 {lat < 24 ? `${lat} 小时` : `${(lat / 24).toFixed(1)} 天`}<span className="muted small"> · 近 30 天 {latency.n} 条有发布时间的公司新闻</span></>}</dd>
      </dl>
    </section>
  );
}
