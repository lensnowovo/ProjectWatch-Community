import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const lock = JSON.parse(readFileSync(new URL("../package-lock.json", import.meta.url), "utf8"));
// Advisory floors at the 2026-10-18 audit, not a replacement for a fresh registry audit.
const floors = { vite: "6.4.3", esbuild: "0.25.0", "react-router": "7.18.0", "react-router-dom": "7.18.0" };
for (const [name, floor] of Object.entries(floors)) {
  test(`${name}: every locked copy meets the reviewed security floor`, () => {
    const copies = Object.entries(lock.packages).filter(([path]) => path.endsWith(`node_modules/${name}`));
    assert.ok(copies.length, `${name} missing from lock`);
    for (const [path, entry] of copies) {
      assert.match(entry.version, /^\d+\.\d+\.\d+$/, `${path} must be a stable version`);
      const actual = entry.version.split(".").map(Number);
      const minimum = floor.split(".").map(Number);
      // A newer major can still predate its own security fix (e.g. Vite 7.0).
      // Moving off the reviewed branch requires revisiting this audit floor.
      assert.equal(actual[0], minimum[0], `${name}: unreviewed major release`);
      const firstDifference = actual.findIndex((n, i) => n !== minimum[i]);
      assert.ok(firstDifference < 0 || actual[firstDifference] > minimum[firstDifference], `${path}@${entry.version} < ${floor}`);
    }
  });
}
