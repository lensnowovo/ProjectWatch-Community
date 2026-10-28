import { useId, useMemo, useState } from "react";
import type { MonitoringPlanView } from "./api";
import { monitorReachModel } from "./monitorReachModel";
import "./monitorReach.css";

export function MonitorReach({ company, plan, paused = false }: {
  company: string; plan: MonitoringPlanView; paused?: boolean;
}) {
  const model = useMemo(() => monitorReachModel(plan), [plan]);
  const uid = useId().replace(/:/g, "");
  const [hovered, setHovered] = useState<string | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  const [hoveredTier, setHoveredTier] = useState<number | null>(null);
  const active = model.rows.find(r => r.id === (hovered ?? focused));
  const count = Math.max(3, model.depth);
  // Keep unusually deep saved chains readable without inventing a depth ceiling.
  const tiers = count <= 6 ? Array.from({ length: count }, (_, i) => i + 1) : [1, 2, 3, count];
  const step = 260;
  const x = (index: number) => 300 + index * step, width = x(tiers.length - 1) + 160;
  const endX = (depth: number) => {
    const index = tiers.indexOf(depth);
    return index >= 0 ? x(index) : x(2) + (depth - 3) / (count - 3) * step;
  };
  const summary = model.rows.map(r => r.label + (r.depth ? "已至第" + r.depth + "层" : "尚未延伸")).join("；");
  return <section className="monitor-reach" aria-label="监控穿透概览">
    <div className="mr-viewport" tabIndex={0} role="region" aria-label="四个方向的关联深度，窄屏可横向滚动">
      <div className="mr-canvas" style={{ minWidth: width }}>
        <svg className="mr-tier-backgrounds" viewBox={`0 0 ${width} 1`} preserveAspectRatio="none" aria-hidden="true">
          {tiers.map((tier, i) => <g key={tier}>
            <rect className="mr-tier-band" x={x(i) - step / 2} y="0" width={i === tiers.length - 1 ? width - x(i) + step / 2 : step} height="1" opacity={Math.max(.022, .08 * .55 ** i)} />
            <rect className="mr-panel-hover" x={x(i) - step / 2} y="0" width={i === tiers.length - 1 ? width - x(i) + step / 2 : step} height="1" opacity={hoveredTier === tier ? .025 : 0} />
            <path className="mr-panel-divider" d={`M ${x(i) - step / 2} 0 V 1`} vectorEffect="non-scaling-stroke" />
          </g>)}
        </svg>
        <header><h3>监控穿透概览</h3><span className="mr-caption" data-active={!!active}>{paused && "已暂停 · "}{active ? active.label + (active.depth ? " · 已至第 " + active.depth + " 层" : " · 尚未建立关联层级") : "各方向最深层级"}<span aria-hidden="true"> · </span>不表示全面覆盖</span></header>
      <svg className="mr-map" viewBox={`0 0 ${width} 134`} role="group" aria-labelledby={`${uid}-title ${uid}-description`} data-inspecting={!!active}>
        <title id={`${uid}-title`}>{summary}</title>
        <desc id={`${uid}-description`}>根据已保存的关联父子关系计算各方向最深层级，不表示全面覆盖、研究完成或任务正在运行。</desc>
        <defs>
          <radialGradient id={`${uid}-glow`}><stop className="mr-glow" stopOpacity=".18" /><stop className="mr-glow" offset="1" stopOpacity="0" /></radialGradient>
        </defs>
        <g aria-hidden="true">
          {tiers.map((tier, i) => <rect key={tier} className="mr-tier-hit" x={x(i) - step / 2} y="0" width={step} height="134"
            onMouseEnter={() => setHoveredTier(tier)} onMouseLeave={() => setHoveredTier(null)} />)}
        </g>
        {tiers.map((tier, i) => <g key={tier} className="mr-tier" aria-hidden="true">
          <text x={x(i)} y="12" textAnchor="middle">第 {tier} 层</text>
          {i > 0 && tier > tiers[i - 1] + 1 && <text x={x(i) - step / 2} y="12" textAnchor="middle">…</text>}
        </g>)}
        <g className="mr-origin" aria-hidden="true"><circle cx="80" cy="75" r="30" fill={`url(#${uid}-glow)`} /><circle className="mr-core" cx="80" cy="75" r="5" />
          <text x="80" y="100" textAnchor="middle">{company.length > 9 ? company.slice(0, 8) + "…" : company || "公司"}</text></g>
        {model.rows.map((r, i) => {
          // Metro-map rows: the fan ends at the direction name, the line resumes after it,
          // so a name never sits on a line or under a tier caption.
          const y = 36 + i * 26, labelX = 198, labelEnd = labelX + r.label.length * 13 + 10;
          const fan = `M 91 75 C 140 75, 146 ${y}, ${labelX - 10} ${y}`;
          const stem = `${fan} M ${labelEnd} ${y}`;
          const label = r.label + (r.depth ? " · 已至第 " + r.depth + " 层" : " · 尚未建立关联层级");
          return <g key={r.id} className="mr-trunk" data-direction={r.id} data-depth={r.depth} data-active={active?.id === r.id}
            tabIndex={0} role="img" aria-label={label}
            onMouseEnter={() => setHovered(r.id)} onMouseLeave={() => setHovered(null)}
            onFocus={() => setFocused(r.id)} onBlur={() => setFocused(null)}>
            <title>{label + "。只表示最深关联，不表示全面覆盖。"}</title>
            <path className="mr-hit" d={stem + ` H ${width - 12}`} />
            <rect className="mr-focus-ring" x={labelX - 14} y={y - 12} width={width - labelX + 2} height="24" rx="12" />
            <path className="mr-base" d={stem + ` H ${x(tiers.length - 1)}`} />
            {!!r.depth && <path className="mr-lit" d={stem + ` H ${endX(r.depth)}`} />}
            <text className="mr-direction" x={labelX} y={y + 4.5}>{r.label}</text>
            {r.depth > 0 && <circle className="mr-front" cx={endX(r.depth)} cy={y} r="10" />}
            {tiers.map((tier, j) => <circle key={tier} className={tier <= r.depth ? "mr-node reached" : "mr-node"} cx={x(j)} cy={y} r={tier === r.depth ? 5 : 3.5} />)}
            {r.depth > 0 && !tiers.includes(r.depth) && <circle className="mr-node reached" cx={endX(r.depth)} cy={y} r="5" />}
            <text className={r.depth ? "mr-arrival" : "mr-empty"} x={width - 26} y={y + 4} textAnchor="end">{r.depth ? "已至第 " + r.depth + " 层" : "尚未延伸"}</text>
          </g>;
        })}
      </svg>
      </div>
    </div>
    {model.excluded > 0 && <p className="mr-note">部分关联的来源链或分类不完整，暂未计入层级；原条目仍保留在下方表格。</p>}
    {!model.depth && !model.excluded && <p className="mr-note">尚未建立关联层级，常规采集与原有关注点仍保留在下方。</p>}
  </section>;
}
