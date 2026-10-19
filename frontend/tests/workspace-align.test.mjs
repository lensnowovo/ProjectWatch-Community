import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

test("portfolio cards keep one rhythm, so a row never ends at two depths", () => {
  const view = read("../src/pages/Projects.tsx");
  const css = read("../src/styles.css");
  // The animated wrapper is the grid item; the card has to fill it.
  assert.match(view, /className="pc-wrap"/);
  assert.match(css, /\.pc-wrap \{ display: flex;/);
  assert.match(css, /\.project-card \{[^}]*flex: 1;/);
  // Each row of the card is one line deep and the chips row keeps its place when empty.
  assert.match(view, /<div className="pc-signals">\{signals\[p\.id\] && <SignalChips/);
  assert.match(css, /\.pc-signals \{ min-height: \d+px;/);
  assert.match(css, /\.pc-headline \{[^}]*-webkit-line-clamp: 3;/);
  assert.match(css, /\.pc-when \{ white-space: nowrap;/);
  // The table's company cell holds a name and one line of context.
  assert.match(css, /\.portfolio td:first-child \.cell-title, \.portfolio td:first-child \.small \{\s*overflow: hidden; text-overflow: ellipsis; white-space: nowrap;/);
});

test("the feed figure band stands on the same gridlines as the columns under it", () => {
  const view = read("../src/pages/Feed.tsx");
  const css = read("../src/styles.css");
  assert.match(view, /className="kpis kpis-split"/);
  // Three figures over the list, 待核对 / 异常 over the 需要你处理 card it describes.
  assert.match(view, /未读信息[\s\S]{0,120}待核对 \/ 异常/);
  assert.match(css, /\.kpis-split \{ grid-template-columns: repeat\(3, minmax\(0, 2\.6667fr\)\) var\(--s7\) minmax\(0, 4fr\); \}/);
  assert.match(css, /\.tf-cols \{ display: grid; grid-template-columns: minmax\(0, 8fr\) minmax\(0, 4fr\); gap: var\(--s7\)/);
  // Band and columns stop splitting at the same width.
  assert.match(css, /@media \(max-width: 1080px\) \{\s*\.tf-cols \{ grid-template-columns: minmax\(0, 1fr\);/);
  assert.match(css, /@media \(max-width: 1080px\)[\s\S]*?\.kpis-split \{ grid-template-columns: repeat\(2, minmax\(0, 1fr\)\); \}/);
});

test("filter rows read as one band of equal controls", () => {
  const css = read("../src/styles.css");
  assert.match(css, /\.filters \{[^}]*--control-h: 32px;/);
  assert.match(css, /\.filters > \.seg, \.filters > select, \.filters > input \{ box-sizing: border-box; height: var\(--control-h\); \}/);
  // Whatever follows the spacer stays at the right edge even after the row wraps.
  assert.match(css, /\.filters > \.spacer \+ \* \{ margin-left: auto; \}/);
});

test("figures count up on frames, not on a spring that never settles", () => {
  const src = read("../src/bits/CountUp.tsx");
  assert.match(src, /requestAnimationFrame/);
  assert.doesNotMatch(src, /useSpring/);
  assert.match(src, /useReducedMotion/);
  assert.match(src, /still land on the real number/);
});
