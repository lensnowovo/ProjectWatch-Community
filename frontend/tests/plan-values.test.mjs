import assert from "node:assert/strict";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const entry = fileURLToPath(new URL("../src/planValues.ts", import.meta.url));
const mod = new Module(entry);
mod._compile(buildSync({ entryPoints: [entry], bundle: true, write: false, platform: "node", format: "cjs" }).outputFiles[0].text, entry);
const { parseMetricValue } = mod.exports;

test("missing and malformed values never become zero", () => {
  for (const value of ["", "  ", ",", "1,2", "NaN", "Infinity", "0x10", "暂无", "1e9999"]) {
    assert.equal(parseMetricValue(value), null, value);
  }
});

test("explicit zero, signed decimals and thousands separators remain usable", () => {
  for (const [raw, expected] of [["0", 0], [" 0.00 ", 0], ["-12.5", -12.5], ["+2", 2], ["1,234.56", 1234.56]]) {
    assert.equal(parseMetricValue(raw), expected);
  }
});
