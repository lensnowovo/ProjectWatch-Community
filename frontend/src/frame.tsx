import { useState, type ReactNode } from "react";

/** One page grammar for every project tab: a conclusion band, a main column with short side notes, and a
 * folded reference area. The band sits at the same place and size on every tab, so the tabs read as one product. */

export function VerdictBand({ eyebrow, title, meta, figures, children }: {
  eyebrow?: ReactNode; title: ReactNode; meta?: ReactNode; figures?: ReactNode; children?: ReactNode;
}) {
  return (
    <section className="tf-band">
      <div className="tf-band-top">
        {eyebrow && <div className="tf-eyebrow">{eyebrow}</div>}
        {meta && <div className="tf-meta">{meta}</div>}
      </div>
      <h2 className="tf-title">{title}</h2>
      {children}
      {figures && <div className="tf-figures">{figures}</div>}
    </section>
  );
}

export function Figure({ label, value, unit, note, tone }: { label: string; value: ReactNode; unit?: string; note?: ReactNode; tone?: "loss" | "warn" | "up" }) {
  return (
    <div className={`tf-fig${tone ? ` tf-fig-${tone}` : ""}`}>
      <span className="tf-fig-label">{label}</span>
      <span className="tf-fig-value"><span className="num-display">{value}</span>{unit && <span className="tf-fig-unit">{unit}</span>}</span>
      {note && <span className="tf-fig-note">{note}</span>}
    </div>
  );
}

/** Main column (8/12) and side notes (4/12). Side notes must stay shorter than the main column. */
export function Columns({ main, side }: { main: ReactNode; side?: ReactNode }) {
  return <div className={`tf-cols${side ? "" : " tf-cols-single"}`}><div className="tf-main">{main}</div>{side && <aside className="tf-side">{side}</aside>}</div>;
}

/** A titled block. `card` adds a theme-aware paper surface for a table, chart or judgment. */
export function Block({ title, note, action, children, className = "", label, card = false }: {
  title: ReactNode; note?: ReactNode; action?: ReactNode; children: ReactNode; className?: string; label?: string; card?: boolean;
}) {
  return (
    <section className={`tf-block${card ? " tf-block-card" : ""} ${className}`} aria-label={label}>
      <div className="tf-block-head"><h3>{title}</h3>{note && <span className="tf-note">{note}</span>}{action && <span className="tf-action">{action}</span>}</div>
      {children}
    </section>
  );
}

/** Reference area: everything a reader may want to check, folded by default. */
export function Reference({ items }: { items: { key: string; title: string; note?: string; body: ReactNode; open?: boolean }[] }) {
  const shown = items.filter((i) => i.body);
  if (!shown.length) return null;
  return (
    <section className="tf-ref" aria-label="参考与依据">
      <h3 className="tf-ref-title">参考与依据</h3>
      {shown.map(({ key, ...i }) => <RefItem key={key} {...i} />)}
    </section>
  );
}

function RefItem({ title, note, body, open: initial = false }: { title: string; note?: string; body: ReactNode; open?: boolean }) {
  const [open, setOpen] = useState(initial);
  return (
    <div className={`tf-ref-item${open ? " open" : ""}`}>
      <button className="tf-ref-head" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="tf-ref-caret" aria-hidden="true">›</span>{title}{note && <span className="tf-note">{note}</span>}
      </button>
      {open && <div className="tf-ref-body">{body}</div>}
    </div>
  );
}

/** Layout-shaped placeholder while a block loads: no spinner, no jump when the content arrives. */
export function Skeleton({ lines = 3, className = "" }: { lines?: number; className?: string }) {
  return <div className={`tf-skel ${className}`} aria-hidden="true">{Array.from({ length: lines }, (_, i) => <span key={i} style={{ width: `${92 - i * 14}%` }} />)}</div>;
}

/** A whole tab while it loads: the band (title + four figures), then the main column and side notes. */
export function TabSkeleton({ label = "正在读取" }: { label?: string }) {
  return (
    <div className="tab-frame tf-skeleton" role="status" aria-label={label}>
      <section className="tf-band"><Skeleton lines={1} className="tf-skel-eyebrow" /><Skeleton lines={2} className="tf-skel-title" />
        <div className="tf-figures">{[0, 1, 2, 3].map((i) => <Skeleton key={i} lines={2} className="tf-skel-fig" />)}</div></section>
      <div className="tf-cols"><div className="tf-main"><Skeleton lines={5} /><Skeleton lines={4} /></div><aside className="tf-side"><Skeleton lines={3} /></aside></div>
    </div>
  );
}
