import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

test("task board is reachable from the sidebar and keeps only work in progress open", () => {
  const app = fs.readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /<NavItem to="\/tasks" label="任务">/);
  assert.match(app, /path="\/tasks"/);
  const page = fs.readFileSync(new URL("../src/pages/Tasks.tsx", import.meta.url), "utf8");
  for (const text of ["正在处理", "运行异常 · 待确认", "已有结果 · 可选查看", "处理记录", "现在没有在处理的任务", "后台任务未运行"]) assert.match(page, new RegExp(text));
  assert.match(page, /<details className="task-history">/);  // history folded by default (no open attribute)
  assert.doesNotMatch(page, /task-kpis/);
  assert.match(page, /api\.tasks/);
});

test("每条需要处理 explains what happened and what to do, with the button that does it", () => {
  const page = fs.readFileSync(new URL("../src/pages/Tasks.tsx", import.meta.url), "utf8");
  for (const text of ["发生了什么", "怎么处理", "查看并重发", "去设置测试连接", "重新运行", "收起提醒"]) assert.match(page, new RegExp(text));
  assert.match(page, /<RecoveryDialog/);
  assert.match(page, /unresolvedJobs\(jobs\)/);
  assert.match(fs.readFileSync(new URL("../src/workState.ts", import.meta.url), "utf8"), /!j\.dismissed/);
  assert.doesNotMatch(page, /去处理 →/);
});
