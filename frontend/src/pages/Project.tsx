import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, type Category, type BodyCoverage, type Job, type ProjectDetail } from "../api";
import { CompetitionView, IndustryView, NewsView } from "../landscape";
import { MetricsView } from "../metrics";
import { Columns, Figure, Reference, Skeleton, TabSkeleton, VerdictBand } from "../frame";
import { RulesCard, SiteWatchCard } from "../monitor";
import { CadenceCard } from "../signals";
import { UploadDialog } from "../upload";
import { EASE, Page, Reveal, useToast } from "../motion";
import { CompanyOverview, CurrentBrief, KeyFigures, OWN_CATEGORIES, type Tab } from "../overview";
import { PlanView } from "../plan";
import { Drawer, ErrorNote, Spinner, Working, errorMessage, locatorLabel, relTime, useAsync } from "../ui";
import ProjectHeader from "../ProjectHeader";
import CommercialSources from "../CommercialSources";
import { RaceBoard } from "../race";
import { raceRevision } from '../raceData';
import { TrackBoard } from "../track";
import { TechRoutes } from "../tech";
import { monitoringLabel } from "../MonitoringProgress";
import { jobWorkState, WORK_LABEL } from "../workState";
import { RecoveryDialog } from '../RecoveryDialog';
import { IndustryJudgment, TechnologyJudgment, useInsight } from "../judgment";
import { ClinicalView, PapersView, PatentsView } from "../researchPages";
import { BusinessView, OperationsView, RisksView, internalCounts } from "../internalPages";
import { FinanceAnalysisView } from "../financeAnalysis";

/** Five sections (8945); each holds one or more of the original pages, which stay the URL's ?tab= values so every
 * existing link keeps working. */
type Section = "home" | "internal" | "external" | "scope" | "analysis";
const SECTIONS: { id: Section; label: string; pages: { id: Tab; label: string }[] }[] = [
  { id: "analysis", label: "当前判断", pages: [{ id: "brief", label: "按问题看判断" }] },
  { id: "home", label: "项目总览", pages: [{ id: "overview", label: "公司概览" }] },
  { id: "internal", label: "内部信息", pages: [{ id: "ops", label: "经营概览" }, { id: "business", label: "业务与研发" }, { id: "numbers", label: "财务报表" },
    { id: "fin", label: "财务分析" }, { id: "risks", label: "风险与核对" }, { id: "documents", label: "资料与版本" }] },
  { id: "external", label: "外部信息", pages: [{ id: "external", label: "公司进展" }, { id: "trials", label: "临床" }, { id: "papers", label: "论文" },
    { id: "patents", label: "专利与注册" }, { id: "competition", label: "竞争对手" }, { id: "industry", label: "行业环境" }] },
  { id: "scope", label: "监控范围", pages: [{ id: "plan", label: "监控方案" }] },
];
const sectionOf = (t: Tab) => SECTIONS.find((s) => s.pages.some((p) => p.id === t)) ?? SECTIONS[0];

const JOB_LABEL: Record<Job["kind"], string> = { internal: "分析内部资料", external: "整理首轮信息", external_followup: "补全外部信息", brief: "生成简报", public_profile: "补充公开档案", landscape: "整理竞争与行业", business: "整理产品与用途", competition: "更新竞对资料", details: "整理商业关系与政策条件", commercial: "更新专业来源", insight: "深度分析" };

const ownCount = (d: ProjectDetail) =>
  Object.entries(d.counts).filter(([k]) => OWN_CATEGORIES.has(k)).reduce((n, [, v]) => n + (v ?? 0), 0);

