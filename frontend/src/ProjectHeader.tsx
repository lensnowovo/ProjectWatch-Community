import { useReducedMotion } from "motion/react";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { ProjectDetail } from "./api";
import Aurora from "./bits/Aurora";
import useHeaderPalette from "./headerPalette";
import { businessIntro, dossierEntries, headerChanges, safeWebsite, stageLabel, type DossierEntry } from './companyDossier';
import { DossierSource } from './CompanyDossierView';
import { projectWork } from './workState';
import { WorkIndicator } from './Operational';
import { relTime } from './ui';

export type HeaderTarget = "brief" | "numbers" | "external" | "overview";

/** Shared company snapshot from existing records; no new model/search requests. */
export default function ProjectHeader({ detail, actions, status }: {
  detail: ProjectDetail; actions: ReactNode; status?: ReactNode; goto: (target: HeaderTarget) => void;
}) {
  const { project } = detail;
  const stage = (project.stage || "").split(/[（(]/)[0].trim();
  const facts = [...new Set([project.industry, project.location]
    .map((value) => value?.trim())
    .filter((value): value is string => Boolean(value) && !/^(?:未知|暂无|未确认|未披露|待确认|未提供|不详)/.test(value!)))];
  const legalName = project.legal_name && project.legal_name !== project.name ? project.legal_name : null;
  const website = safeWebsite(project.website);
  const intro = businessIntro(detail);
  const entries = dossierEntries(detail);
  const product = entries.find(e=>e.fact?.kind === 'product') ?? entries.find(e=>e.group === 'business');
  const person = entries.find(e=>e.group === 'team');
  const changes = headerChanges(detail);
  const [source,setSource] = useState<DossierEntry|null>(null);
  const work = projectWork(detail);
  useEffect(()=>setSource(null),[project.id]);
  const knownStage = stage && !/^(?:未知|暂无|未确认|未披露|待确认|未提供|不详)/.test(stage);

  return <header className="project-hero company-hero">
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
    <div className={`hero-body company-hero-body${changes.length ? ' has-changes' : ''}`}>
      <div className="hero-id">
        <h1>{project.name}</h1>
        {legalName && <p className="hero-legal">{legalName}</p>}
        {intro && <p className="company-hero-intro">{intro}</p>}
        {(facts.length > 0 || website) && <div className="hero-meta">
          {facts.map((fact) => <span key={fact}>{fact}</span>)}
          {website && <a href={website} target="_blank" rel="noreferrer noopener">公司官网 ↗</a>}
        </div>}
        {(product || person || knownStage) && <dl className="company-hero-facts">
          {([{entry:product,label:'业务与产品'},{entry:person,label:'核心团队'}]).map(({entry,label})=>entry && <div key={label}><dt>{label}</dt><dd><button onClick={()=>setSource(entry)} title={entry.text}>{entry.text}</button></dd>{entry.date && <small>{entry.dateLabel} {entry.date}</small>}</div>)}
          {knownStage && <div><dt>{stageLabel(project.stage)}</dt><dd><span title={project.stage || stage}>{stage}</span></dd></div>}
        </dl>}
      </div>
      {changes.length>0 && <section className="company-hero-changes" aria-label="近期记录"><h2>近期记录 <span>公司自身</span></h2>{changes.map(e=><button key={e.id} onClick={()=>setSource(e)}><span className="company-change-meta"><span>{e.label}</span><time>{e.dateLabel} {e.date}</time></span><strong>{e.text}</strong><small>{e.fact ? '公开来源' : '公司资料'} · 查看依据 ↗</small></button>)}</section>}
    </div>
    {status && <details className="company-hero-runtime"><summary><span className="company-runtime-label"><WorkIndicator state={work.state} label={work.label}/>{work.alongside && <WorkIndicator state={work.alongside.state} label={`另有 ${work.alongside.label}`}/>}</span><span className="company-runtime-meta">{project.last_external_at && <span>外部信息更新于 {relTime(project.last_external_at)}</span>}<span>运行详情 ⌄</span></span></summary><div className="hero-status">{status}</div></details>}
    <DossierSource entry={source} onClose={()=>setSource(null)}/>
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
