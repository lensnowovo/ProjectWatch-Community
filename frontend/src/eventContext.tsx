import { Link } from "react-router-dom";
import { api, type EventContext } from "./api";
import { Skeleton } from "./frame";
import { CATEGORY_LABEL, Drawer, ErrorNote, dayLabel, useAsync } from "./ui";

/**
 * Why an event matters, in reading order: what the source says → which monitoring point it hit and why that point
 * exists → which question or AI judgment it moved. Opens beside the list; nothing is fetched until it is opened.
 */
export function EventContextDrawer({ projectId, projectName, eventId, title, open, onClose }: {
  projectId: string; projectName: string; eventId: string; title: string; open: boolean; onClose: () => void;
}) {
  return <Drawer open={open} onClose={onClose} eyebrow="关联与依据" title={title}>
    {open && <EventContextBody projectId={projectId} projectName={projectName} eventId={eventId} />}
  </Drawer>;
}

function EventContextBody({ projectId, projectName, eventId }: { projectId: string; projectName: string; eventId: string }) {
  const { data, error, loading } = useAsync<EventContext>(() => api.eventContext(projectId, eventId), [projectId, eventId]);
  if (loading && !data) return <Skeleton lines={6} />;
  if (!data) return <ErrorNote error={error} />;
  const { event, excerpts, hits, assessments, judgments } = data;
  const linked = assessments.length > 0 || judgments.length > 0;
  return <div className="ec">
    <div className="ec-meta"><span className={`cat cat-${event.category}`}>{CATEGORY_LABEL[event.category] ?? event.category}</span>
      {(event.event_at || event.created_at) && <span>{dayLabel(event.event_at ?? event.created_at)}</span>}</div>
    {event.summary && event.summary !== event.title && <p className="ec-lead">{event.summary}</p>}

    <section className="ec-step" aria-label="来源节选">
      <h4><span className="ec-no">1</span>来源节选</h4>
      {excerpts.length ? excerpts.slice(0, 3).map((x, i) => <blockquote key={i} className="ec-quote">
        {x.text || x.title}
        <footer>{x.url ? <a href={x.url} target="_blank" rel="noreferrer noopener">{x.source || "原文"} ↗</a> : x.source}{x.date && ` · ${x.date}`}</footer>
      </blockquote>) : <p className="muted small">没有保存原文摘录。</p>}
      {excerpts.length > 3 && <p className="muted small">另有 {excerpts.length - 3} 篇报道。</p>}
    </section>

    <section className="ec-step" aria-label="关联依据">
      <h4><span className="ec-no">2</span>为什么在监控范围内</h4>
      {hits.length ? hits.map((h) => <div key={h.id} className="ec-hit">
        <div className="ec-chain">
          <span className="ec-node">{h.objects.length ? h.objects.slice(0, 2).join("、") : "公司自身"}</span>
          <span className="ec-arrow" aria-hidden="true">→</span>
          <span className="ec-node ec-rel">{h.section_label}{h.ring_label && ` · ${h.ring_label}`}</span>
          <span className="ec-arrow" aria-hidden="true">→</span>
          <span className="ec-node">{projectName}</span>
        </div>
        <p className="ec-question">{h.text}</p>
        {h.reason && <p className="ec-reason">因为 {h.reason.replace(/^因为[：:，,\s]*/, "")}</p>}
        {h.parent && <p className="muted small">由「{h.parent}」延伸</p>}
      </div>) : <p className="muted small">没有命中具体关注点：它来自常规采集（公司或赛道新闻）。</p>}
      {hits.length > 0 && <Link className="link small" to={`/projects/${projectId}?tab=plan`}>在监控范围查看 →</Link>}
    </section>

    <section className="ec-step" aria-label="关联判断">
      <h4><span className="ec-no">3</span>它改变了什么判断</h4>
      {assessments.map((a, i) => <div key={`a${i}`} className="ec-judge">
        <span className="ec-tag">{a.status_label}</span>
        <div><p className="ec-question">{a.question}</p><p className="ec-note">{a.note}</p></div>
      </div>)}
      {judgments.map((j, i) => <div key={`j${i}`} className="ec-judge">
        <span className={`ec-tag ${j.sign === "+" ? "plus" : "minus"}`}>{j.sign === "+" ? "支持" : "反对"}</span>
        <div><p className="ec-question">{j.label}{j.verdict_label && <b> · {j.verdict_label}</b>}</p><p className="ec-note">{j.text}</p></div>
      </div>)}
      {!linked && <p className="muted small">还没有问题判断或 AI 判断引用这条动态。</p>}
      {judgments.length > 0 && <Link className="link small" to={`/projects/${projectId}`}>在当前判断查看 →</Link>}
    </section>
  </div>;
}
