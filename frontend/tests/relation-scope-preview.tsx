// Synthetic fixture; no model/search calls, no connection to a running project.
import React from "react";
import { createRoot } from "react-dom/client";
import { RelationScope } from "../src/relationScope";
import { BrowserRouter } from "react-router-dom";
import { PlanView } from "../src/plan";
import { api } from "../src/api";
import "../src/styles.css";
const base = { project_id: "synthetic", status: "active", queries: [], sources: "https://example.com",
  tier: "background", cadence: "low", section: "product", view: "evidence", objects: [], scope_kind: "relation" };
const items = [
  { ...base, id: "product", relation_type: "product", relation_target: "SYN-ONE", relation_aliases: ["合成手部运动辅助系统"],
    reason: "公司开发的核心产品，持续跟踪注册、临床与商业化进展。" },
  { ...base, id: "tech", relation_type: "technology", relation_target: "合成光学路线",
    reason: "该产品采用这一技术路线，同路线的临床结果与工艺变化影响产品竞争位置。" },
  { ...base, id: "disease", relation_type: "indication", relation_target: "合成适应症",
    reason: "现有产品面向这一患者群体，关注治疗方式与临床指南的变化。" },
  { ...base, id: "q1", parent_id: "product", scope_kind: "question", text: "产品是否获批进入临床应用？" },
  { ...base, id: "q2", parent_id: "product", scope_kind: "question", text: "是否出现新的商业交付？" },
  { ...base, id: "q3", parent_id: "tech", scope_kind: "question", text: "同路线是否出现临床效果新证据？" },
];
const updates = { product: { count: 4, items: [{ event_id: "e1", title: "合成公司公布手部运动辅助系统注册进展",
  url: "https://example.com/event", date: "2026-10-20", source: "合成公告", basis: "full_body",
  matched_term: "合成手部运动辅助系统", excerpt: "合成手部运动辅助系统已完成本轮注册申报，后续以审批公告为准。本段为合成测试文字。" }] } };
const explorations = { q1: { state: "stopped", label: "本次问题已回答" }, q2: { state: "waiting", label: "等待新证据" },
  q3: { state: "active", label: "新证据唤醒" } };
const full = new URLSearchParams(location.search).has("full");
for (const key of Object.keys(api)) api[key] = async () => { throw new Error("Preview blocks actual API calls"); };
api.monitoringPlan = async () => ({ items, relation_updates: updates, explorations, changes: [], proposals: [],
  modules: {}, module_labels: {}, templates: [], latest: {}, meta: { templates: [] },
  framework: [{ key: "product", label: "资产与产品", ring: "company" }], rings: [],
  labels: { origin: { system: "系统" }, tier: { background: "背景", topic: "专题" }, cadence: { low: "低频" } },
  coverage: {}, state: { phase: "growing", label: "生长中", revisions: 1 } });
api.signals = async () => ({ cadence: { hours: 12 } });
const detail = { project: { id: "synthetic", name: "合成技术", status: "active", execution_mode: "api", activated: true },
  jobs: [], documents: [], counts: { company: 4 }, observations: { items: [] }, event_states: { items: [] } };
createRoot(document.getElementById("root")!).render(<BrowserRouter><div className="page page-project" style={{ margin: "30px auto", maxWidth: 1250 }}>
  <header><p className="muted small">合成数据 · 不连接当前实例</p><h1>合成技术</h1></header>
  <div className="tabs-row"><nav className="tabs"><button>当前判断</button><button>项目总览</button>
    <button>内部信息</button><button>外部信息</button><button className="active">监控范围</button></nav></div>
  {full ? <PlanView projectId="synthetic" company="合成技术" detail={detail} /> : <RelationScope items={items} updates={updates} explorations={explorations}
    renderState={() => <span style={{ color: "var(--ok)" }}>● 持续关注</span>}
    renderDetail={(i) => <div><strong>{i.text || i.relation_target}</strong><p>{i.reason || "研究结束后，长期关联仍保持监控。"}</p>
      <a href="https://example.com" target="_blank" rel="noreferrer">纳入依据 ↗</a></div>} />}
</div></BrowserRouter>);