export default function ProjectPage({ onChanged }: { onChanged: () => void }) {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  // Core opens the company overview; explicit saved-judgment links remain readable.
  const raw = params.get("tab") as Tab | null;
  const { data, error, loading, reload } = useAsync(() => api.project(id), [id]);
  const defaultTab = data?.capabilities?.insight_generation === true ? "brief" : "overview";
  const tab: Tab = raw && SECTIONS.some((s) => s.pages.some((p) => p.id === raw)) ? raw : defaultTab;
  const busy = data?.jobs.some((j) => j.status === "queued" || j.status === "running")
    || data?.documents.some((d) => d.status === "queued" || d.status === "processing")
    || (data?.model_ready && (data?.pending_documents ?? 0) > 0);

  useEffect(() => {
    if (!busy) return;
    const stopped = data?.execution?.analysis === "stopped" && data?.execution?.parsing === "stopped";
    const timer = window.setInterval(() => reload(), stopped ? 30000 : 4000);
    return () => window.clearInterval(timer);
  }, [busy, reload, data?.execution?.analysis, data?.execution?.parsing]);

  if (loading && !data) return <Page className="page-wide page-project"><div className="project-head-skel"><Skeleton lines={2} /></div><TabSkeleton label="加载项目" /></Page>;
  if (error && !data) return <Page><ErrorNote error={error} /></Page>;
  if (!data) return null;

  const { project } = data;
  const commercialRevision = data.jobs.map(j => `${j.id}:${j.status}`).join("|");
  const setTab = (t: Tab) => setParams(previous => {
    const next = new URLSearchParams(previous);
    if (t === defaultTab) next.delete('tab'); else next.set('tab', t);
    return next;
  });
  const section = sectionOf(tab);

  return (
    <Page className="page-wide page-project">
      <Header detail={data} goto={setTab} onReload={() => { reload(); onChanged(); }} onRemoved={onChanged} status={<Progress detail={data} onReload={reload} />} />
      <div className="tabs-row">
      <nav className="tabs" aria-label="项目内容">
        {SECTIONS.map((sec, i) => (
          <button key={sec.id} className={section.id === sec.id ? "on" : ""} aria-current={section.id === sec.id ? "page" : undefined}
            onClick={() => section.id !== sec.id && setTab(sec.pages[0].id)}>
            {section.id === sec.id && <motion.span layoutId="tab-underline" className="tab-underline" transition={{ type: "spring", stiffness: 500, damping: 40 }} />}
            <span className="tab-no">0{i + 1}</span>{sec.label}
            {sec.id === "external" && ownCount(data) > 0 && <span className="count">{ownCount(data)}</span>}
            {sec.id === "internal" && data.documents.length > 0 && <span className="count">{data.documents.length}</span>}
          </button>
        ))}
      </nav>
      </div>
      {section.id === "external" && <CategoryStrip detail={data} page={tab} go={setTab} />}
      {section.id === "internal" && <PageStrip label="内部信息分区" page={tab} go={setTab} cells={internalCounts(data)} />}
      {section.pages.length > 1 && section.id !== "external" && section.id !== "internal" && <nav className="subtabs" aria-label={`${section.label}分区`}>
        {section.pages.map((p) => <button key={p.id} className={tab === p.id ? "on" : ""} aria-current={tab === p.id ? "page" : undefined} onClick={() => setTab(p.id)}>
          {p.label}{p.id === "documents" && data.documents.length > 0 && <span className="count">{data.documents.length}</span>}</button>)}
      </nav>}
      <AnimatePresence mode="wait" initial={false}>
        <motion.div key={tab} className="tab-panel" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0, transition: { duration: 0.22, ease: EASE } }}
          exit={{ opacity: 0, transition: { duration: 0.1 } }}>
          {tab === "overview" && <CompanyOverview detail={data} onReload={reload} />}
          {tab === "plan" && <PlanView projectId={id} company={data.project.short_name || data.project.name} detail={data} />}
          {tab === "brief" && <CurrentBrief detail={data} goto={setTab} onReload={reload}
            aside={<Note projectId={project.id} initial={project.note ?? ""} onSaved={reload} />} />}
          {tab === "ops" && <OperationsView detail={data} go={setTab} />}
          {tab === "business" && <BusinessView detail={data} go={setTab} />}
          {tab === "risks" && <RisksView detail={data} go={setTab} />}
          {tab === "fin" && <FinanceAnalysisView detail={data} go={setTab} />}
          {tab === "numbers" && <MetricsView metrics={data.metrics} issues={data.issues ?? []} finance={data.finance} risks={data.profile?.risks}
            figures={<KeyFigures numbers={data.finance?.key_numbers} />} />}
          {tab === "external" && <NewsView detail={data} financing={<CommercialSources projectId={id} revision={commercialRevision} onChanged={reload} />} />}
          {tab === "trials" && <ClinicalView detail={data} />}
          {tab === "papers" && <PapersView detail={data} />}
          {tab === "patents" && <PatentsView detail={data} />}
          {tab === "competition" && <CompetitionView detail={data} onReload={reload}
            race={data.capabilities?.insight_projections === true && <RaceBoard projectId={id} revision={raceRevision(data)} />}
            financing={<CommercialSources projectId={id} revision={commercialRevision} peers onChanged={reload} />} />}
          {tab === "industry" && (data.capabilities?.insight_projections === true ? <IndustryFrame projectId={id} revision={`${commercialRevision}|${project.last_external_at ?? ""}`}
            insightRevision={project.last_external_at ?? ""} details={<IndustryView detail={data} />} /> : <IndustryView detail={data} />)}
          {tab === "documents" && <DocumentsTab detail={data} onUploaded={reload} />}
        </motion.div>
      </AnimatePresence>
      {!project.activated && project.status !== 'paused' && (
        <div className="note">尚未启动自动监控。
          <button className="link" onClick={() => api.activate(id).then(() => reload())}>立即启动</button>
        </div>
      )}
    </Page>
  );
}

