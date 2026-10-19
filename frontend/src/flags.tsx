import { api, type MonitoringProgress, type RedFlag, type RedFlagsView } from "./api";
import { showStoredPanel } from "./MonitoringProgress";
import { Reveal } from "./motion";
import { Grade } from "./race";
import { relTime, useAsync } from "./ui";

const LEVEL_CLASS: Record<RedFlag["level"], string> = { 高: "high", 中: "mid", 低: "low" };

function FlagRow({ f }: { f: RedFlag }) {
  return (
    <li className="flag">
      <span className={`flag-level flag-${LEVEL_CLASS[f.level]}`}>{f.level}</span>
      <div>
        <div>{!f.is_self && !f.title.includes(f.who) && <strong>{f.who} · </strong>}
          {f.url ? <a href={f.url} target="_blank" rel="noreferrer noopener">{f.title}</a> : f.title}</div>
        <div className="flag-meta muted small">
          {f.date && <span>{f.date}</span>}
          {f.grade && <Grade grade={f.grade} />}
          {f.source && <span>{f.source}</span>}
        </div>
        {f.note && <div className="flag-note small">{f.note}</div>}
      </div>
    </li>
  );
}

/** 坏信号: negative public events for the company and its competitors, rule-identified, each with its source. */
export function RedFlags({ projectId, revision, progress }: { projectId: string; revision: string; progress?: MonitoringProgress }) {
  const { data } = useAsync<RedFlagsView>(() => api.redFlags(projectId), [projectId, revision]);
  if (!data) return null;
  const none = !data.company.length && !data.peers.length;
  // Nothing found is not shown as a placeholder: the card appears only when there is a signal.
  if (none || !showStoredPanel(progress, data.company.length + data.peers.length)) return null;
  return (
    <Reveal as="section" className="card card-pad flags-panel">
      <div className="card-head">
        <h3>坏信号</h3>
        <span className="muted small">近 {data.window_days} 天 · 规则识别 · {relTime(data.generated_at)}</span>
      </div>
      <div className={`flags-grid ${data.company.length && data.peers.length ? "" : "single"}`}>
        {data.company.length > 0 && <div>
          <h4>本公司 <span className="count">{data.counts.company}</span></h4>
          <ul className="flag-list">{data.company.map((f, i) => <FlagRow key={i} f={f} />)}</ul>
        </div>}
        {data.peers.length > 0 && <div>
          <h4>竞对 <span className="count">{data.counts.peers}</span></h4>
          <ul className="flag-list">{data.peers.map((f, i) => <FlagRow key={i} f={f} />)}</ul>
        </div>}
      </div>
    </Reveal>
  );
}
