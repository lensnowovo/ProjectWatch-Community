import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useParams, useSearchParams } from "react-router-dom";
import { api, type Project } from "./api";
import { CommandPalette, EASE, ThemeToggle, ToastProvider } from "./motion";
import DigestPage from "./pages/Digest";
import FeedPage from "./pages/Feed";
import HomePage from "./pages/Home";
import NewProjectPage from "./pages/NewProject";
import ProjectPage from "./pages/Project";
import TasksPage from "./pages/Tasks";
import ProjectsPage from "./pages/Projects";
import SettingsPage from "./pages/Settings";
import { SourceDrawerProvider } from "./ui";
import SettingsGear from "./SettingsGear";
import ProjectNavStatus from "./ProjectNavStatus";

// 深度分析 is no longer a separate page: each part lives in the tab about its subject.
const DEEP_TAB: Record<string, string> = { position: "competition", technology: "industry", industry: "industry" };
export function DeepRedirect() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const tab = DEEP_TAB[params.get("tab") ?? ""];
  return <Navigate to={`/projects/${id}${tab ? `?tab=${tab}` : ""}`} replace />;
}

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

export default function App() {
  const location = useLocation();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [modelReady, setModelReady] = useState<boolean | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const refreshProjects = () => api.projects().then((list) => {
    setProjects(list);
    setProjectError(null);
  }).catch(() => setProjectError("暂时无法读取项目，请重试。"));
  const projectList = projects ?? [];
  // Running or queued work across watched projects, from the stored project rows (no extra request).
  const busyJobs = projectList.filter((p) => p.job_status === "running" || p.job_status === "queued").length;

  useEffect(() => {
    refreshProjects();
    api.settings().then((s) => setModelReady(s.model.ready)).catch(() => undefined);
    setMenuOpen(false);
    // Stored-state reads only: no models/search. Keep alert dots current while
    // background monitoring runs; hidden tabs do not poll.
    const timer = window.setInterval(() => {
      if (!document.hidden) refreshProjects();
    }, 30000);
    return () => window.clearInterval(timer);
  }, [location.pathname]);

  const section = location.pathname.split("/")[1] || "home";
  const routeKey = section === "projects" ? location.pathname : section;

  return (
    <MotionConfig reducedMotion="user">
      <ToastProvider>
        <SourceDrawerProvider>
          <div className={`shell ${menuOpen ? "menu-open" : ""}`}>
            <aside className="sidebar">
              <NavLink to="/" end className="brand" aria-label="项目监控首页">
                <span className="brand-mark" aria-hidden>
                  <svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="10.5" /><circle cx="16" cy="16" r="5" /><path d="M16 1.5v6M16 24.5v6M1.5 16h6M24.5 16h6" /></svg>
                </span>
                <div>
                  <div className="brand-name">ProjectWatch</div>
                  <div className="brand-sub">项目监控</div>
                </div>
              </NavLink>
              <NavLink to="/new" className="btn sidebar-add">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
                  <path d="M12 5v14M5 12h14" />
                </svg>
                <span>添加项目</span>
              </NavLink>
              <button className="search-trigger" onClick={() => window.dispatchEvent(new Event("pw:command"))}>
                <span>搜索或跳转</span><kbd>{isMac ? "⌘" : "Ctrl"} K</kbd>
              </button>
              <nav className="nav">
                <NavItem to="/feed" label="今日动态" />
                <NavItem to="/projects" label="项目组合" end />
                <NavItem to="/digest" label="周报" />
                <NavItem to="/tasks" label="任务">
                  {busyJobs > 0 && <span className="nav-count" title="进行中与排队的任务">{busyJobs}</span>}
                </NavItem>
              </nav>
              <div className="nav-section">关注的公司</div>
              <nav className="nav nav-projects">
                {projectList.map((p) => (
                  <NavItem key={p.id} to={`/projects/${p.id}`} label={p.name}>
                    <ProjectNavStatus project={p} />
                  </NavItem>
                ))}
                {projects?.length === 0 && <div className="nav-empty">还没有项目</div>}
              </nav>
              <div className="sidebar-foot">
                <NavItem to="/settings" label="设置" icon={<SettingsGear size={18} />}>
                  {modelReady === false && <span className="tag tag-warn">待配置</span>}
                </NavItem>
                <ThemeToggle />
              </div>
            </aside>
            <div className="main">
              <header className="topbar">
                <button className="icon-btn menu-btn" onClick={() => setMenuOpen((v) => !v)} aria-label="菜单">☰</button>
                <NavLink to="/" className="topbar-title" aria-label="项目监控首页">ProjectWatch</NavLink>
              </header>
              <AnimatePresence>
                {modelReady === false && location.pathname !== "/settings" && (
                  <motion.div className="banner" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.3, ease: EASE }}>
                    <div className="banner-inner">
                      未连接 AI 模型：仅收集公开新闻，暂不分析资料或筛选信息。
                      <NavLink to="/settings">去设置 →</NavLink>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
              <main className="content">
                {/* Company changes remove the old workspace immediately, not after an exit animation. */}
                <AnimatePresence key={section === "projects" ? routeKey : "sections"} mode="wait" initial={false}>
                  <Routes location={location} key={routeKey}>
                    <Route path="/" element={<HomePage projects={projects} modelReady={modelReady} error={projectError} onRetry={refreshProjects} />} />
                    <Route path="/feed" element={<FeedPage projects={projectList} />} />
                    <Route path="/projects" element={<ProjectsPage onChanged={refreshProjects} />} />
                    <Route path="/projects/:id" element={<ProjectPage onChanged={refreshProjects} />} />
                    <Route path="/projects/:id/deep" element={<DeepRedirect />} />
                    <Route path="/new" element={<NewProjectPage />} />
                    <Route path="/digest" element={<DigestPage />} />
                    <Route path="/tasks" element={<TasksPage projects={projectList} />} />
                    <Route path="/settings" element={<SettingsPage onSaved={(ready) => setModelReady(ready)} />} />
                    <Route path="*" element={<Navigate to="/" replace />} />
                  </Routes>
                </AnimatePresence>
              </main>
            </div>
            <div className="scrim" onClick={() => setMenuOpen(false)} />
          </div>
          <CommandPalette projects={projectList} />
        </SourceDrawerProvider>
      </ToastProvider>
    </MotionConfig>
  );
}

function NavItem({ to, label, end, children, icon }: { to: string; label: string; end?: boolean; children?: ReactNode; icon?: ReactNode }) {
  return (
    <NavLink to={to} end={end} className="nav-item">
      {({ isActive }) => (
        <>
          {isActive && <motion.span layoutId="nav-active" className="nav-active" transition={{ type: "spring", stiffness: 520, damping: 42 }} />}
          {icon}
          <span className="nav-label truncate">{label}</span>
          {children}
        </>
      )}
    </NavLink>
  );
}
