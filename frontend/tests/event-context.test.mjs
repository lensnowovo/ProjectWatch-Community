import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("every news row and each top change can open why it matters", () => {
  assert.match(read("../src/items.tsx"), /<EventContextDrawer projectId=\{item\.project_id\}[^>]*eventId=\{item\.id\}/);
  const overview = read("../src/overview.tsx");
  assert.match(overview, /c\.origin === "公开" && <button className="item-why"/);  // internal changes have no event to explain
  assert.match(read("../src/api.ts"), /\/api\/studio\/projects\/\$\{projectId\}\/events\/\$\{encodeURIComponent\(eventId\)\}\/context/);
});

test("the drawer reads in order: source excerpt → why it is watched → what judgment it moved", () => {
  const ec = read("../src/eventContext.tsx");
  const order = ["来源节选", "为什么在监控范围内", "它改变了什么判断"].map((t) => ec.indexOf(t));
  assert.ok(order.every((i) => i > 0) && order[0] < order[1] && order[1] < order[2]);
  assert.match(ec, /没有命中具体关注点：它来自常规采集/);  // honest when nothing in the plan matched
  assert.match(ec, /还没有问题判断或 AI 判断引用这条动态/);
  assert.match(ec, /\{open && <EventContextBody/);  // nothing is fetched until the drawer opens
});