function Header({ detail, goto, onReload, status, onRemoved }: { detail: ProjectDetail; goto: (tab: Tab) => void; onReload: () => void; status: React.ReactNode; onRemoved: () => void }) {
  const { project } = detail;
  const [adding, setAdding] = useState(false);
  const removed = project.status === "removed";
  const restore = async () => {
    setError(null);
    try { await api.restoreProject(project.id); toast("已恢复关注"); onRemoved(); onReload(); }
    catch (e) { setError(e); }
  };
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  const refresh = async () => {
    setError(null);
    try {
      await api.refresh(project.id, "external");
      toast("已加入更新队列");
      onReload();
    } catch (e) {
      setError(e);
    }
  };
  const toggle = async () => {
    const next = project.status === "paused" ? "active" : "paused";
    await api.patchProject(project.id, { status: next });
    toast(next === "paused" ? "已暂停自动监控" : "已恢复自动监控", "info");
    onReload();
  };

  return (
    <>
      {removed && <div className="removed-banner" role="status">
        <span><strong>已移出关注</strong>：不再自动更新，也不出现在列表中；资料与历史记录仍保留在本机。</span>
        <button className="btn btn-sm" onClick={restore}>恢复关注</button>
      </div>}
      <ProjectHeader detail={detail} goto={goto} actions={removed ? null : <>
        <button className="btn project-upload" onClick={() => setAdding(true)}>＋ 上传资料</button>
        <button className="btn" onClick={refresh} disabled={!project.activated}>立即更新</button>
        <button className="btn btn-ghost" onClick={toggle}>{project.status === "paused" ? "恢复监控" : "暂停监控"}</button>
      </>} status={status} />
      <UploadDialog projectId={project.id} open={adding} onClose={() => setAdding(false)} onAdded={onReload} />
      <ErrorNote error={error} />
    </>
  );
}

