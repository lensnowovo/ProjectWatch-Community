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
