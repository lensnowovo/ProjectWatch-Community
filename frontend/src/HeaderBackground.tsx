import { useReducedMotion } from "motion/react";
import Aurora from "./bits/Aurora";
import ColorBends from "./bits/ColorBends";
import Grainient from "./bits/Grainient";
import useHeaderPalette from "./headerPalette";

// Real React Bits Backgrounds, constrained to the cover, behind its content.
// Static CSS fallbacks preserve readability with reduced motion / unavailable GPU.
export default function HeaderBackground({ variant }: { variant: "feed" | "portfolio" | "digest" }) {
  const reduced = useReducedMotion();
  const palette = useHeaderPalette();
  return <div className={`header-background background-${variant}`} aria-hidden="true">
    {!reduced && variant === "feed" && <Aurora colorStops={[palette.flow1, palette.flow2, palette.flow3]}
      amplitude={.75} blend={.65} speed={.25} lightMode={palette.lightMode === "1"} />}
    {!reduced && variant === "portfolio" && <ColorBends baseColor={palette.paperBase}
      colors={[palette.paper1, palette.paper2, palette.paper3]} />}
    {!reduced && variant === "digest" && <Grainient color1={palette.grain1} color2={palette.grain2} color3={palette.grain3}
      timeSpeed={.18} warpStrength={1.8} rotationAmount={200} contrast={1} grainAmount={.025}
      grainAnimated={false} saturation={.65} blendSoftness={.25} />}
  </div>;
}