export function Progress({ detail, onReload }: { detail: ProjectDetail; onReload: () => void }) {
  const [recovering, setRecovering] = useState(false);
  const active = detail.jobs.filter((j) => j.status === "queued" || j.status === "running");
  const parsing = detail.documents.filter((d) => d.status === "queued" || d.status === "processing");
  const failed = detail.jobs.find((j) => j.status === "failed");
  const lastDone = detail.jobs.find((j) => j.status === "done");
  const waiting = detail.model_ready ? detail.pending_documents : 0;
  useEffect(() => { setRecovering(false); }, [detail.project.id, failed?.id, active.length]);
  if (detail.execution?.health?.restart_required) {
    return <div className="progress progress-error" role="status"><strong>后台受阻，等待恢复</strong><span>已有结果保留，不会自动重发结果不明的请求。</span><Link to="/tasks">查看运行状态 →</Link></div>;
  }
  if (detail.brief?.origin && !active.length && !parsing.length && !failed && detail.project.status === "paused") {
    return <div className="progress"><strong>{detail.brief.origin.producer} 已更新</strong><span className="muted"> · 自动监控已暂停，本批由外部 Agent 写回</span></div>;
  }
  if (active.length || parsing.length || waiting) {
    const states = active.map(j => jobWorkState(detail, j));
    const parseRunning = parsing.some(d => d.status === "processing") && detail.execution?.parsing !== "stopped";
    const executing = parseRunning || states.some(s => s === "running" || s === "stopping");
    const stopping = !states.includes("running") && !parseRunning && states.includes("stopping");
    const offline = states.includes("offline") || (parsing.length > 0 && detail.execution?.parsing === "stopped")
      || (!!waiting && detail.execution?.analysis === "stopped");
    const paused = states.length > 0 && states.every(s => s === "paused") && !parsing.length && !waiting;
    const label = executing ? stopping ? "正在结束当前更新" : "正在更新"
      : paused ? "监控已暂停" : offline ? "自动更新未运行" : "等待更新";
    return (
      <motion.div className="progress" initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }}>
        <div>
          {executing ? <Working text={label} /> : <strong>{label}</strong>}
          <span className="muted">
            {parsing.length > 0 && ` · ${parseRunning ? "正在解析" : "等待解析"} ${parsing.length} 份资料`}
            {!parsing.length && waiting > 0 && !active.some((j) => j.kind === "internal") && ` · ${waiting} 份新资料等待分析`}
            {active.map((j) => {
              const state = jobWorkState(detail, j);
              return ` · ${JOB_LABEL[j.kind]}${state === "running" && j.stage ? `：${j.stage}` : `（${WORK_LABEL[state]}）`}`;
            }).join("")}
          </span>
        </div>
      </motion.div>
    );
  }
  if (failed && (!lastDone || failed.created_at > lastDone.created_at)) {
    return (
      <div className="progress progress-error">
        <strong>上次{JOB_LABEL[failed.kind]}没有完成</strong>
        <span>{errorMessage(failed.error)}</span>
        <button className="link" onClick={() => setRecovering(true)}>继续更新 →</button>
        {recovering && <RecoveryDialog key={`${detail.project.id}:${failed.id}`} projectId={detail.project.id} job={failed}
          onClose={() => setRecovering(false)} onQueued={onReload} />}
        {!detail.model_ready && <Link to="/settings">去设置模型 →</Link>}
      </div>
    );
  }
  if (detail.detail_summary?.status === "partial") {
    const summary = detail.detail_summary;
    return <div className="progress progress-idle muted small">
      商业关系与政策条件：本次整理保留 {summary.fact_count} 条结果
      {summary.rejected > 0 && ` · ${summary.rejected} 条线索未采纳`}
      {summary.unquantified > 0 && ` · ${summary.unquantified} 条金额未明确`}
    </div>;
  }
  return (
    <div className="progress progress-idle muted small">
      {monitoringLabel(detail.monitoring_progress, detail.project.status === "paused")}
      {detail.execution?.analysis === "stopped" && detail.project.status !== "paused" && " · 自动更新未运行"}
      {detail.project.last_external_at && ` · 外部信息更新于 ${relTime(detail.project.last_external_at)}`}
      {detail.project.last_internal_at && ` · 资料分析于 ${relTime(detail.project.last_internal_at)}`}
    </div>
  );
}

function Note({ projectId, initial, onSaved }: { projectId: string; initial: string; onSaved: () => void }) {
  const [value, setValue] = useState(initial);
  const [state, setState] = useState<"" | "saving" | "saved">("");
  const toast = useToast();
  useEffect(() => setValue(initial), [initial]);
  const save = async () => {
    setState("saving");
    await api.patchProject(projectId, { note: value });
    setState("saved");
    toast("已保存你的判断");
    onSaved();
  };
  return (
    <Reveal as="section" className="card card-pad">
      <div className="card-head"><h3>我的判断</h3>{state === "saved" && <span className="muted small">已保存</span>}</div>
      <textarea rows={5} value={value} placeholder="项目看法与跟进要点（本机保存，AI 不改写）"
        onChange={(e) => { setValue(e.target.value); setState(""); }} />
      <div className="actions">
        <button className="btn btn-sm" disabled={value === initial || state === "saving"} onClick={save}>保存</button>
      </div>
    </Reveal>
  );
}

