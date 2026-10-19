import { Columns, Skeleton } from "../frame";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Category, type Item, type Project } from "../api";
import { AttentionBoard } from "../attention";
import { ItemList } from "../items";
import { Page, useToast } from "../motion";
import { CATEGORY_LABEL, Empty, ErrorNote, byNewest, dayLabel, seenAt, useAsync } from "../ui";
import { Kpi, Seg } from "../widgets";
import PageHeader from "../PageHeader";

const RANGES = [
  { days: 7, label: "7 天" },
  { days: 30, label: "30 天" },
  { days: 90, label: "90 天" },
];

export default function FeedPage({ projects }: { projects: Project[] }) {
  const [days, setDays] = useState(7);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [older, setOlder] = useState(false);
  const toast = useToast();
  const [level, setLevel] = useState<"" | "major" | "notable">("");
  const [category, setCategory] = useState<"" | Category>("");
  const [projectId, setProjectId] = useState("");
  const { data, error, loading, setData } = useAsync(
    () => api.feed({ days, importance: level || undefined, category: category || undefined,
      project_id: projectId || undefined, limit: 150 }),
    [days, level, category, projectId],
  );

  const attention = useAsync(() => (projects.length ? api.attention(7) : Promise.resolve(null)), [projects.length]);
  const update = (changed: Item) =>
    setData((items) => (items ?? []).map((i) => (i.id === changed.id ? changed : i))
      .filter((i) => i.status !== "dismissed"));

  if (!projects.length) {
    return (
      <Page className="page-workspace">
        <PageHeader variant="feed" title="今日动态" label="动态汇总"
          description="公司、竞品、行业与政策的新进展。" />
        <Empty title="还没有项目" action={<Link to="/new" className="btn btn-primary">添加第一个项目</Link>} />
      </Page>
    );
  }

  const all = data ?? [];
  const items = (unreadOnly ? all.filter((i) => i.status === "new") : all).slice().sort(byNewest);
  // The last three days with news stay open; older days wait behind one button.
  const dayList = [...new Set(items.map((i) => dayLabel(seenAt(i))))];
  const recent = items.filter((i) => dayList.slice(0, 3).includes(dayLabel(seenAt(i))));
  const unreadHere = all.filter((i) => i.status === "new").length;
  const readAll = async () => {
    const r = await api.readAll({ project_id: projectId || undefined, days });
    setData((list) => (list ?? []).map((i) => (i.status === "new" ? { ...i, status: "read" } : i)));
    toast(r.updated ? `已将 ${r.updated} 条标为已读` : "没有未读信息", "info");
  };
  const majorRecent = projects.reduce((n, p) => n + (p.major_recent ?? 0), 0);
  const unread = projects.reduce((n, p) => n + (p.unread ?? 0), 0);
  const todo = (attention.data?.totals.crosscheck ?? 0) + (attention.data?.totals.silence ?? 0);
  const updating = projects.filter((p) => p.job_status === "running" && p.worker_state !== "stopped").length;
  const today = new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long" });

  return (
    <Page className="page-workspace">
      <PageHeader variant="feed" title="今日动态" label="动态汇总"
        description="公司、竞品、行业与政策的新进展。"
        meta={<>{today}{updating > 0 && <span> · {updating} 个项目正在更新</span>}</>} />
      {/* The band follows the two columns below it: the three feed figures over the list,
        * and 待核对 / 异常 over the 需要你处理 card it belongs to. */}
      <div className="kpis kpis-split">
        <Kpi label="关注的公司" value={projects.length} />
        <Kpi label="近 30 天重大事件" value={majorRecent} tone={majorRecent ? "major" : undefined} />
        <Kpi label="未读信息" value={unread} />
        <Kpi label="待核对 / 异常" value={todo} tone={todo ? "major" : undefined} />
      </div>
      <Columns main={<>
      <div className="filters">
        <Seg id="days" value={days} onChange={setDays} options={RANGES.map((r) => ({ value: r.days, label: r.label }))} />
        <Seg id="level" value={level} onChange={setLevel}
          options={[{ value: "", label: "全部" }, { value: "major", label: "重大" }, { value: "notable", label: "关注" }]} />
        <label className="check check-inline"><input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />只看未读{unreadHere > 0 && <span className="count">{unreadHere}</span>}</label>
        <select value={category} onChange={(e) => setCategory(e.target.value as Category | "")}>
          <option value="">全部类型</option>
          {Object.entries(CATEGORY_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value="">全部项目</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <span className="spacer" />
        {unreadHere > 0 && <button className="btn btn-sm btn-ghost" onClick={readAll}>全部标为已读</button>}
      </div>
      <ErrorNote error={error} />
      {loading && !data && <Skeleton lines={8} />}
      {data && items.length === 0 && unreadOnly && all.length > 0 && <Empty title="都读完了">“全部”中可回看已读动态。</Empty>}
      {data && all.length === 0 && (
        <Empty title="这段时间没有新动态">
          首次采集可能需几分钟。未检索到信息不代表没有变化。
        </Empty>
      )}
      <div className="feed-list">
        <ItemList key={`${days}-${level}-${category}-${projectId}`} items={older ? items : recent} showProject onChange={update} foldRoutine={!level && !unreadOnly} />
      </div>
      {!older && items.length > recent.length && <button className="btn btn-ghost btn-sm news-older" onClick={() => setOlder(true)}>
        更早 {items.length - recent.length} 条（{dayList.slice(3, 6).join("、")}{dayList.length > 6 ? " 等" : ""}）</button>}
      </>} side={<AttentionBoard data={attention.data} title="需要你处理" maxProjects={4} perProject={1} note="核对、沉默与官网变化" />} />
    </Page>
  );
}
