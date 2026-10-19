import { cloneElement, useEffect, useId, useLayoutEffect, useRef, useState, type ReactElement } from "react";
import { createPortal } from "react-dom";
const useHintLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

/** No layout wrapper: works on HTML bars and SVG marks without changing chart geometry. */
export function ChartHint({ title, value, meta, note, children }: {
  title: string; value: string; meta?: string; note?: string; children: ReactElement;
}) {
  const id = useId();
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const tip = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const clear = () => { if (timer.current) clearTimeout(timer.current); };
  const close = () => { clear(); setAnchor(null); };
  const leave = () => { clear(); timer.current = setTimeout(() => setAnchor(null), 120); };
  const show = (e: { currentTarget: Element }) => { clear(); setAnchor(e.currentTarget.getBoundingClientRect()); };
  useEffect(() => () => clear(), []);
  useEffect(() => {
    if (!anchor) return;
    const escape = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", escape);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => { window.removeEventListener("keydown", escape); window.removeEventListener("scroll", close, true); window.removeEventListener("resize", close); };
  }, [anchor]);
  useHintLayoutEffect(() => {
    if (!anchor || !tip.current) return;
    const { width, height } = tip.current.getBoundingClientRect();
    setPosition({ left: Math.max(12, Math.min(anchor.left + anchor.width / 2 - width / 2, window.innerWidth - width - 12)),
      top: Math.max(12, Math.min(anchor.top >= height + 12 ? anchor.top - height - 8 : anchor.bottom + 8, window.innerHeight - height - 12)) });
  }, [anchor]);
  return <>{cloneElement(children, {
    tabIndex: 0, "data-chart-hint": "", "aria-label": [title, value, meta, note].filter(Boolean).join("；"),
    "aria-describedby": anchor ? id : undefined, title: undefined,
    onMouseEnter: show, onMouseLeave: leave, onFocus: show, onBlur: close,
    onClick: show,
  })}{anchor && createPortal(<div ref={tip} id={id} role="tooltip" className="chart-hint" style={position} onMouseEnter={clear} onMouseLeave={leave}>
    <span>{title}</span><strong>{value}</strong>{meta && <small>{meta}</small>}{note && <p>{note}</p>}
  </div>, document.body)}</>;
}
