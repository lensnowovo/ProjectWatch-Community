import { motion } from "motion/react";
import type { ReactNode } from "react";
import CountUp from "./bits/CountUp";

export function Seg<T extends string | number>({ value, options, onChange, id }: {
  value: T; options: { value: T; label: ReactNode; disabled?: boolean; title?: string }[]; onChange: (v: T) => void; id: string;
}) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={String(o.value)} className={value === o.value ? "on" : ""} disabled={o.disabled} title={o.title}
          onClick={() => onChange(o.value)}>
          {value === o.value && <motion.span layoutId={`seg-${id}`} className="seg-pill" transition={{ type: "spring", stiffness: 520, damping: 40 }} />}
          <span className="seg-label">{o.label}</span>
        </button>
      ))}
    </div>
  );
}

export function Kpi({ label, value, suffix, tone }: {
  label: string; value: number; suffix?: string; tone?: "major" | "ok";
}) {
  return (
    <div className={`kpi ${tone ? `kpi-${tone}` : ""}`}>
      <div className="kpi-value">
        <CountUp to={value} duration={1.1} separator="," className="num-display" />
        {suffix && <span className="kpi-suffix">{suffix}</span>}
      </div>
      <div className="kpi-label">{label}</div>
    </div>
  );
}
