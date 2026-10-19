import { useEffect, useState } from "react";

const defaults = {
  paperBase: "#f3eee4", paper1: "#eae2d4", paper2: "#faf6ef", paper3: "#ddd4c8",
  flow1: "#768c9f", flow2: "#d3dbe0", flow3: "#8c9bac", lightMode: "1",
  grain1: "#d8c8ad", grain2: "#f1eade", grain3: "#a8b2bc",
  deep1: "#a9bccf", deep2: "#e6d6b0", deep3: "#8fa7c0", deepLight: "1",
};
export type HeaderPalette = typeof defaults;
const tokens: Record<keyof HeaderPalette, string> = {
  paperBase: "--header-paper-base", paper1: "--header-paper-1", paper2: "--header-paper-2", paper3: "--header-paper-3",
  flow1: "--header-flow-1", flow2: "--header-flow-2", flow3: "--header-flow-3", lightMode: "--header-light-mode",
  grain1: "--header-grain-1", grain2: "--header-grain-2", grain3: "--header-grain-3",
  deep1: "--deep-flow-1", deep2: "--deep-flow-2", deep3: "--deep-flow-3", deepLight: "--deep-light-mode",
};

export function readHeaderPalette(styles: Pick<CSSStyleDeclaration, "getPropertyValue">): HeaderPalette {
  return Object.fromEntries(Object.entries(tokens).map(([key, token]) =>
    [key, styles.getPropertyValue(token).trim() || defaults[key as keyof HeaderPalette]])) as HeaderPalette;
}

// Canvas/WebGL cannot consume CSS variables directly. Observe the same root
// attribute and system preference as the rest of the UI, not a second theme store.
export default function useHeaderPalette() {
  const read = () => readHeaderPalette(getComputedStyle(document.documentElement));
  const [palette, setPalette] = useState<HeaderPalette>(() => typeof document === "undefined" ? defaults : read());
  useEffect(() => {
    const sync = () => setPalette(previous => {
      const next = read();
      return Object.keys(tokens).every(key => previous[key as keyof HeaderPalette] === next[key as keyof HeaderPalette]) ? previous : next;
    });
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const system = window.matchMedia("(prefers-color-scheme: dark)");
    system.addEventListener("change", sync);
    sync();
    return () => { observer.disconnect(); system.removeEventListener("change", sync); };
  }, []);
  return palette;
}
