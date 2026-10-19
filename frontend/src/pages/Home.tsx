import { motion } from "motion/react";
import { Link } from "react-router-dom";
import type { Project } from "../api";
import { HeroWaves } from "../hero";
import { EASE, Page } from "../motion";
import { ErrorNote } from "../ui";

/** A permanent cover: the drawn lines fill the page and nothing about any project is shown here.
 * What changed and what needs attention live on 今日动态 and 项目组合. */
export default function HomePage({ projects, modelReady, error, onRetry }: {
  projects: Project[] | null;
  modelReady: boolean | null;
  error: string | null;
  onRetry: () => void;
}) {
  const hasProjects = projects !== null && projects.length > 0;
  const empty = projects !== null && projects.length === 0;

  return (
    <Page className="page-hero">
      <div className="hero">
        <HeroWaves />
        <motion.div className="hero-copy" initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, ease: EASE, delay: 0.1 }}>
          <div className="eyebrow">ProjectWatch · 项目监控</div>
          <h1 className="display">
            {empty ? <>输入公司名，<br />或者拖入一份 BP。</>
              : <>关注的公司，<br />持续看见变化。</>}
          </h1>
          <p>整理公司材料，跟踪公司、竞品与行业动态，保留出处。</p>
          <div className="hero-actions">
            {hasProjects ? <>
              <Link to="/feed" className="btn btn-primary btn-lg">查看项目动态</Link>
              <Link to="/projects" className="btn btn-ghost btn-lg">项目组合</Link>
              <Link to="/new" className="btn btn-ghost btn-lg">添加项目</Link>
            </> : empty ? <>
              <Link to="/new" className="btn btn-primary btn-lg">添加第一个项目</Link>
              {modelReady !== true && <Link to="/settings" className="btn btn-ghost btn-lg">先配置模型</Link>}
            </> : error ? <>
              <button className="btn btn-primary btn-lg" onClick={onRetry}>重新读取项目</button>
              <Link to="/projects" className="btn btn-ghost btn-lg">项目组合</Link>
            </> : <span className="muted" role="status">正在读取项目…</span>}
          </div>
          <ErrorNote error={error} />
        </motion.div>
      </div>
    </Page>
  );
}
