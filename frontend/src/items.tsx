import { AnimatePresence, motion } from "motion/react";
import { EventContextDrawer } from "./eventContext";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Item } from "./api";
import { EASE } from "./motion";
import { FeedbackMenu } from "./monitor";
import { CATEGORY_LABEL, IMPORTANCE_LABEL, ImportanceDot, dayLabel, seenAt, relTime } from "./ui";

export function ItemRow({ item, showProject, onChange, index = 0, watch, also = [] }: {
  item: Item;
  /** The same report attached to other projects; shown once with every project named. */
  also?: Item[];
  watch?: string[];
  showProject?: boolean;
  onChange?: (item: Item) => void;
  index?: number;
}) {
  const set = (status: Item["status"]) => {
    api.setItem(item.id, status).then(() => onChange?.({ ...item, status }));
  };
  const markRead = () => {
    if (item.status === "new") set("read");
    for (const other of also) if (other.status === "new") api.setItem(other.id, "read").then(() => onChange?.({ ...other, status: "read" }));
  };
  const when = item.published_at ?? item.collected_at;
  const [open, setOpen] = useState(false);
  const [why, setWhy] = useState(false);
  const others = (item.source_count ?? 1) - 1;
  return (
    <motion.article
      layout="position"
      className={`item item-${item.importance} ${item.status === "new" ? "item-new" : ""}`}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0, transition: { duration: 0.4, ease: EASE, delay: Math.min(index, 12) * 0.03 } }}
      exit={{ opacity: 0, height: 0, paddingTop: 0, paddingBottom: 0, marginBottom: 0, transition: { duration: 0.28, ease: EASE } }}
      onMouseMove={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        e.currentTarget.style.setProperty("--mx", `${e.clientX - r.left}px`);
        e.currentTarget.style.setProperty("--my", `${e.clientY - r.top}px`);
      }}
    >
      <div className="item-rail"><ImportanceDot level={item.importance} /></div>
      <div className="item-body">
        <div className="item-meta">
          {showProject && (
            <Link to={`/projects/${item.project_id}`} className="item-project">{item.project_name}</Link>
          )}
          {showProject && also.map((o) => (
            <Link key={o.id} to={`/projects/${o.project_id}`} className="item-project item-project-also">{o.project_name}</Link>
          ))}
          <span className={`cat cat-${item.category}`}>{CATEGORY_LABEL[item.category] ?? item.category}</span>
          {item.importance !== "routine" && (
            <span className={`imp imp-${item.importance}`}>{IMPORTANCE_LABEL[item.importance]}</span>
          )}
          {watch?.map((w) => <span key={w} className="watch-tag" title="命中你的关注点">◎ {w}</span>)}
          <span className="muted">{item.source}</span>
          {!!(item.extra.agent_result as { producer?: string } | undefined)?.producer && <span className="muted">· {(item.extra.agent_result as { producer: string }).producer} 整理 · {item.extra.read_scope === "summary" ? "搜索摘录" : "已读材料"}</span>}
          {others > 0 && (
            <button className="sources-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
              共 {item.source_count} 篇报道 {open ? "▴" : "▾"}
            </button>
          )}
          <span className="muted">· {relTime(when)}</span>
        </div>
        <a className="item-title" href={item.url} target="_blank" rel="noreferrer noopener"
          onClick={markRead}>
          {item.title}
        </a>
        {item.summary && item.summary !== item.title && <p className="item-summary">{item.summary}</p>}
        <button className="item-why" onClick={() => { setWhy(true); markRead(); }}>关联与依据 →</button>
        <EventContextDrawer projectId={item.project_id} projectName={item.project_name} eventId={item.id} title={item.title}
          open={why} onClose={() => setWhy(false)} />
        {item.impact && <p className="item-impact"><span>{also.length ? `对${item.project_name}的意义` : "对项目的意义"}</span>{item.impact}</p>}
        {also.filter((o) => o.impact && o.impact !== item.impact).map((o) => (
          <p key={o.id} className="item-impact"><span>对{o.project_name}的意义</span>{o.impact}</p>
        ))}
        <AnimatePresence initial={false}>
          {open && item.sources && (
            <motion.ul className="item-sources" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.25, ease: EASE }}>
              {item.sources.map((s) => (
                <li key={s.id}>
                  <a href={s.url} target="_blank" rel="noreferrer noopener">{s.title}</a>
                  <span className="muted small">{s.source} · {relTime(s.published_at ?? s.collected_at)}
                    {s.body_status === "article_body" || s.body_status === "partial_body" ? " · 已读正文" : ""}</span>
                </li>
              ))}
            </motion.ul>
          )}
        </AnimatePresence>
      </div>
      <div className="item-actions">
        <motion.button whileTap={{ scale: 0.85 }} className={`icon-btn ${item.status === "starred" ? "on" : ""}`}
          title="标记重要" onClick={() => set(item.status === "starred" ? "read" : "starred")}>★</motion.button>
        <FeedbackMenu item={item} onChange={onChange} />
      </div>
    </motion.article>
  );
}

