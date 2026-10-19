import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const entry = fileURLToPath(new URL("../src/headerPalette.ts", import.meta.url));
const component = new Module(entry);
component.paths = require.resolve.paths("react");
component._compile(buildSync({ entryPoints: [entry], bundle: true, write: false,
  platform: "node", format: "cjs", packages: "external",
}).outputFiles[0].text, entry);
const { readHeaderPalette } = component.exports;
const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const styles = block => {
  const tokens = Object.fromEntries([...block.matchAll(/(--(?:header|deep)-[\w-]+):\s*([^;]+);/g)].map(m => [m[1], m[2]]));
  return { getPropertyValue: key => tokens[key] ?? "" };
};

test("canvas palette has safe defaults before CSS is available", () => {
  const palette = readHeaderPalette(styles(""));
  assert.equal(palette.paperBase, "#f3eee4");
  assert.equal(palette.lightMode, "1");
  assert.ok(Object.values(palette).every(Boolean));
});

test("light and dark palettes update paper, flow and report colours together", () => {
  const light = readHeaderPalette(styles(css.match(/:root\s*\{([^}]+)\}/)[1]));
  const dark = readHeaderPalette(styles(css.match(/:root\[data-theme="dark"\]\s*\{([^}]+)\}/)[1]));
  assert.equal(light.lightMode, "1");
  assert.equal(dark.lightMode, "0");
  for (const key of Object.keys(light)) assert.notEqual(light[key], dark[key], key);
  assert.equal(dark.paperBase, "#282c32");
});

test("system dark preference uses the same palette as explicit dark", () => {
  const explicit = readHeaderPalette(styles(css.match(/:root\[data-theme="dark"\]\s*\{([^}]+)\}/)[1]));
  const system = readHeaderPalette(styles(css.match(/:root:not\(\[data-theme="light"\]\)\s*\{([^}]+)\}/)[1]));
  assert.deepEqual(system, explicit);
});
