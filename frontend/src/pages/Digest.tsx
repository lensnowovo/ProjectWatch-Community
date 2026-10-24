import { Skeleton } from "../frame";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type DigestProject } from "../api";
import { Page, Reveal, useToast } from "../motion";
import { CATEGORY_LABEL, Empty, ErrorNote, ImportanceDot, Spinner, TONE_LABEL, dayLabel, relTime, useAsync } from "../ui";
import { Kpi, Seg } from "../widgets";
import { AttentionBoard } from "../attention";
import PageHeader from "../PageHeader";
import { DigestProgress } from "../MonitoringProgress";
import { useStagedRead } from "../Operational";

const RANGES = [
  { value: 7, label: "近 7 天" },
  { value: 14, label: "近 14 天" },
  { value: 30, label: "近 30 天" },
];

export default function DigestPage() {
  const [days, setDays] = useState(7);
  const { data, error, loading, reload, setData } = useAsync(() => api.digest(days), [days]);
  const staged = useStagedRead(String(days), () => api.digest(days), next => next.markdown !== data?.markdown, !!data);
  const [pushing, setPushing] = useState(false);
  const toast = useToast();

  const copy = async () => {
    if (!data) return;
    try {
      await navigator.clipboard.writeText(data.markdown);
      toast("已复制 Markdown");
    } catch {
      toast("复制失败，请使用下载");
    }
  };
  const push = async () => {
    setPushing(true);
    try {
      const r = await api.pushDigest(days);
      toast(r.message);
      reload();
    } finally {
      setPushing(false);
    }
  };

  return (
    <Page className="page-workspace">
      <PageHeader variant="digest" title="周报" label="组合回顾"
        description="本期重要变化，一份周报。"
        meta={`近 ${days} 天 · 已收录信息`} />
      <div className="filters digest-tools">
        <Seg id="digest-days" value={days} onChange={setDays} options={RANGES} />
        <div className="actions">
          <button className="btn" disabled={!data} onClick={copy}>复制 Markdown</button>
          <a className="btn" href={`/api/studio/digest.md?days=${days}`} download>下载 .md</a>
          {data?.notify.ready
            ? <button className="btn btn-primary" disabled={pushing} onClick={push}>{pushing ? <Spinner label="推送中…" /> : "推送到群"}</button>
            : <Link className="btn btn-ghost" to="/settings">设置群推送</Link>}
        </div>
      </div>
      {data?.notify.last && (
        <p className={`small ${data.notify.last.error ? "text-warn" : "muted"}`}>
          上次自动推送 {relTime(data.notify.last.at)}{data.notify.last.error ? `：失败（${data.notify.last.error}），30 分钟后重试` : ""}
        </p>
      )}
      <ErrorNote error={error} />
      {staged.failed && <p className="muted small">暂时无法检查更新，当前周报保留。</p>}
      {staged.pending && <button className="link op-new" onClick={() => { setData(staged.pending); staged.clear(); }}>周报有更新 · 点击查看</button>}
      {!data && loading && <Skeleton lines={6} />}
      {data && (
        <>
          <div className="kpis">
            <Kpi label="有变化的项目" value={data.totals.projects} />
            <Kpi label="重大事件" value={data.totals.major} tone={data.totals.major ? "major" : undefined} />
            <Kpi label="值得关注" value={data.totals.notable} />
            <Kpi label="新资料" value={data.totals.doc_changes} />
          </div>
          {(data.attention?.projects.length ?? 0) > 0 && (
            <AttentionBoard data={data.attention} title="需要核对与异常信号" maxProjects={20} perProject={4}
              note="群推送仅列前 5 个项目" />
          )}
          {data.projects.length === 0 ? (
            <Empty title="暂未整理出重要变化">{data.incomplete?.length ? "监控进度见下方。" : "一般动态可在“今日动态”查看。"}</Empty>
          ) : (
            <div className="digest">
              {data.projects.map((p, i) => <DigestCard key={p.id} p={p} delay={i * 0.04} />)}
            </div>
          )}
          <DigestProgress incomplete={data.incomplete} />
          {data.quiet.length > 0 && <p className="muted small">已收录信息未见重要变化：{data.quiet.join("、")}</p>}
        </>
      )}
    </Page>
  );
}

function DigestCard({ p, delay }: { p: DigestProject; delay: number }) {
  return (
    <Reveal as="section" className="card card-pad digest-card" delay={delay}>
      <div className="card-head">
        <h3><Link to={`/projects/${p.id}`}>{p.name}</Link></h3>
        {p.tone && p.tone in TONE_LABEL && <span className={`tone tone-${p.tone}`}>{TONE_LABEL[p.tone as keyof typeof TONE_LABEL]}</span>}
      </div>
      {p.headline && <p className="digest-headline">{p.headline}</p>}
      {p.monitoring_progress && p.monitoring_progress.state !== "observed" &&
        <p className="muted small">{p.monitoring_progress.label} · 以下为已有结果</p>}
      {p.events.length > 0 && (
        <ul className="digest-events">
          {p.events.map((e, i) => (
            <li key={i}>
              <ImportanceDot level={e.importance} />
              <div>
                <div>
                  <span className="muted small">{CATEGORY_LABEL[e.category] ?? e.category} · {dayLabel(e.when)}</span>{" "}
                  {e.url ? <a href={e.url} target="_blank" rel="noreferrer noopener">{e.title}</a> : e.title}
                  {e.source_count > 1 && <span className="muted small">（{e.source_count} 篇报道）</span>}
                </div>
                {e.impact && <div className="muted small">{e.impact}</div>}
              </div>
            </li>
          ))}
        </ul>
      )}
      {p.more_events > 0 && <p className="muted small">另有 {p.more_events} 条值得关注的动态</p>}
      {p.doc_changes.map((c, i) => (
        <div key={i} className="digest-docs">
          <h4>新资料：{c.documents.join("、")}</h4>
          <ul className="bullets">{c.items.map((t, j) => <li key={j}>{t}</li>)}</ul>
        </div>
      ))}
      {p.issues.length > 0 && (
        <ul className="bullets bullets-risk">{p.issues.map((t, i) => <li key={i}>需核对：{t}</li>)}</ul>
      )}
    </Reveal>
  );
}
