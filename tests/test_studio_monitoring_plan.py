"""监控方案: plan items on top of watch points, with a change log, cadence and owner-only decisions. Synthetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app


def _app(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成药业"}, "consent": True}).json()["id"]
    return app, client, pid, app.state.studio_pipeline.monitoring_plan


ITEM = {"text": "合成资产一期是否按计划入组？", "axis": "self", "objects": ["合成资产"], "queries": ["合成资产 入组"],
        "reason": "核心资产", "cadence": "high", "tier": "core", "trigger_rule": "登记状态变化", "exit_rule": "一期结束"}


def test_import_view_edit_and_revert_keep_a_change_log(tmp_path):
    _, client, pid, _plan = _app(tmp_path)
    [item] = client.post(f"/api/studio/projects/{pid}/monitoring-plan/import",
                         json={"items": [ITEM], "reason": "首版方案（用户已审阅）"}).json()
    assert item["plan_origin"] == "draft" and item["objects"] == ["合成资产"] and item["tier"] == "core"
    # Re-importing the same question updates it instead of duplicating.
    client.post(f"/api/studio/projects/{pid}/monitoring-plan/import",
                json={"items": [{**ITEM, "cadence": "medium"}], "reason": "修订"})
    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    assert len(view["items"]) == 1 and view["items"][0]["cadence"] == "medium"
    assert [c["action"] for c in view["changes"]] == ["modify", "add"]
    # Retire, then revert the retirement.
    client.patch(f"/api/studio/monitoring-plan/items/{item['id']}", json={"status": "retired", "note": "已结束"})
    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    retire = view["changes"][0]
    assert retire["action"] == "retire" and retire["reason"] == "已结束" and view["summary"]["core"] == 0
    assert client.post(f"/api/studio/monitoring-plan/changes/{retire['id']}/revert").json()["status"] == "active"
    assert client.post(f"/api/studio/monitoring-plan/changes/{retire['id']}/revert").status_code == 409
    assert client.patch(f"/api/studio/monitoring-plan/items/{item['id']}", json={"axis": "nonsense"}).status_code == 422


def test_system_additions_are_low_cadence_trials_and_cannot_overrule_people(tmp_path):
    _, client, pid, plan = _app(tmp_path)
    added = plan.add(pid, {**ITEM, "text": "系统发现的新竞品读出", "cadence": "high"}, actor="system", origin="system",
                     reason="竞品读出后新出现")
    assert added["status"] == "trial" and added["cadence"] == "low"
    background = plan.add(pid, {**ITEM, "text": "系统建议的背景对象", "tier": "background"}, actor="system", origin="system")
    with pytest.raises(PermissionError):
        plan.update(background["id"], {"tier": "core"}, actor="system")  # promotion needs a person
    human = client.post(f"/api/studio/projects/{pid}/monitoring-plan/items", json={**ITEM, "text": "我关心的问题"}).json()
    with pytest.raises(PermissionError):
        plan.update(human["id"], {"status": "retired"}, actor="system")
    # Reverting a system addition retires it, history kept.
    change = next(c for c in plan.view(pid)["changes"] if c["watch_id"] == added["id"])
    assert plan.revert(change["id"])["status"] == "retired"
    questions = client.get(f"/api/studio/projects/{pid}/questions").json()
    listed = questions.get("questions", questions) if isinstance(questions, dict) else questions
    assert all(q.get("id") != added["id"] and q.get("watch_id") != added["id"] for q in listed)


def test_cadence_decides_what_runs_and_closed_items_never_run(tmp_path):
    _, _client, pid, plan = _app(tmp_path)
    high = plan.add(pid, {**ITEM, "text": "高频问题"}, actor="human", origin="human")
    weekly = plan.add(pid, {**ITEM, "text": "每周问题", "cadence": "medium", "tier": "background"}, actor="human", origin="human")
    event = plan.add(pid, {**ITEM, "text": "事件驱动问题", "cadence": "event"}, actor="human", origin="human")
    closed = plan.add(pid, {**ITEM, "text": "已关闭问题"}, actor="human", origin="human")
    plan.update(closed["id"], {"status": "retired"}, actor="human")
    assert [w["id"] for w in plan.due(pid)] == [high["id"], weekly["id"]]  # core first; event waits; closed never
    plan.mark_run([high["id"], weekly["id"]])
    assert [w["id"] for w in plan.due(pid)] == [high["id"]]  # weekly ran today
    later = datetime.now(UTC) + timedelta(days=7)
    assert {w["id"] for w in plan.due(pid, at=later)} == {high["id"], weekly["id"]}
    assert event["id"] not in {w["id"] for w in plan.due(pid, at=later)}


def test_competition_and_demand_items_file_news_under_their_own_category(tmp_path):
    app, _client, pid, plan = _app(tmp_path)
    pipeline = app.state.studio_pipeline
    plan.add(pid, {**ITEM, "text": "同靶点竞品读出", "axis": "competition", "queries": ["竞品 读出"]}, actor="human", origin="human")
    plan.add(pid, {**ITEM, "text": "患者有哪些治疗选择", "axis": "demand", "queries": ["病种 治疗 指南"]}, actor="human", origin="human")
    seen = []

    class Collect:
        has_search = False

        def news(self, query, search_count=0):
            seen.append(query)
            return [{"key": query, "title": f"{query} 报道", "url": f"https://news.example/{len(seen)}", "source": "s",
                     "snippet": "", "published_at": None}]

    pipeline._collect_monitor(pid, pipeline.store.get_project(pid), Collect(), lambda _t: None)
    categories = {i["title"]: i["category"] for i in pipeline.store.untriaged(pid)}
    assert categories == {"竞品 读出 报道": "competitor", "病种 治疗 指南 报道": "industry"}


def test_every_plan_shows_the_full_framework_with_project_baseline_and_grown_items(tmp_path):
    _, client, pid, _plan = _app(tmp_path)
    meta = {"understanding": "合成公司做连接器", "method": "汽车零部件方法", "out_of_scope": ["铜矿"],
            "sections": {"supply": {"baseline": "原材料价格与主要供应商", "known": "主要原料：铜材", "coverage": "partial"}}}
    client.post(f"/api/studio/projects/{pid}/monitoring-plan/import", json={
        "items": [{**ITEM, "text": "铜价会不会压缩毛利？", "axis": "supply"},
                  {**ITEM, "text": "主要客户定点有没有变化？", "axis": "customer"},
                  {**ITEM, "text": "旧的需求类问题", "axis": "demand"}],
        "meta": meta, "reason": "首版"})
    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    assert [s["key"] for s in view["framework"]] == ["asset", "operations", "peers", "alternatives", "customer",
                                                    "partners", "market", "science", "rules"]
    assert [s["ring"] for s in view["framework"]][::2] == ["company", "competition", "chain", "environment", "environment"]
    assert [r["label"] for r in view["rings"]] == ["公司本体", "竞争", "产业链", "环境"]
    supply = next(s for s in view["framework"] if s["key"] == "partners")  # older "supply" key lands on 供应、合作与资本方
    assert supply["baseline"] == "原材料价格与主要供应商" and supply["known"] == "主要原料：铜材"
    rules = next(s for s in view["framework"] if s["key"] == "rules")
    assert rules["baseline"] and rules["coverage"] == "none"  # empty sections still show the framework default
    sections = {w["text"]: w["section"] for w in view["items"]}
    assert sections["旧的需求类问题"] == "alternatives" and sections["铜价会不会压缩毛利？"] == "partners"
    assert sections["主要客户定点有没有变化？"] == "customer"
    assert view["meta"]["understanding"] == "合成公司做连接器" and view["meta"]["out_of_scope"] == ["铜矿"]
    # The summary change is logged and revertible like any item change.
    change = next(c for c in view["changes"] if c["action"] == "meta")
    assert change["item"] == "方案概要与分类基础"
    client.put(f"/api/studio/projects/{pid}/monitoring-plan/meta", json={"understanding": "改写", "note": "修订"})
    latest = next(c for c in client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["changes"] if c["action"] == "meta")
    client.post(f"/api/studio/monitoring-plan/changes/{latest['id']}/revert")
    assert client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["meta"]["understanding"] == "合成公司做连接器"
    assert client.put(f"/api/studio/projects/{pid}/monitoring-plan/meta", json={"sections": {"nope": {}}}).status_code == 422


def test_triage_sees_each_focus_point_with_its_objects_and_closed_ones_drop_out(tmp_path):
    from project_watch.studio import monitor as mon

    _, _client, pid, plan = _app(tmp_path)
    live = plan.add(pid, {**ITEM, "text": "TEAD 路线人体证据如何？", "axis": "science", "objects": ["VT3989", "ISM6331"],
                          "reason": "决定同靶点资产的可信度"}, actor="human", origin="human")
    gone = plan.add(pid, {**ITEM, "text": "旧问题"}, actor="human", origin="human")
    plan.update(gone["id"], {"status": "retired"}, actor="human")
    prompt = mon.watch_prompt([w for w in plan.items(pid) if w["status"] != "retired"])
    assert "对象：VT3989、ISM6331" in prompt and "与公司的关系：决定同靶点资产的可信度" in prompt
    assert "分类：学术与技术证据" in prompt and "即使不提被监控公司" in prompt
    assert "旧问题" not in prompt and live["text"] in prompt


def test_one_off_ai_suggestions_become_trials_and_skip_projects_with_a_plan(tmp_path):
    app, _client, pid, plan = _app(tmp_path)
    ledger = app.state.studio_pipeline.questions

    class Advisor:
        def suggest(self, llm, project, plan_, profile, existing):
            return [{"text": "竞品 VT3989 的最新读出意味着什么？", "label": "竞品读出", "queries": ["VT3989 读出"]}]

    llm = type("L", (), {"ready": True})()
    assert ledger.suggest(Advisor(), llm, pid, {"name": "合成药业"}, {}, None) == 1
    [item] = plan.items(pid)
    assert item["status"] == "trial" and item["plan_origin"] == "system" and item["cadence"] == "low"
    assert plan.view(pid)["changes"][0]["actor"] == "system"
    # A project that already has a reviewed plan does not get a one-off suggestion round.
    _app2, _c2, pid2, plan2 = _app(tmp_path / "second")
    plan2.add(pid2, ITEM, actor="human", origin="draft")
    ledger2 = _app2.state.studio_pipeline.questions
    assert ledger2.suggest(Advisor(), llm, pid2, {"name": "合成药业"}, {}, None) == 0


def test_display_modules_draw_only_on_stored_records(tmp_path):
    app, client, pid, plan = _app(tmp_path)
    store = app.state.studio_store
    timeline = plan.add(pid, {**ITEM, "text": "资产一期进度", "view": "timeline",
                              "view_params": {"milestones": [{"date": "2027-06", "label": "一期读出（计划）"}]}}, actor="human", origin="human")
    store.add_items(pid, "company", [{"key": "k1", "title": "合成资产一期完成首例给药", "url": "https://n.example/1",
                                      "source": "s", "snippet": "", "published_at": "2026-09-01"}])
    item_id = store.untriaged(pid)[0]["id"]
    store.save_triage([{"id": item_id, "relevant": True, "importance": "major", "summary": "s", "impact": ""}])
    plan.monitor.add_hits([(timeline["id"], item_id)])
    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    points = view["modules"][timeline["id"]]["points"]
    assert [p["planned"] for p in points] == [False, True] and points[0]["title"].startswith("合成资产一期")

    peers = plan.add(pid, {**ITEM, "text": "同行动作", "axis": "competition", "objects": ["甲公司（Alpha）", "乙公司"],
                           "view": "peer_table"}, actor="human", origin="human")
    store.create_event(pid, {"title": "甲公司宣布新产线投产", "category": "competitor", "importance": "notable"}, [])
    rows = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["modules"][peers["id"]]["rows"]
    assert rows[0]["latest"]["title"] == "甲公司宣布新产线投产" and rows[0]["recent_90d"] == 1 and rows[1]["latest"] is None

    metric = plan.add(pid, {**ITEM, "text": "客户车系月销", "axis": "customer", "view": "metric_series",
                            "view_params": {"metric": "月销量", "unit": "辆", "target": 160000, "target_label": "目标月均"}},
                      actor="human", origin="human")
    assert client.post(f"/api/studio/monitoring-plan/items/{metric['id']}/points",
                       json={"period": "2026-09", "value": 52000, "unit": "辆", "url": "ftp://x"}).status_code == 422
    point = client.post(f"/api/studio/monitoring-plan/items/{metric['id']}/points",
                        json={"period": "2026-09", "value": 52000, "unit": "辆", "url": "https://n.example/sales"}).json()
    series = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["modules"][metric["id"]]
    assert series["points"][0]["value"] == 52000 and series["target"] == 160000
    assert client.delete(f"/api/studio/monitoring-plan/points/{point['id']}").status_code == 204

    promise = plan.add(pid, {**ITEM, "text": "承诺是否兑现", "view": "commitments", "view_params": {"commitments": [
        {"claim": "一期 2026 年完成首例给药", "source": "BP p3", "keywords": ["首例给药"]},
        {"claim": "年底完成 B 轮", "source": "BP p9", "keywords": ["B 轮"]}]}}, actor="human", origin="human")
    plan.monitor.add_hits([(promise["id"], item_id)])
    rows = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["modules"][promise["id"]]["rows"]
    assert rows[0]["evidence"]["title"].startswith("合成资产一期") and rows[1]["evidence"] is None
    assert client.patch(f"/api/studio/monitoring-plan/items/{promise['id']}", json={"view": "bogus"}).status_code == 422


def test_industry_templates_fill_section_baselines_without_overwriting_project_text(tmp_path):
    _, client, pid, _plan = _app(tmp_path)
    catalog = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["templates"]
    assert {"innovative_drug", "auto_parts", "platform_service"} <= {t["key"] for t in catalog}
    client.put(f"/api/studio/projects/{pid}/monitoring-plan/meta",
               json={"sections": {"asset": {"baseline": "项目自写的自身进展口径", "coverage": "covered"}}})
    client.post(f"/api/studio/projects/{pid}/monitoring-plan/templates", json={"templates": ["innovative_drug", "platform_service"]})
    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    sections = {s["key"]: s for s in view["framework"]}
    assert sections["asset"]["baseline"] == "项目自写的自身进展口径"  # project text kept
    assert "同靶点" in sections["peers"]["baseline"] and "外部验证" in sections["science"]["baseline"]
    assert view["meta"]["templates"] == ["innovative_drug", "platform_service"] and "创新药方法" in view["meta"]["method"]
    assert client.post(f"/api/studio/projects/{pid}/monitoring-plan/templates", json={"templates": ["unknown"]}).status_code == 422


def test_peer_names_try_short_forms_and_skip_ambiguous_latin_abbreviations():
    from project_watch.studio.monitoring_plan import peer_names

    assert peer_names("电连技术") == ["电连技术", "电连"]
    assert peer_names("TE|泰科") == ["泰科"]  # bare "TE" would match unrelated headlines
    assert peer_names("英矽智能（ISM6331）") == ["英矽智能", "英矽", "ISM6331"]


def test_modules_show_only_hits_about_the_items_objects_and_merge_reposts(tmp_path):
    app, client, pid, plan = _app(tmp_path)
    store = app.state.studio_store
    item = plan.add(pid, {**ITEM, "text": "资产进展", "objects": ["SIGX1094"]}, actor="human", origin="human")
    store.add_items(pid, "company", [
        {"key": f"k{n}", "title": title, "url": f"https://n.example/{n}", "source": "s", "snippet": "", "published_at": "2026-09-01"}
        for n, title in enumerate(["SIGX1094 临床分中心启动", "SIGX1094临床分中心启动", "SIGX1094临床分中心启动仪式召开", "TRD-205 术后疼痛在研"])])
    rows = store.untriaged(pid)
    store.save_triage([{"id": r["id"], "relevant": True, "importance": "notable", "summary": "", "impact": ""} for r in rows])
    plan.monitor.add_hits([(item["id"], r["id"]) for r in rows])
    module = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["modules"][item["id"]]
    # Exact reposts merge; an expanded headline is not proof of the same event.
    assert len(module["items"]) == 2 and all(h["title"].startswith("SIGX1094") for h in module["items"])
    assert all("TRD-205" not in h["title"] for h in module["items"])


def test_instance_local_templates_load_from_the_data_directory(tmp_path):
    import json as _json

    from project_watch.studio import plan_templates

    folder = tmp_path / "app" / "plan_templates"  # the instance data directory the app loads at start-up
    folder.mkdir(parents=True)
    (folder / "connectors.json").write_text(_json.dumps({
        "key": "auto_connectors", "label": "汽车连接器", "method": "连接器细分方法", "source": "NEI 方法转换",
        "sections": {"technology": "接口标准与速率演进"},
        "focus": [{"section": "technology", "text": "接口标准有没有新版本？", "view": "timeline"}]}, ensure_ascii=False), encoding="utf-8")
    (folder / "bad.json").write_text('{"key": "innovative_drug", "sections": {"self": "x"}}', encoding="utf-8")  # shadows built-in
    (folder / "broken.json").write_text("{not json", encoding="utf-8")
    try:
        assert plan_templates.load_local(folder) == 1  # the shadowing and broken files are skipped
        _, client, pid, _plan = _app(tmp_path / "app")
        catalog = {t["key"]: t for t in client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["templates"]}
        assert catalog["auto_connectors"]["source"] == "NEI 方法转换" and catalog["innovative_drug"]["source"] == "内置"
        client.post(f"/api/studio/projects/{pid}/monitoring-plan/templates", json={"templates": ["auto_parts", "auto_connectors"]})
        tech = next(s for s in client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["framework"] if s["key"] == "science")  # written for the older "technology" key
        assert "接口标准与速率演进" in tech["baseline"]
    finally:
        plan_templates.LOCAL.clear()


def test_each_focus_point_has_one_latest_line(tmp_path):
    app, client, pid, plan = _app(tmp_path)
    store = app.state.studio_store
    noted = plan.add(pid, {**ITEM, "text": "有当前认识的问题"}, actor="human", origin="human")
    plan.update(noted["id"], {"current_state": "一期已入组 12 例"}, actor="system")
    hit = plan.add(pid, {**ITEM, "text": "只有命中的问题"}, actor="human", origin="human")
    store.add_items(pid, "company", [{"key": "k", "title": "合成资产一期完成首例给药", "url": "https://n.example/1",
                                      "source": "s", "snippet": "", "published_at": "2026-09-01"}])
    row = store.untriaged(pid)[0]
    store.save_triage([{"id": row["id"], "relevant": True, "importance": "notable", "summary": "", "impact": ""}])
    plan.monitor.add_hits([(hit["id"], row["id"])])
    routine = plan.add(pid, {**ITEM, "text": "只有一般信息的问题"}, actor="human", origin="human")
    store.add_items(pid, "company", [{"key": "r", "title": "合成资产试剂价格表", "url": "https://n.example/2",
                                      "source": "s", "snippet": "", "published_at": "2026-09-02"}])
    plain = next(i for i in store.untriaged(pid))
    store.save_triage([{"id": plain["id"], "relevant": True, "importance": "routine", "summary": "", "impact": ""}])
    plan.monitor.add_hits([(routine["id"], plain["id"])])
    idle = plan.add(pid, {**ITEM, "text": "还没跑过的问题"}, actor="human", origin="human")
    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    latest = view["latest"]
    assert latest[noted["id"]]["kind"] == "note" and latest[noted["id"]]["text"] == "一期已入组 12 例"
    assert not latest[noted["id"]]["fresh"]  # editing an understanding alone is not new evidence
    assert latest[hit["id"]]["kind"] == "hit" and latest[hit["id"]]["text"].startswith("合成资产一期")
    assert latest[idle["id"]] == {"kind": "none", "text": "尚未检索", "date": "", "url": "", "count": 0, "fresh": False}
    assert latest[routine["id"]]["kind"] == "none" and latest[routine["id"]]["text"] == "已检索，仅有一般信息（1 条）"
    assert view["week"]["new_items"] == 4 and view["week"]["fresh"] >= 1


def test_routine_collection_is_shown_per_section_beside_the_focus_points(tmp_path):
    app, client, pid, _plan = _app(tmp_path)
    store = app.state.studio_store
    store.put_plan(pid, {"company_queries": ["合成药业"], "has_search": False, "has_registry": False,
                         "last_report": {"company": {"checked": 2, "added": 3, "errors": []},
                                         "competitor": {"checked": 0, "added": 0, "errors": ["bing: http_failure"]}}})
    store.create_event(pid, {"title": "竞品融资", "category": "competitor", "importance": "notable"}, [])
    coverage = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["coverage"]
    assert set(coverage) == {"asset", "operations", "peers", "alternatives", "customer", "partners", "market", "science", "rules"}
    competitor = next(c for c in coverage["peers"] if c["key"] == "competitor")
    assert competitor["count"] == 1 and competitor["window"] == "近 30 天" and competitor["state"] == "error"
    assert next(c for c in coverage["operations"] if c["key"] == "registry")["state"] == "off"  # no registry service configured
    assert next(c for c in coverage["science"] if c["key"] == "patent")["state"] == "off"  # needs a search service
    assert coverage["alternatives"] == []  # nothing collected routinely: only focus points cover it


def test_older_keys_are_placed_by_what_the_item_is_about():
    from project_watch.studio.monitoring_plan import place

    assert place("self", "新基地投产后产线是否按计划爬坡？") == "operations"
    assert place("technology", "核心管线锁定哪些适应症？") == "asset"
    assert place("technology", "TEAD 路线的人体证据在变强还是变弱？") == "science"
    assert place("capital", "与晶泰的合作经济安排如何影响收益归属？") == "partners"
    assert place("peers", "任何措辞") == "peers"  # new keys are kept as given


def test_older_eight_section_plans_move_once_into_the_nine_dimensions(tmp_path):
    _app2, client, pid, plan = _app(tmp_path)
    store = plan.monitor.store
    rows = {text: plan.add(pid, {**ITEM, "text": text}, actor="human", origin="human")["id"] for text in
            ("资产一期进度如何？", "A 轮融资承诺是否兑现？", "同病种替代疗法在变吗？", "铜价会不会压缩毛利？", "同行融资热度如何？")}
    old = {"资产一期进度如何？": "self", "A 轮融资承诺是否兑现？": "self", "同病种替代疗法在变吗？": "competition",
           "铜价会不会压缩毛利？": "supply", "同行融资热度如何？": "capital"}
    with plan.monitor._db() as db, db:  # as stored before the change
        for text, axis in old.items():
            db.execute("UPDATE studio_watchpoints SET axis=? WHERE id=?", (axis, rows[text]))
    import json as _json

    store.set_state(f"monitoring_plan_meta:{pid}", _json.dumps({"sections": {
        "self": {"known": "一期进行中", "coverage": "partial"}, "supply": {"known": "主要原料：铜材", "coverage": "covered"}}},
        ensure_ascii=False))
    store.set_state("monitoring_plan_framework", "")
    assert plan.migrate_framework() == 5 and plan.migrate_framework() == 0  # once
    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    placed = {w["text"]: w["section"] for w in view["items"]}
    assert placed == {"资产一期进度如何？": "asset", "A 轮融资承诺是否兑现？": "operations", "同病种替代疗法在变吗？": "alternatives",
                      "铜价会不会压缩毛利？": "partners", "同行融资热度如何？": "market"}
    sections = {s["key"]: s for s in view["framework"]}
    assert sections["asset"]["known"] == "一期进行中" and sections["partners"]["known"] == "主要原料：铜材"
    regroup = [c for c in view["changes"] if c["action"] == "regroup"]
    assert len(regroup) == 6 and all(c["reason"].startswith("监控框架改为四圈九类") for c in regroup)
    assert client.post(f"/api/studio/monitoring-plan/changes/{regroup[0]['id']}/revert").status_code in (409, 422)
    # People move a misplaced item themselves.
    moved = client.patch(f"/api/studio/monitoring-plan/items/{rows['铜价会不会压缩毛利？']}", json={"axis": "market", "note": "移到"}).json()
    assert moved["axis"] == "market"
