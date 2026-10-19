// Adapted from React Bits BlurText (MIT + Commons Clause); see LICENSE-react-bits.md.
// Inline semantics, SSR-visible text and reduced-motion support for report headings.
import { motion, useReducedMotion } from "motion/react";
import { useEffect, useState } from "react";

export default function BlurText({ text, direction = "bottom", delay = 35 }: {
  text: string;
  direction?: "top" | "bottom" | "left" | "fade";
  delay?: number;
}) {
  const [mounted, setMounted] = useState(false);
  const reduced = useReducedMotion();
  useEffect(() => setMounted(true), []);
  const offset = direction === "top" ? -8 : direction === "bottom" ? 8 : 0;
  const move = direction === "left" ? -8 : 0;
  return (
    <span className="header-blur-text" aria-label={text}>
      {Array.from(text).map((letter, index) => (
        <motion.span key={index} aria-hidden="true" initial={false}
          animate={mounted && !reduced ? {
            filter: ["blur(5px)", "blur(0px)"], opacity: [.45, 1], y: [offset, 0], x: [move, 0],
          } : { filter: "blur(0px)", opacity: 1, y: 0, x: 0 }}
          transition={{ duration: .48, delay: reduced ? 0 : index * delay / 1000, ease: [.22, 1, .36, 1] }}>
          {letter === " " ? "\u00a0" : letter}
        </motion.span>
      ))}
    </span>
  );
}
