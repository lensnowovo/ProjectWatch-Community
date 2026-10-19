"""方案修订 (P4): rounds and new materials grow the plan under the owner's rules. Synthetic, fake model."""

from __future__ import annotations

from fastapi.testclient import TestClient

from project_watch.studio import plan_replan
from project_watch.studio.app import create_studio_app

ITEM = {"text": "合成资产一期是否按计划入组？", "axis": "self", "objects": ["合成资产"], "queries": ["合成资产 入组"],
        "reason": "核心资产", "cadence": "medium", "tier": "core"}


def _setup(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成药业"}, "consent": True}).json()["id"]
    pipeline = app.state.studio_pipeline
    plan = pipeline.monitoring_plan
    draft = plan.add(pid, ITEM, actor="human", origin="draft", reason="首版")
    trial = plan.add(pid, {**ITEM, "text": "系统试运行的旧问题"}, actor="system", origin="system")
    event = pipeline.store.create_event(pid, {"title": "竞品甲公布同靶点一期数据", "category": "competitor",
                                              "importance": "major", "impact": "同靶点竞争加剧"}, [])
    return app, client, pid, pipeline, plan, draft, trial, event


class Model:
    ready = True

    def __init__(self, changes):
        self.changes, self.inputs = changes, []

    def json(self, system, user, max_tokens=0):
        self.inputs.append(user)
        return {"changes": self.changes}


def test_a_round_grows_the_plan_within_the_owners_rules(tmp_path):
    _app, client, pid, pipeline, plan, draft, _trial, _event = _setup(tmp_path)
    model = Model([
        {"action": "add", "section": "competition", "text": "竞品甲的同靶点数据会改变竞争格局吗？", "label": "竞品甲",
         "objects": ["竞品甲"], "queries": ["竞品甲 一期 数据"], "reason": "同靶点", "view": "peer_table",
         "from": "W1", "refs": ["E1"], "why": "竞品甲首次公布同靶点数据，现有关注点未覆盖"},
        {"action": "add", "section": "competition", "text": "没有出处的新增", "refs": [], "why": "x"},          # uncited
        {"action": "add", "section": "nowhere", "text": "不在框架里的分类问题", "refs": ["E1"], "why": "x"},     # bad section
        {"action": "add", "section": "self", "text": ITEM["text"], "refs": ["E1"], "why": "x"},                # duplicate
        {"action": "note", "item": "W1", "text": "竞品甲已公布同靶点一期数据，入组节奏需对照", "refs": ["E1"], "why": "新竞品数据"},
        {"action": "retire", "item": "W1", "refs": ["E1"], "why": "建议关闭首版条目"},                         # reviewed → proposal
        {"action": "reduce", "item": "W2", "refs": ["E1"], "why": "系统旧问题已无新信息"},                     # system trial → applied
        {"action": "retire", "item": "W9", "refs": ["E1"], "why": "不存在的条目"},
    ])
    counts = plan_replan.run(pipeline, model, pid, since="2000-01-01", trigger="external")
    # The note on W1 cites a competitor event that never names 合成资产: refused (unrelated evidence).
    assert counts == {"added": 1, "noted": 0, "applied": 1, "proposed": 1, "dropped": 5}
    assert "未被任何关注点覆盖" in model.inputs[0] and "[W1]" in model.inputs[0]

    items = {w["text"]: w for w in plan.items(pid)}
    added = items["竞品甲的同靶点数据会改变竞争格局吗？"]
    assert added["status"] == "trial" and added["axis"] == "peers" and added["view"] == "peer_table"  # older key from the model, placed
    assert items[ITEM["text"]]["current_state"] == "" and items[ITEM["text"]]["status"] == "active"
    assert items["系统试运行的旧问题"]["status"] == "reduced"

    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    [proposal] = view["proposals"]
    assert proposal["action"] == "retire" and proposal["evidence"][0]["title"] == "竞品甲公布同靶点一期数据"
    assert all(c["state"] != "proposed" for c in view["changes"])
    add_change = next(c for c in view["changes"] if c["action"] == "add" and c["actor"] == "system")
    assert add_change["evidence"][0]["ref"] == "E1"

    # The owner rejects the proposal: the reviewed item stays open.
    assert client.post(f"/api/studio/monitoring-plan/changes/{proposal['id']}/reject").status_code == 200
    assert plan._get(pid, draft["id"])["status"] == "active"
    assert client.post(f"/api/studio/monitoring-plan/changes/{proposal['id']}/accept").status_code == 404


def test_accepting_a_proposal_applies_it_as_the_owner(tmp_path):
    _app, client, pid, _pipeline, plan, draft, _trial, _event = _setup(tmp_path)
    change_id = plan.propose(draft["id"], {"status": "reduced"}, reason="长期无新信息", evidence=[{"ref": "E1"}])
    assert plan.propose(draft["id"], {"status": "reduced"}, reason="重复", evidence=[]) == change_id  # no duplicates
    client.post(f"/api/studio/monitoring-plan/changes/{change_id}/accept")
    assert plan._get(pid, draft["id"])["status"] == "reduced"
    latest = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["changes"][0]
    assert latest["actor"] == "human" and latest["reason"].startswith("接受系统建议")


def test_at_most_three_additions_and_nothing_new_means_no_call(tmp_path):
    _app, _client, pid, pipeline, _plan, *_ = _setup(tmp_path)
    adds = [{"action": "add", "from": "W1", "section": "customer", "text": f"新增需求问题第{n}个", "refs": ["E1"], "why": "新事件"} for n in range(5)]
    assert plan_replan.run(pipeline, Model(adds), pid, since="2000-01-01", trigger="external")["added"] == 3
    quiet = Model([])
    assert plan_replan.run(pipeline, quiet, pid, since="2999-01-01", trigger="external") == {"skipped": True}
    assert quiet.inputs == []


def test_new_materials_are_numbered_as_material_changes(tmp_path):
    _app, _client, pid, pipeline, _plan, *_ = _setup(tmp_path)
    with pipeline.store._connect() as db:
        db.execute("INSERT INTO studio_doc_changes (id, project_id, source_ids, data, created_at) VALUES (?,?,?,?,?)",
                   ("c1", pid, "[]", '{"changes":[{"kind":"新进展","text":"BP 新增：关键原料依赖单一供应商"}]}', "2026-10-14T00:00:00+00:00"))
    model = Model([{"action": "add", "from": "W1", "section": "supply", "text": "单一供应商依赖会不会断供？", "refs": ["M1"], "why": "新资料披露"}])
    counts = plan_replan.run(pipeline, model, pid, since="2999-01-01", trigger="internal")
    assert counts["added"] == 1 and "[M1] 新进展：BP 新增：关键原料依赖单一供应商" in model.inputs[0]
    added = next(w for w in pipeline.monitoring_plan.items(pid) if w["text"] == "单一供应商依赖会不会断供？")
    assert added["plan_origin"] == "internal" and added["status"] == "trial"


def test_pipeline_runs_the_injected_reviser_and_records_the_time(tmp_path):
    _app, _client, pid, pipeline, *_ = _setup(tmp_path)
    assert pipeline.plan_reviser is plan_replan  # wired by the composition root
    calls = []

    class Reviser:
        since_last = staticmethod(plan_replan.since_last)
        mark = staticmethod(plan_replan.mark)

        @staticmethod
        def run(pipe, llm, project_id, since, trigger):
            calls.append((project_id, trigger))
            return {}

    pipeline.plan_reviser = Reviser
    pipeline._replan(pid, type("L", (), {"ready": True})(), lambda _t: None, trigger="external")
    assert calls == [(pid, "external")] and pipeline.store.get_state(plan_replan.STATE_KEY.format(pid))


def test_plan_health_counts_runs_hits_and_the_owners_decisions(tmp_path):
    _app, client, pid, pipeline, plan, draft, _trial, _event = _setup(tmp_path)
    plan.mark_run([draft["id"]])
    model = Model([{"action": "add", "from": "W1", "section": "customer", "text": "新的需求问题是什么？", "refs": ["E1"], "why": "新事件"},
                   {"action": "retire", "item": "W1", "refs": ["E1"], "why": "建议关闭"}])
    plan_replan.run(pipeline, model, pid, since="2000-01-01", trigger="external")
    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    client.post(f"/api/studio/monitoring-plan/changes/{view['proposals'][0]['id']}/reject")
    added = next(c for c in view["changes"] if c["actor"] == "system" and c["action"] == "add")
    client.post(f"/api/studio/monitoring-plan/changes/{added['id']}/revert")
    health = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()["health"]
    assert health["ran_14d"] == 1 and health["hit_30d"] == 0 and health["never_hit"] == health["items"]
    assert health["system_added"] == 2 and health["system_reverted"] == 1  # the setup's trial + this round's addition
    assert health["proposals"] == {"proposed": 0, "accepted": 0, "rejected": 1}


def test_a_current_state_note_needs_the_points_own_evidence_and_supported_figures(tmp_path):
    _app, _client, pid, pipeline, plan, draft, _trial, _event = _setup(tmp_path)
    pipeline.store.create_event(pid, {"title": "合成资产一期完成首批入组", "summary": "合成资产一期已入组 12 例患者",
                                      "category": "company", "importance": "notable"}, [])
    gathered = plan_replan.gather(pipeline, pid, since="2000-01-01", trigger="external")
    own = next(k for k, v in gathered["numbered"].items() if v.get("title") == "合成资产一期完成首批入组")
    other = next(k for k, v in gathered["numbered"].items() if v.get("title") == "竞品甲公布同靶点一期数据")
    w1 = next(k for k, w in gathered["items"].items() if w["id"] == draft["id"])
    model = Model([
        {"action": "note", "item": w1, "text": "一期已入组 12 例，按计划推进", "refs": [own], "why": "新进展"},
    ])
    assert plan_replan.run(pipeline, model, pid, since="2000-01-01", trigger="external")["noted"] == 1
    assert plan._get(pid, draft["id"])["current_state"] == "一期已入组 12 例，按计划推进"
    for text, refs in (("一期已入组 30 例", [own]),                  # figure not in the cited material
                       ("竞品数据显示入组放缓", [other])):           # evidence not about this point
        result = plan_replan.run(pipeline, Model([{"action": "note", "item": w1, "text": text, "refs": refs, "why": "x"}]),
                                 pid, since="2000-01-01", trigger="external")
        assert result["noted"] == 0 and result["dropped"] == 1
    assert plan._get(pid, draft["id"])["current_state"] == "一期已入组 12 例，按计划推进"


BOOT = {"templates": ["innovative_drug", "nonexistent"], "understanding": "合成药业做一款靶向药", "in_scope": ["同靶点资产"],
        "out_of_scope": ["无关业务"], "sections": {"self": {"known": "一期进行中", "coverage": "partial"}, "bogus": {"known": "x"}},
        "items": [
            {"section": "self", "text": "合成资产一期进度如何？", "label": "一期进度", "objects": ["合成资产"], "queries": ["合成资产 一期"],
             "reason": "核心资产", "view": "timeline", "tier": "core", "cadence": "high", "hop": 0, "trigger": "读出", "exit": "一期结束"},
            {"section": "competition", "text": "同靶点竞品甲进展如何？", "objects": ["竞品甲"], "queries": ["竞品甲 一期"],
             "view": "peer_table", "tier": "core", "cadence": "medium", "hop": 1},
            {"section": "customer", "text": "目标病种有哪些替代疗法？", "objects": ["目标病种"], "hop": 5},
            {"section": "nowhere", "text": "不在框架里"}]}


def _fresh(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成药业"}, "consent": True}).json()["id"]
    return app, client, pid, app.state.studio_pipeline


class Boot(Model):
    def json(self, system, user, max_tokens=0):
        self.inputs.append(user)
        return self.changes


def test_a_name_becomes_a_first_plan_marked_for_review(tmp_path):
    _app, client, pid, pipeline = _fresh(tmp_path)
    model = Boot(BOOT)
    result = plan_replan.bootstrap(pipeline, model, pid)
    assert result["added"] == 3 and result["dropped"] == 1 and result["templates"] == ["innovative_drug"]
    assert "可选行业模板" in model.inputs[0] and "innovative_drug" in model.inputs[0]
    view = client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    assert view["meta"]["review"] == "pending" and view["meta"]["understanding"] == "合成药业做一款靶向药"
    items = {w["text"]: w for w in view["items"]}
    assert items["合成资产一期进度如何？"]["status"] == "active" and items["合成资产一期进度如何？"]["plan_origin"] == "bootstrap"
    assert items["同靶点竞品甲进展如何？"]["hop"] == "1" and items["目标病种有哪些替代疗法？"]["hop"] == "2"  # capped at 2
    sections = {s["key"]: s for s in view["framework"]}
    assert sections["asset"]["known"] == "一期进行中" and "同靶点" in sections["peers"]["baseline"]
    assert items["目标病种有哪些替代疗法？"]["section"] == "alternatives"  # older "customer" + 替代 wording → 同需求替代
    assert view["state"]["phase"] == "growing"
    assert plan_replan.bootstrap(pipeline, Boot(BOOT), pid) == {"skipped": True}  # only once
    # Confirming the review protects every item from unilateral system closure.
    client.post(f"/api/studio/projects/{pid}/monitoring-plan/review")
    plan = pipeline.monitoring_plan
    first = next(w for w in plan.items(pid) if w["text"] == "合成资产一期进度如何？")
    assert plan._owned_by_people(first) and plan.meta(pid)["review"] == "done"


def test_chains_are_bounded_and_new_directions_open_as_topics(tmp_path):
    _app, _client, pid, pipeline, plan, draft, _trial, _event = _setup(tmp_path)
    child = plan.add(pid, {**ITEM, "text": "一阶延伸", "axis": "competition", "parent_id": draft["id"]}, actor="system", origin="system")
    grandchild = plan.add(pid, {**ITEM, "text": "二阶延伸", "axis": "competition", "parent_id": child["id"]}, actor="system", origin="system")
    assert child["hop"] == "1" and grandchild["hop"] == "2"
    try:
        plan.add(pid, {**ITEM, "text": "三阶延伸", "axis": "competition", "parent_id": grandchild["id"]}, actor="system", origin="system")
        raise AssertionError("third hop accepted")
    except ValueError as exc:
        assert "最多延伸 2 层" in str(exc)
    plan.add(pid, {**ITEM, "text": "竞争第三个", "axis": "competition", "hop": "1"}, actor="system", origin="system")
    plan.add(pid, {**ITEM, "text": "竞争第四个", "axis": "competition", "hop": "1"}, actor="system", origin="system")
    try:
        plan.add(pid, {**ITEM, "text": "竞争第五个", "axis": "competition", "hop": "1"}, actor="system", origin="system")
        raise AssertionError("section cap ignored")
    except ValueError as exc:
        assert "已有 4 个关注点" in str(exc)
    gathered = plan_replan.gather(pipeline, pid, since="2000-01-01", trigger="external")
    w1 = next(k for k, w in gathered["items"].items() if w["id"] == draft["id"])
    model = Model([{"action": "explore", "section": "supply", "text": "关键原料是否只有一家供应商？", "objects": ["原料甲"],
                    "exit": "确认供应商数量后关闭", "from": w1, "refs": ["E1"], "why": "新信息提到原料紧张"}])
    assert plan_replan.run(pipeline, model, pid, since="2000-01-01", trigger="external")["added"] == 1
    topic = next(w for w in plan.items(pid) if w["text"] == "关键原料是否只有一家供应商？")
    assert topic["tier"] == "topic" and topic["status"] == "trial" and topic["exit_rule"] == "确认供应商数量后关闭"
    assert topic["parent_id"] == draft["id"] and topic["hop"] == "1"


def test_the_plan_settles_and_wakes_up_on_major_news_or_new_material(tmp_path):
    _app, _client, pid, pipeline, plan, *_ = _setup(tmp_path)
    for _ in range(3):
        plan.record_revision(pid, {"added": 0, "noted": 1, "proposed": 0}, "external")
    assert plan.state(pid)["phase"] == "stable"
    quiet = Model([])
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_events SET importance='notable'")
    assert plan_replan.run(pipeline, quiet, pid, since="2000-01-01", trigger="external") == {"skipped": True, "reason": "stable"}
    assert quiet.inputs == []  # no model call while stable and nothing major
    with pipeline.store._connect() as db:
        db.execute("UPDATE studio_events SET importance='major'")
    plan_replan.run(pipeline, quiet, pid, since="2000-01-01", trigger="external")
    assert len(quiet.inputs) == 1  # major news reopens the plan
    plan.record_revision(pid, {"added": 1}, "external")
    assert plan.state(pid)["phase"] == "growing"


def test_pipeline_bootstraps_when_a_project_has_no_plan(tmp_path):
    _app, _client, pid, pipeline = _fresh(tmp_path)
    calls = []

    class Reviser:
        since_last = staticmethod(plan_replan.since_last)
        mark = staticmethod(plan_replan.mark)

        @staticmethod
        def bootstrap(pipe, llm, project_id):
            calls.append("bootstrap")
            return {}

        @staticmethod
        def run(pipe, llm, project_id, since, trigger):
            calls.append("run")
            return {}

    pipeline.plan_reviser = Reviser
    pipeline._replan(pid, type("L", (), {"ready": True})(), lambda _t: None, trigger="external")
    assert calls == ["bootstrap"]


def test_the_first_plan_absorbs_earlier_one_off_questions(tmp_path):
    _app, _client, pid, pipeline = _fresh(tmp_path)
    monitor = pipeline.monitor
    keep = monitor.add_watchpoint(pid, "合成资产一期进度如何？", "一期", ["合成资产"], "进度", False, origin="ai")
    drop = monitor.add_watchpoint(pid, "公司股价怎么样？", "股价", ["合成药业 股价"], "股价", False, origin="ai")
    plan = pipeline.monitoring_plan
    assert not plan.has_plan(pid)  # older one-off questions are not a plan
    model = Boot(BOOT)
    result = plan_replan.bootstrap(pipeline, model, pid)
    assert "已有跟踪问题" in model.inputs[0] and "公司股价怎么样？" in model.inputs[0]
    assert result["kept"] == 1 and result["retired"] == 1
    items = {w["id"]: w for w in plan.items(pid)}
    assert items[keep["id"]]["plan_origin"] == "bootstrap" and items[keep["id"]]["status"] == "active"
    assert items[keep["id"]]["view"] == "timeline"  # filled from the first plan
    assert items[drop["id"]]["status"] == "retired"
    assert plan.has_plan(pid)


def test_first_plan_uses_modules_it_can_fill_and_matches_reworded_old_questions(tmp_path):
    _app, _client, pid, pipeline = _fresh(tmp_path)
    old = pipeline.monitor.add_watchpoint(pid, "化学重编程在糖尿病上的合作授权进展？", "授权", ["合作"], "x", False, origin="ai")
    boot = {"templates": ["innovative_drug"], "understanding": "u", "sections": {}, "items": [
        {"section": "commercial", "text": "合成药业化学重编程在糖尿病上的合作授权进展？", "view": "timeline", "objects": ["平台"]},
        {"section": "technology", "text": "核心专利与人员变动？", "view": "commitments", "objects": ["专利"]},
        {"section": "customer", "text": "试剂盒客户采用如何？", "view": "metric_series", "objects": ["试剂盒"]},
        {"section": "customer", "text": "终端月销如何？", "view": "metric_series", "metric": "月销量", "unit": "辆", "objects": ["车型"]}]}
    result = plan_replan.bootstrap(pipeline, Boot(boot), pid)
    assert result["kept"] == 1 and result["retired"] == 0  # reworded old question kept, not duplicated
    items = {w["text"]: w for w in pipeline.monitoring_plan.items(pid)}
    assert items[old["text"]]["plan_origin"] == "bootstrap" and items[old["text"]]["view"] == "timeline"
    assert "合成药业化学重编程在糖尿病上的合作授权进展？" not in items
    assert items["核心专利与人员变动？"]["view"] == "evidence"  # no stated commitments to compare
    assert items["试剂盒客户采用如何？"]["view"] == "evidence"  # no metric named
    assert items["终端月销如何？"]["view"] == "metric_series" and items["终端月销如何？"]["view_params"]["metric"] == "月销量"


def test_reasons_are_written_as_causes_tied_to_a_concrete_relation():
    for prompt in (plan_replan.SYSTEM, plan_replan.BOOTSTRAP):
        assert "因果句" in prompt
        for relation in ("主要产品", "同一适应症或同一靶点的竞品", "关键客户", "关键原料或供应商"):
            assert relation in prompt
        assert "不写“很重要”“核心资产”这类没有因果的空话" in prompt


def test_prompts_use_the_four_rings_and_place_items_by_their_object():
    for prompt in (plan_replan.SYSTEM, plan_replan.BOOTSTRAP):
        assert "四圈九类" in prompt and "八类" not in prompt
        for key in ("asset", "operations", "peers", "alternatives", "customer", "partners", "market", "science", "rules"):
            assert key in prompt
        assert "竞品的论文归 peers" in prompt


def test_the_first_plan_never_closes_a_persons_question_and_an_empty_answer_changes_nothing(tmp_path):
    _app, _client, pid, pipeline = _fresh(tmp_path)
    monitor = pipeline.monitor
    mine = monitor.add_watchpoint(pid, "合成资产的人工问题？", "人工", ["合成资产"], "进度", False, origin="human")
    assert plan_replan.bootstrap(pipeline, Boot({"understanding": "无条目"}), pid)["skipped"]
    plan = plan_replan.MonitoringPlan(monitor)
    assert next(w for w in plan.items(pid) if w["id"] == mine["id"])["status"] == "active"  # untouched
    result = plan_replan.bootstrap(pipeline, Boot({**BOOT, "items": BOOT["items"][:1]}), pid)
    assert result["proposed"] == 1 and result["retired"] == 0
    assert next(w for w in plan.items(pid) if w["id"] == mine["id"])["status"] == "active"
    view = _client.get(f"/api/studio/projects/{pid}/monitoring-plan").json()
    assert any(c["watch_id"] == mine["id"] and c["after"].get("status") == "retired" for c in view["proposals"])
