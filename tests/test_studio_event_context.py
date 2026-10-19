"""Event detail chain: source excerpt → plan point it hit → assessments and AI judgment points citing it. Synthetic."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient


def _event(store, project_id, title, snippet):
    store.add_items(project_id, "company", [{"key": title, "title": title, "url": f"https://n.example/{title}",
                                             "source": "示例来源", "snippet": snippet, "published_at": "2026-10-14"}])
    item = next(i for i in store.untriaged(project_id) if i["title"] == title)
    store.save_triage([{"id": item["id"], "relevant": True, "importance": "notable", "summary": title}])
    event_id = store.create_event(project_id, {"title": title, "category": "company", "importance": "notable", "summary": title,
                                               "event_at": datetime.now(UTC).isoformat()}, [item["id"]])
    return item["id"], event_id or store.list_events(project_id=project_id)[0]["id"]


def _point(text, url):
    return {"text": text, "refs": [{"code": "N1", "kind": "public", "label": "公司新闻", "text": "合成", "url": url}]}


def test_event_context_links_source_plan_point_assessment_and_judgment(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    c = TestClient(app)
    store, monitor = app.state.studio_store, app.state.studio_pipeline.monitor
    pid = c.post("/api/studio/projects", json={"entity": {"legal_name": "合成生物"}, "consent": True}).json()["id"]
    item_id, event_id = _event(store, pid, "合成产品进入II期", "合成原文：产品完成II期首例给药。")
    _other_item, other_event = _event(store, pid, "无关报道", "与本事件无关。")
    assert c.get(f"/api/studio/projects/{pid}/events/{event_id}/context").json()["assessments"] == []  # before any question table
    _ = app.state.studio_pipeline.questions  # the ledger creates its tables on first use
    watch = monitor.add_watchpoint(pid, "合成产品能否按期进入注册临床？", "合成产品临床", ["合成 临床"], "临床节点", True)
    with store._connect() as db:
        db.execute("UPDATE studio_watchpoints SET axis='asset', objects=?, reason='主力产品决定估值' WHERE id=?",
                   (json.dumps(["合成产品"], ensure_ascii=False), watch["id"]))
        db.execute("INSERT INTO studio_question_assessments (id, project_id, watch_id, actor, status, note, event_ids, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, pid, watch["id"], "system", "progress", "进入II期，按计划推进",
                                                json.dumps([event_id]), datetime.now(UTC).isoformat()))
        db.execute("INSERT INTO studio_question_assessments (id, project_id, watch_id, actor, status, note, event_ids, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, pid, watch["id"], "system", "unchanged", "只引用另一条",
                                                json.dumps([other_event + "x"]), datetime.now(UTC).isoformat()))
    monitor.add_hits([(watch["id"], item_id)])
    url = "https://n.example/合成产品进入II期"
    ring = {"verdict": "steady", "confidence": "medium", "summary": "s", "for": [], "against": [], "change_if": []}
    analysis = {"headline": "h", "gaps": [], "cited": [],
                "position": {**ring, "verdict": "behind", "peers": [], "for": [_point("对手更快", "https://other.example")],
                             "against": [_point("产品进入II期，差距缩小", url)]},
                "technology": {"route": "r", "maturity": "advanced", "consensus": "forming", "confidence": "low", "summary": "",
                               "academic": [], "industry": [], "risks": []},
                "industry": {**ring, "verdict": "growing", "drivers": [], "headwinds": []},
                "operations": {**ring, "for": [_point("临床推进", url)]}, "chain": ring}
    store.set_state(f"insight:{pid}", json.dumps({"analysis": analysis, "generated_at": "2026-10-15T00:00:00+00:00",
                                                  "counts": {}, "total": 1, "model": "synthetic"}, ensure_ascii=False))

    body = c.get(f"/api/studio/projects/{pid}/events/{event_id}/context").json()
    assert body["event"]["title"] == "合成产品进入II期"
    assert body["excerpts"][0]["text"] == "合成原文：产品完成II期首例给药。" and body["excerpts"][0]["url"] == url
    hit = body["hits"][0]
    assert (hit["section_label"], hit["ring_label"], hit["objects"], hit["reason"]) == ("资产与产品", "公司本体", ["合成产品"], "主力产品决定估值")
    assert [(a["note"], a["status_label"]) for a in body["assessments"]] == [("进入II期，按计划推进", "有进展")]
    assert {(j["part"], j["sign"], j["verdict_label"]) for j in body["judgments"]} == {("position", "−", "略落后"), ("operations", "+", "平稳")}

    other = c.get(f"/api/studio/projects/{pid}/events/{other_event}/context").json()
    assert other["hits"] == [] and other["assessments"] == [] and other["judgments"] == []
    assert c.get(f"/api/studio/projects/{pid}/events/missing/context").status_code == 404
    other_pid = c.post("/api/studio/projects", json={"entity": {"legal_name": "另一家"}, "consent": True}).json()["id"]
    assert c.get(f"/api/studio/projects/{other_pid}/events/{event_id}/context").status_code == 404  # never across projects
