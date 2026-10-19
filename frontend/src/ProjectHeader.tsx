import { useReducedMotion } from "motion/react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { ProjectDetail } from "./api";
import Aurora from "./bits/Aurora";
import useHeaderPalette from "./headerPalette";

export type HeaderTarget = "brief" | "numbers" | "external" | "overview";

/** Shared project identity. Analysis and operating figures live in their own tabs. */
export default function ProjectHeader({ detail, actions, status }: {
  detail: ProjectDetail; actions: ReactNode; status?: ReactNode; goto: (target: HeaderTarget) => void;
}) {
  const { project } = detail;
  const stage = (project.stage || "").split(/[（(]/)[0].trim();
  const facts = [...new Set([project.industry, stage, project.location]
    .map((value) => value?.trim())
    .filter((value): value is string => Boolean(value) && !/^(?:未知|暂无|未确认|未披露|待确认|未提供|不详)/.test(value!)))];
  const legalName = project.legal_name && project.legal_name !== project.name ? project.legal_name : null;
  const website = websiteLink(project.website);

  return <header className="project-hero">
    <HeroGlow />
    <div className="hero-top">
      <Link className="project-breadcrumb" to="/projects">
        <svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true">
          <path d="M10 3 5 8l5 5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        项目组合
      </Link>
      <div className="project-actions">{actions}</div>
    </div>
    <div className="hero-body">
      <div className="hero-id">
        <h1>{project.name}</h1>
        {legalName && <p className="hero-legal">{legalName}</p>}
        {(facts.length > 0 || website) && <div className="hero-meta">
          {facts.map((fact) => <span key={fact}>{fact}</span>)}
          {website && <a href={website} target="_blank" rel="noreferrer noopener">公司官网 ↗</a>}
        </div>}
      </div>
    </div>
    {status && <div className="hero-status">{status}</div>}
  </header>;
}

/** Muted, slow-moving light keeps the shared identity calm; static colour remains without motion or WebGL. */
function HeroGlow() {
  const reduced = useReducedMotion();
  const palette = useHeaderPalette();
  const light = palette.deepLight === "1";
  const colors = light ? ["#e9e2d6", "#c9d1d0", "#e5dccb"] : ["#665f54", "#5e6c70", "#716758"];
  const background = light
    ? "radial-gradient(ellipse at 78% 0%, rgba(201,209,208,.40), transparent 62%), radial-gradient(ellipse at 30% 15%, rgba(229,220,203,.36), transparent 68%)"
    : "radial-gradient(ellipse at 78% 0%, rgba(94,108,112,.20), transparent 62%), radial-gradient(ellipse at 30% 15%, rgba(113,103,88,.18), transparent 68%)";

  return <div className="hero-glow hero-glow-editorial" aria-hidden="true" style={{ background }}>
    {!reduced && <Aurora colorStops={colors} amplitude={.5} blend={.84} speed={.16} lightMode={light} />}
  </div>;
}

function websiteLink(value?: string | null): string | null {
  if (!value?.trim()) return null;
  const candidate = value.trim();
  try {
    const url = new URL(/^[a-z][a-z\d+.-]*:/i.test(candidate) ? candidate : `https://${candidate}`);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}
