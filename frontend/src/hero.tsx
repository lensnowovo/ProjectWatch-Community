import { useEffect, useMemo, useState, type ReactNode } from "react";
import Waves from "./bits/Waves";

/** Ambient engraved-line background for the permanent cover: flows on its own, ignores the cursor. */
export function HeroWaves() {
  return (
    <Waves
      className="hero-waves"
      lineColor="var(--wave)"
      interactive={false}
      flow={1.6}
      waveSpeedX={0.022}
      waveSpeedY={0.011}
      waveAmpX={36}
      waveAmpY={18}
      xGap={12}
      yGap={36}
      maxFps={40}
    />
  );
}

/** FNV-1a: a stable 32-bit hash so a project always gets the same line signature. */
export function hashString(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

function signature(seed: string) {
  const h = hashString(seed || "portfolio");
  const pick = (shift: number, min: number, max: number) => min + (((h >>> shift) & 0xff) / 255) * (max - min);
  return {
    noiseSeed: (h % 65000) + 300,
    xGap: Math.round(pick(0, 9, 15)),
    yGap: Math.round(pick(8, 26, 42)),
    ampX: pick(16, 22, 42),
    ampY: pick(24, 10, 22),
    angle: pick(4, -9, 9),
    speedX: pick(12, 0.012, 0.026),
    speedY: pick(20, 0.005, 0.013),
  };
}

const TONE_VAR: Record<string, string> = {
  positive: "var(--wave-positive)", watch: "var(--wave-watch)", negative: "var(--wave-negative)",
};

function useReducedMotion() {
  const [reduced, setReduced] = useState(() =>
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

/**
 * The line field as a page signature.
 * - seed: each project draws its own, stable pattern (direction, density, rhythm);
 * - tone: a faint tint following the latest judgement (向好 / 需关注 / 承压);
 * - activity 0–1: how much happened recently — a quiet company's lines barely move;
 * - busy: background work in progress — the field speeds up, then settles when done.
 */
export function SignatureBand({ seed, tone, activity = 0, busy = false, children, className = "" }: {
  seed: string; tone?: string; activity?: number; busy?: boolean; children?: ReactNode; className?: string;
}) {
  const sig = useMemo(() => signature(seed), [seed]);
  const reduced = useReducedMotion();
  const level = Math.max(0, Math.min(1, activity));
  const energy = busy ? 3.2 : 0.12 + level * 0.9;
  return (
    <div className={`sig-band ${busy ? "sig-busy" : ""} ${className}`} data-tone={tone || "neutral"}>
      <div className="sig-field" aria-hidden ref={(el) => el?.style.setProperty("--sig-angle", `${sig.angle}deg`)}>
        <Waves
          key={seed}
          lineColor={TONE_VAR[tone ?? ""] ?? "var(--wave)"}
          interactive={false}
          seed={sig.noiseSeed}
          energy={reduced ? 0 : energy}
          flow={0.5 + level * 1.4}
          waveSpeedX={sig.speedX}
          waveSpeedY={sig.speedY}
          waveAmpX={sig.ampX}
          waveAmpY={sig.ampY}
          xGap={sig.xGap}
          yGap={sig.yGap}
          maxFps={30}
        />
      </div>
      <div className="sig-content">{children}</div>
    </div>
  );
}

/** Map recent major/notable counts to 0–1 (diminishing: a handful of majors already reads as "busy"). */
export function activityLevel(major = 0, notable = 0): number {
  return 1 - Math.exp(-(major * 0.5 + notable * 0.12));
}