export function ItemList({ items, showProject, onChange, grouped = true, watchLabels, foldRoutine = false }: {
  items: Item[];
  watchLabels?: Record<string, string[]>;
  showProject?: boolean;
  onChange?: (item: Item) => void;
  grouped?: boolean;
  /** Per day, keep major/notable/starred/watch-hit items visible and fold routine ones behind a toggle. */
  foldRoutine?: boolean;
}) {
  const [unfolded, setUnfolded] = useState<Set<string>>(new Set());
  if (!grouped) {
    return (
      <div className="items items-flat">
        <AnimatePresence>
          {items.map((i, n) => <ItemRow key={i.id} index={n} item={i} showProject={showProject} onChange={onChange} watch={watchLabels?.[i.id]} />)}
        </AnimatePresence>
      </div>
    );
  }
  const groups: [string, Item[]][] = [];
  for (const item of items) {
    const label = dayLabel(seenAt(item));
    const last = groups[groups.length - 1];
    if (last && last[0] === label) last[1].push(item);
    else groups.push([label, [item]]);
  }
  const keep = (i: Item) => i.importance !== "routine" || i.status === "starred" || !!watchLabels?.[i.id];
  let n = 0;
  // Across projects, one report linked to several companies is shown once.
  const merge = (list: Item[]): { item: Item; also: Item[] }[] => {
    if (!showProject) return list.map((item) => ({ item, also: [] }));
    const out: { item: Item; also: Item[] }[] = [];
    const byKey = new Map<string, { item: Item; also: Item[] }>();
    for (const item of list) {
      const key = `${item.category}|${item.title.replace(/\s+/g, "").toLocaleLowerCase()}`;
      const found = byKey.get(key);
      if (found && found.item.project_id !== item.project_id && !found.also.some((o) => o.project_id === item.project_id)) found.also.push(item);
      else if (!found) { const row = { item, also: [] as Item[] }; byKey.set(key, row); out.push(row); }
      else out.push({ item, also: [] });
    }
    return out;
  };
  return (
    <div className="items">
      {groups.map(([label, list]) => {
        const open = !foldRoutine || unfolded.has(label);
        // A day with only routine items still shows its first two, so the stream never looks empty.
        const important = list.filter(keep);
        const visible = open ? list : important.length ? important : list.slice(0, 2);
        const hidden = merge(list).length - merge(visible).length;
        return (
          <section key={label} className="day">
            <h3 className="day-label"><span>{label}</span>{foldRoutine && <span className="day-count">{merge(list).length} 条</span>}</h3>
            <div className="day-items">
              <AnimatePresence>
                {merge(visible).map(({ item: i, also }) => <ItemRow key={i.id} index={n++} item={i} also={also} showProject={showProject} onChange={onChange} watch={watchLabels?.[i.id]} />)}
              </AnimatePresence>
              {foldRoutine && (hidden > 0 || (unfolded.has(label) && list.some((i) => !keep(i)))) && (
                <button className="day-fold" onClick={() => setUnfolded((prev) => {
                  const next = new Set(prev);
                  if (next.has(label)) next.delete(label); else next.add(label);
                  return next;
                })}>
                  {open ? "收起一般动态 ▴" : `另有 ${hidden} 条一般动态 ▾`}
                </button>
              )}
            </div>
          </section>
        );
      })}
    </div>
  );
}