type CoverageKey = Category | "filing" | "registry" | "cn_trial" | "site" | "watch" | "thread" | "tender" | "license";
const COVERAGE_ROWS: { key: CoverageKey; label: string; free: boolean }[] = [
  { key: "company", label: "公司新闻", free: true },
  { key: "site", label: "公司官网页面", free: true },
  { key: "watch", label: "我的关注点", free: true },
  { key: "thread", label: "事件线索后续", free: true },
  { key: "tender", label: "招投标（政府采购平台）", free: false },
  { key: "license", label: "资质与许可（监管部门网站）", free: false },
  { key: "competitor", label: "竞品动态", free: true },
  { key: "industry", label: "赛道动态", free: true },
  { key: "policy", label: "政策监管", free: true },
  { key: "paper", label: "论文（OpenAlex）", free: true },
  { key: "trial", label: "临床试验（ClinicalTrials.gov）", free: true },
  { key: "patent", label: "专利", free: false },
  { key: "filing", label: "上市公司公告（巨潮资讯）", free: true },
  { key: "cn_trial", label: "国内临床登记与审评", free: false },
  { key: "registry", label: "工商变更", free: false },
];

function Coverage({ detail }: { detail: ProjectDetail }) {
  const plan = detail.plan;
  const report = plan?.last_report ?? {};
  // Judge “needs configuring” by today's settings; the round's own flags only say what that round could use.
  const hasSearch = detail.search_ready ?? plan?.has_search;
  const hasRegistry = detail.registry_ready ?? plan?.has_registry;
  return (
    <>
      <section className="coverage-block">
        {!plan && <p className="muted small">第一次检索完成后显示。</p>}
        {plan && (
          <table className="coverage">
            <tbody>
              {COVERAGE_ROWS.map((row) => {
                const r = report[row.key];
                let status: React.ReactNode;
                if (row.key === "trial" && !plan.is_biomed) status = <span className="muted">不适用</span>;
                else if (row.key === "cn_trial" && !plan.is_biomed) status = <span className="muted">不适用</span>;
                else if ((row.key === "patent" || row.key === "cn_trial" || row.key === "tender" || row.key === "license") && !hasSearch) status = <Link to="/settings" className="muted">需配置搜索服务</Link>;
                else if (row.key === "registry" && !hasRegistry) status = <Link to="/settings" className="muted">需配置工商数据服务</Link>;
                else if (!r && !plan.has_search && hasSearch) status = <span className="muted">下次更新时检索</span>;
                else if (!r && (row.key === "watch" || row.key === "thread")) status = <span className="muted">暂无</span>;
                else if (!r && row.key === "site") status = <span className="muted">见“官网监测”</span>;
                else if (!r) status = <span className="muted">未检索</span>;
                else if (r.errors.length && !r.checked) status = <span className="text-warn" title={r.errors.join("\n")}>检索失败</span>;
                else status = <span>{row.key === "site" ? "变化" : "新增"} {r.added}{r.errors.length ? <span className="text-warn" title={r.errors.join("\n")}> · 部分失败</span> : null}</span>;
                return <tr key={row.key}><td>{row.label}</td><td className="num">{status}</td></tr>;
              })}
            </tbody>
          </table>
        )}
        <p className="muted small">未检索到不代表未发生；覆盖受网络与来源限制。</p>
      </section>
    </>
  );
}

/** The search terms the system generated for this company (the plan itself lives on 监控方案). */
function QueryTerms({ detail }: { detail: ProjectDetail }) {
  const plan = detail.plan;
  return (
    <>
      {plan && (
        <section>
          <dl className="plan">
            <dt>公司检索词</dt><dd>{plan.company_queries.join("、") || "—"}</dd>
            {plan.people.length > 0 && <><dt>核心人物</dt><dd>{plan.people.join("、")}</dd></>}
            <dt>赛道</dt><dd>{plan.industry_queries.join("、") || "—"}</dd>
            {plan.policy_queries.length > 0 && <><dt>政策</dt><dd>{plan.policy_queries.join("、")}</dd></>}
            {plan.affiliation_names.length + plan.paper_queries.length > 0 && <><dt>论文</dt><dd>{[...plan.affiliation_names, ...plan.paper_queries].join("、")}</dd></>}
            {plan.is_biomed && <><dt>临床申办方</dt><dd>{plan.trial_sponsors.join("、") || "—"}</dd></>}
          </dl>
          <p className="muted small">AI 生成，随资料更新；关注点与检索方向在“监控方案”里调整。</p>
        </section>
      )}
    </>
  );
}

