import { api, type Category, type Item, type TrackCell } from "./api";
import { Skeleton } from "./frame";
import { ItemList } from "./items";
import { Grade } from "./race";
import { Drawer, Empty, byNewest, useAsync } from "./ui";

/**
 * The records behind one sector signal: first what the number was read from, then every collected item of that
 * kind. Counts describe what was collected, never the whole market.
 */
const FEED: Record<TrackCell["key"], { category: Category; what: string }> = {
  demand: { category: "industry", what: "赛道与需求报道" },
  capital: { category: "industry", what: "赛道报道（含融资）" },
  price: { category: "industry", what: "赛道与价格报道" },
  policy: { category: "policy", what: "政策与监管" },
};

export function SignalListDrawer({ projectId, cell, open, onClose }: {
  projectId: string; cell: TrackCell | null; open: boolean; onClose: () => void;
}) {
  return <Drawer open={open} onClose={onClose} eyebrow={cell ? `赛道信号 · ${cell.label}` : "赛道信号"}
    title={cell ? `${cell.label}：${cell.value || "暂无数字"}` : ""} subtitle={cell?.direction}>
    {open && cell && <SignalListBody projectId={projectId} cell={cell} />}
  </Drawer>;
}

function SignalListBody({ projectId, cell }: { projectId: string; cell: TrackCell }) {
  const feed = FEED[cell.key];
  const { data, loading, setData } = useAsync(() => api.feed({ project_id: projectId, category: feed.category, limit: 200 }),
    [projectId, feed.category]);
  const items = (data ?? []).slice().sort(byNewest);
  const update = (changed: Item) => setData((prev) => (prev ?? []).map((i) => (i.id === changed.id ? changed : i)).filter((i) => i.status !== "dismissed"));
  return <div className="sl">
    {cell.for_us && <p className="sl-forus">对公司：{cell.for_us}</p>}
    <section className="sl-step">
      <h4>这个数字读自</h4>
      {cell.evidence.length ? <ul className="sl-ev">{cell.evidence.map((e, i) => <li key={i}>
        {e.url ? <a href={e.url} target="_blank" rel="noreferrer noopener">{e.title}</a> : e.title}
        <span className="muted small">{e.date}{e.source && ` · ${e.source}`}</span> <Grade grade={e.grade} />
      </li>)}</ul> : <p className="muted small">这一项还没有可引用的记录，数字为空或仅来自季度计数。</p>}
    </section>
    <section className="sl-step">
      <h4>{feed.what}{items.length > 0 && <span className="muted small"> · 已收录 {items.length} 条</span>}</h4>
      {loading && !data ? <Skeleton lines={5} />
        : items.length ? <ItemList items={items} onChange={update} grouped={false} />
        : <Empty title={`暂无${feed.what}`}>公开信息覆盖有限，不代表没有发生。</Empty>}
    </section>
  </div>;
}
