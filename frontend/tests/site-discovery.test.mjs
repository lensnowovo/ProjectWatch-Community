import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

test("官网监测 explains an automatically found site and what happened when none was found", () => {
  const monitor = fs.readFileSync(new URL("../src/monitor.tsx", import.meta.url), "utf8");
  assert.match(monitor, /<SiteOrigin website=\{website\} pages=\{state\.pages\} found=\{state\.site_discovery\} \/>/);
  for (const text of ["官网为系统自动找到", "没有找到能确认的官网", "未配置检索服务", "不再自动寻找", "首轮更新时会自动搜索"]) {
    assert.match(monitor, new RegExp(text));
  }
  const api = fs.readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
  assert.match(api, /site_discovery\?: SiteDiscovery/);
});