function Documents({ detail, onUploaded }: { detail: ProjectDetail; onUploaded: () => void }) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [dragging, setDragging] = useState(false);
  const upload = async (files: FileList) => {
    setBusy(true);
    setError(null);
    try {
      for (const f of Array.from(files)) await api.uploadFile(detail.project.id, f);
      onUploaded();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const retry = async (sourceId: string) => {
    setError(null);
    try { await api.retryFile(detail.project.id, sourceId); onUploaded(); }
    catch (e) { setError(e); }
  };
  const STATUS: Record<string, string> = { queued: "等待识别", processing: "正在识别文字", completed: "已识别", needs_ocr: "部分页面未识别", failed: "识别失败" };
  return (
    <div className="documents">
      <div className={`dropzone ${dragging ? "drag" : ""}`} role="button" tabIndex={0}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); upload(e.dataTransfer.files); }}
        onClick={() => setAdding(true)}>
        {busy ? <Spinner label="上传中" /> : <><strong>拖入文件，或点击添加资料</strong><span className="muted">文件 / 公开链接 · 自动解析</span></>}
      </div>
      <UploadDialog projectId={detail.project.id} open={adding} onClose={() => setAdding(false)} onAdded={onUploaded} />
      <ErrorNote error={error} />
      {detail.documents.some((d) => d.status === "needs_ocr") && <div className="note">
        {detail.ocr?.available ? "部分页面未识别，可重试；已识别内容仍可分析。" : "缺少中文 OCR，请安装 OCR 版本后重试；已识别内容保留。"}
      </div>}
      {detail.documents.length > 0 && (
        <div className="card">
          <table className="table">
            <thead><tr><th>文件</th><th>收到时间</th><th>状态</th><th /></tr></thead>
            <tbody>
              {detail.documents.map((d) => (
                <tr key={d.id}>
                  <td className="cell-title">{d.name}</td>
                  <td className="muted">{new Date(d.created_at).toLocaleString("zh-CN")}</td>
                  <td><span className={`tag ${d.status === "completed" ? "tag-ok" : d.status === "failed" || d.status === "needs_ocr" ? "tag-warn" : ""}`}>{d.status === "needs_ocr" && !d.fragments ? "未读到文字" : STATUS[d.status] ?? d.status}</span>{d.error && <span className="muted small"> {d.error}</span>}{!!d.fragments && <span className="muted small"> · 已读 {d.fragments} 段</span>}</td>
                  <td><DocOpen id={d.id} name={d.name} disabled={!d.fragments && d.status !== "completed"} />
                    {(d.status === "failed" || d.status === "needs_ocr") && <button className="link" onClick={() => retry(d.id)}>重新识别</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">原件本机保存。</p>
    </div>
  );
}

/** 资料库: files and the public sources' health; long tables fold into the reference area. */
function DocumentsTab({ detail, onUploaded }: { detail: ProjectDetail; onUploaded: () => void }) {
  const docs = detail.documents;
  const pending = docs.filter((d) => d.status === "needs_ocr" || d.status === "failed").length;
  const read = docs.filter((d) => d.status === "completed" || d.fragments).length;
  const c = detail.body_coverage;
  const rate = c && c.total ? Math.round(((c.article + c.twin) / Math.max(1, c.total - (c.declined ?? 0))) * 100) : null;
  const report = detail.plan?.last_report ?? {};
  const failed = Object.values(report).filter((r) => r.errors.length && !r.checked).length;
  return (
    <div className="tab-frame documents-frame">
      <VerdictBand eyebrow="资料库" meta="原件本机保存"
        title={docs.length ? `${read} 份内部资料已读入${pending ? `，${pending} 份需要处理` : ""}${rate !== null ? `；近 30 天报道正文读取率 ${rate}%` : ""}` : "还没有内部资料：上传 BP、年报或经营月报"}
        figures={<>
          <Figure label="内部资料" value={docs.length} unit="份" note={docs.length ? `已读入 ${read} 份` : "拖入或点击上传"} />
          <Figure label="需要处理" value={pending} unit="份" note="未识别或识别失败" tone={pending ? "warn" : undefined} />
          <Figure label="报道正文读取率" value={rate ?? "—"} unit={rate !== null ? "%" : undefined} note="AI 依据正文判断的比例" />
          <Figure label="上轮检索失败" value={failed} unit="项" note="见下方检索覆盖" tone={failed ? "warn" : undefined} />
        </>} />
      <Columns
        main={<><Documents detail={detail} onUploaded={onUploaded} /><MonitorSources detail={detail} /></>}
        side={<MonitorCadence detail={detail} />} />
      <Reference items={[
        { key: "coverage", title: "检索覆盖", note: "上一轮每类来源的新增与失败", body: <Coverage detail={detail} /> },
        { key: "body", title: "报道正文", note: rate !== null ? `读取率 ${rate}%` : undefined, body: c && c.total > 0 ? <BodyCoverageCard c={c} /> : null },
        { key: "terms", title: "检索词", note: "系统生成", body: detail.plan ? <QueryTerms detail={detail} /> : null },
        { key: "rules", title: "过滤规则", body: <MonitorRules detail={detail} /> },
      ]} />
    </div>
  );
}

function DocOpen({ id, name, disabled }: { id: string; name: string; disabled: boolean }) {
  const [open, setOpen] = useState(false);
  const { data } = useAsync(() => (open ? api.fragments(id) : Promise.resolve(null)), [open, id]);
  return (
    <>
      <button className="link" disabled={disabled} onClick={() => setOpen(true)}>阅读</button>
      <Drawer open={open} onClose={() => setOpen(false)} eyebrow="资料" title={name}>
        {!data && <Spinner />}
        {data?.map((f) => <p key={f.id} className="frag"><span className="frag-loc">{locatorLabel(f.locator)}</span>{f.text}</p>)}
      </Drawer>
    </>
  );
}

function BodyCoverageCard({ c }: { c: BodyCoverage }) {
  const parts = [
    { key: "article", label: "原文／部分正文", n: c.article },
    { key: "twin", label: "其他来源转载", n: c.twin },
    { key: "summary", label: "搜索引擎摘要", n: c.summary },
    { key: "none", label: "仅标题／短摘要", n: c.none },
    { key: "declined", label: "判断为同名或无关，未读取", n: c.declined ?? 0 },
  ];
  const judged = c.article + c.twin;
  const considered = Math.max(1, c.total - (c.declined ?? 0));
  return (
    <section className="card card-pad body-coverage">
      <div className="card-head">
        <h3>报道正文</h3>
        <span className="muted small">近 30 天收集 · AI 依据正文判断的比例 {Math.round((judged / considered) * 100)}%</span>
      </div>
      <div className="cov-bar" role="img" aria-label={parts.map((p) => `${p.label} ${p.n}`).join("，")}>
        {parts.filter((p) => p.n).map((p) => <span key={p.key} className={`cov-${p.key}`} ref={(el) => el?.style.setProperty("flex-grow", String(p.n))} />)}
      </div>
      <ul className="cov-legend">
        {parts.map((p) => <li key={p.key}><i className={`cov-${p.key}`} />{p.label}<b className="num">{p.n}</b></li>)}
      </ul>
      {c.outlets.some((o) => o.read < o.total) && (
        <>
          <h4>读取困难的网站</h4>
          <ul className="cov-outlets">
            {c.outlets.filter((o) => o.read < o.total).slice(0, 5).map((o) => (
              <li key={o.host}><span>{o.host}</span><span className="num muted">{o.read}/{o.total}</span></li>
            ))}
          </ul>
        </>
      )}
      <p className="muted small">原文不可读时采用转载或搜索摘要；仅有标题时保守判断。</p>
    </section>
  );
}

function MonitorSources({ detail }: { detail: ProjectDetail }) {
  const { data, reload } = useAsync(() => api.monitor(detail.project.id), [detail.project.id, detail.project.last_external_at]);
  if (!data) return null;
  return <SiteWatchCard projectId={detail.project.id} website={detail.project.website} state={data} onChanged={reload} />;
}

function MonitorRules({ detail }: { detail: ProjectDetail }) {
  const { data, reload } = useAsync(() => api.monitor(detail.project.id), [detail.project.id, detail.project.last_external_at]);
  if (!data) return null;
  return <RulesCard projectId={detail.project.id} rules={data.rules} onChanged={reload} />;
}

function MonitorCadence({ detail }: { detail: ProjectDetail }) {
  const { data } = useAsync(() => api.signals(detail.project.id), [detail.project.id, detail.project.last_external_at]);
  return <CadenceCard data={data ?? null} />;
}

/** 行业环境 reads the same AI cross-analysis as the cover; each part renders nothing until it exists. */
function IndustryInsight({ projectId, revision, part, onSettled }: { projectId: string; revision: string; part: "industry" | "technology"; onSettled?: () => void }) {
  const { data, loading } = useInsight(projectId, revision);
  const settled = !loading || !!data;
  useEffect(() => { if (settled) onSettled?.(); }, [settled]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!settled) return null;  // the frame shows nothing below until this settles, so there is nothing to push
  return part === "industry" ? <IndustryJudgment view={data} /> : <TechnologyJudgment view={data} />;
}

/** 外部信息 at a glance (8945) and its navigation: one cell per page with the count of what was collected for it. */
const STRIP: { page: Tab; label: string; cats: Category[]; always?: boolean }[] = [
  { page: "external", label: "公司进展", cats: ["company"], always: true },
  { page: "trials", label: "临床", cats: ["trial"] },
  { page: "papers", label: "论文", cats: ["paper"] },
  { page: "patents", label: "专利与注册", cats: ["patent"] },
  { page: "competition", label: "竞品动态", cats: ["competitor"], always: true },
  { page: "industry", label: "行业与政策", cats: ["industry", "policy"], always: true },
];

function CategoryStrip({ detail, page, go }: { detail: ProjectDetail; page: Tab; go: (page: Tab) => void }) {
  const cells = STRIP.map((s) => ({ ...s, n: s.cats.reduce((a, c) => a + (detail.counts[c] ?? 0), 0) })).filter((s) => s.always || s.n > 0 || s.page === page);
  return <PageStrip label="外部信息分区" page={page} go={go} cells={cells} />;
}

/** One strip for every multi-page section: each cell is a page and the count of what it reads. */
function PageStrip({ label, page, go, cells }: { label: string; page: Tab; go: (page: Tab) => void; cells: { page: Tab; label: string; n: number }[] }) {
  return <nav className="cat-strip" aria-label={label}>
    {cells.map((c) => <button key={c.page} className={page === c.page ? "on" : ""} aria-current={page === c.page ? "page" : undefined}
      onClick={() => go(c.page)}><span>{c.label}</span><b>{c.n}</b></button>)}
  </nav>;
}

/** 行业环境 reveals top to bottom: a block appears only once the one above has settled, so nothing already on screen
 * is pushed down by a slower block arriving above it. */
function IndustryFrame({ projectId, revision, insightRevision, details }: { projectId: string; revision: string; insightRevision: string; details: ReactNode }) {
  const [track, setTrack] = useState(false);
  const [judged, setJudged] = useState(false);
  const [tech, setTech] = useState(false);
  useEffect(() => { setTrack(false); setJudged(false); setTech(false); }, [projectId]);
  return <div className="tab-frame industry-frame">
    <TrackBoard projectId={projectId} revision={revision} onSettled={() => setTrack(true)} />
    {track && <IndustryInsight projectId={projectId} revision={insightRevision} part="industry" onSettled={() => setJudged(true)} />}
    {track && judged && <TechRoutes projectId={projectId} revision={insightRevision} onSettled={() => setTech(true)} />}
    {track && judged && tech && <IndustryInsight projectId={projectId} revision={insightRevision} part="technology" />}
    {track && judged && tech && <Reference items={[{ key: "industry", title: "行业细节：市场指标、资本动态、政策与赛道报道", note: "上方结论的依据", body: details }]} />}
  </div>;
}
