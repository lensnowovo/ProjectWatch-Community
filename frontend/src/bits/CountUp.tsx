import { useReducedMotion } from "motion/react";
import { useEffect, useRef } from "react";

interface CountUpProps {
  to: number;
  from?: number;
  direction?: "up" | "down";
  delay?: number;
  duration?: number;
  className?: string;
  startWhen?: boolean;
  separator?: string;
  onStart?: () => void;
  onEnd?: () => void;
}

/** Adapted from React Bits CountUp, driven by requestAnimationFrame rather than a spring:
 * the spring version never settled in the embedded browser and left every figure at its start value. */
export default function CountUp({
  to,
  from = 0,
  direction = "up",
  delay = 0,
  duration = 2,
  className = "",
  startWhen = true,
  separator = "",
  onStart,
  onEnd,
}: CountUpProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const still = useReducedMotion();
  const start = direction === "down" ? to : from;
  const end = direction === "down" ? from : to;

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const decimals = Math.max(places(from), places(to));
    const show = (v: number) => {
      const text = new Intl.NumberFormat("en-US", {
        useGrouping: !!separator, minimumFractionDigits: decimals, maximumFractionDigits: decimals,
      }).format(v);
      node.textContent = separator ? text.replace(/,/g, separator) : text;
    };
    if (!startWhen || still) { show(end); onEnd?.(); return; }
    show(start);
    let frame = 0, began = false;
    const begin = window.setTimeout(() => {
      onStart?.();
      began = true;
      const t0 = performance.now(), ms = Math.max(1, duration * 1000);
      const step = (now: number) => {
        const p = Math.min(1, (now - t0) / ms);
        show(start + (end - start) * (1 - Math.pow(1 - p, 3)));
        if (p < 1) frame = requestAnimationFrame(step); else onEnd?.();
      };
      frame = requestAnimationFrame(step);
    }, delay * 1000);
    // If frames never arrive (a hidden or throttled pane), still land on the real number.
    const settle = window.setTimeout(() => { if (!began || frame) show(end); }, (delay + duration) * 1000 + 300);
    return () => { clearTimeout(begin); clearTimeout(settle); cancelAnimationFrame(frame); };
  }, [start, end, from, to, delay, duration, separator, startWhen, still]); // eslint-disable-line react-hooks/exhaustive-deps

  return <span className={className} ref={ref} />;
}

function places(num: number): number {
  const str = num.toString();
  if (!str.includes(".")) return 0;
  const decimals = str.split(".")[1];
  return parseInt(decimals) !== 0 ? decimals.length : 0;
}
