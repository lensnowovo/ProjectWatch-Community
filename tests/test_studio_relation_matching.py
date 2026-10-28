"""Synthetic public aliases/body routing; no paid tools or desktop state."""
import pytest

from project_watch.studio.app import create_studio_app
from project_watch.studio.questions import QuestionLedger


@pytest.fixture
def sample(tmp_path):
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project("合成公司").id
    p.store.upsert_project(pid, {"legal_name": "合成公司"}, consent=True)
    root = p.monitoring_plan.add(pid, {
        "text": "合成产品持续监控", "scope_kind": "relation", "relation_type": "product",
        "relation_target": "ABC-ONE", "relation_aliases": ["合成手部系统"],
        "reason": "公司开发该产品", "sources": "https://example.com/product",
        "queries": ["ABC-ONE 新进展"], "tier": "background", "cadence": "high", "axis": "asset",
    }, actor="human", origin="human", visibility="public")
    child = p.monitoring_plan.add(pid, {
        "text": "ABC-ONE 是否获得许可", "scope_kind": "question", "parent_id": root["id"],
        "queries": ["ABC-ONE 获批"], "tier": "topic", "exit_rule": "许可公告出现",
    }, actor="human", origin="human", visibility="public")
    return p, pid, root, child


def add_event(p, pid, key, body, status="article_body"):
    p.store.add_items(pid, "company", [{"key": key, "title": "公开行业观察",
        "url": "https://example.com/" + key, "source": "synthetic", "snippet": "行业观察", "published_at": None}])
    item = p.store.item_by_key(pid, key)
    p.store.save_body(item["id"], status, body)
    eid = p.store.create_event(pid, {"title": "公开行业观察", "importance": "notable", "category": "company"}, [item["id"]])
    return item["id"], eid


def test_alias_body_routes_and_displays_attributable_excerpt(sample):
    p, pid, root, child = sample
    assert child["relation_aliases"] == ["合成手部系统"]
    _, eid = add_event(p, pid, "body", "这是一条报道。合成手部系统获得许可，可用于临床。")
    assert p.questions.route_existing_events(pid, [root["id"], child["id"]], focused=True) == 2
    view = p.monitoring_plan.view(pid)["relation_updates"][root["id"]]
    assert view["count"] == 1
    assert view["items"][0]["basis"] == "full_body"
    assert view["items"][0]["matched_term"] == "合成手部系统"
    assert view["items"][0]["event_id"] == eid
    assert p.questions.public_event_match(child, {}, {"id": eid})


@pytest.mark.parametrize("status", ["read_failed", "unavailable"])
def test_unread_bodies_do_not_count_as_evidence(sample, status):
    p, pid, root, child = sample
    add_event(p, pid, "unread", "合成手部系统获得许可", status)
    assert p.questions.route_existing_events(pid, [root["id"], child["id"]], focused=True) == 0
    assert p.monitoring_plan.view(pid)["relation_updates"][root["id"]]["count"] == 0


def test_saved_search_excerpt_routes_as_summary_not_body(sample):
    p, pid, root, child = sample
    item, eid = add_event(p, pid, "excerpt", "合成手部系统获得许可。", "search_summary")
    assert p.questions.route_existing_events(pid, [root["id"], child["id"]], focused=True) == 2
    match = p.questions.public_event_match(child, {}, {"id": eid})
    assert match["basis"] == "search_summary"
    pending = p.questions.new_events(pid, child["id"])
    with p.monitor._db() as db, db:
        p.questions._consume(db, pid, child["id"], pending)
    p.monitoring_plan.explorations.refresh(pid)
    p.monitoring_plan.explorations.stop(child, "answered")
    assert p.questions.route_existing_events(pid, [child["id"]], focused=True) == 0
    p.monitoring_plan.explorations.refresh(pid)
    assert p.monitoring_plan.explorations.view(pid)[child["id"]]["state"] == "stopped"
    p.store.save_body(item, "search_summary", "合成手部系统的许可被撤销。")
    assert p.questions.route_existing_events(pid, [child["id"]], focused=True) == 1
    p.monitoring_plan.explorations.refresh(pid)
    assert p.monitoring_plan.explorations.view(pid)[child["id"]]["reason"] == "new_evidence"
    assert p.questions.new_events(pid, child["id"])


def test_question_requires_outcome_near_subject_and_ascii_boundaries(sample):
    _, _, root, child = sample
    match = QuestionLedger.public_row_match
    assert not match(root, {}, {"title": "ABC-ONE-PLUS 新进展"})
    assert not match(child, {}, {"body_status": "article_body", "body": "ABC-ONE " + "无关段落。" * 300 + "另一产品获得许可"})
    assert match(child, {}, {"body_status": "partial_body", "body": "合成手部系统获得许可"})["basis"] == "partial_body"


def test_alias_change_is_audited_and_inherited(sample):
    p, pid, root, child = sample
    p.monitoring_plan.update(root["id"], {"relation_aliases": ["合成系统全称"]}, actor="human")
    updated = next(w for w in p.monitor.watchpoints(pid) if w["id"] == child["id"])
    assert updated["relation_aliases"] == ["合成系统全称"]
    assert int(updated["lifecycle_version"]) > int(child["lifecycle_version"])
    assert any(c["watch_id"] == child["id"] and "relation_aliases" in c["after"]
               for c in p.monitoring_plan.view(pid)["changes"])


@pytest.mark.parametrize("aliases", [[], ["合成手部系统"]])
def test_child_state_update_preserves_inherited_alias_type(sample, aliases):
    p, _pid, root, child = sample
    p.monitoring_plan.update(root["id"], {"relation_aliases": aliases}, actor="human")
    updated = p.monitoring_plan.update(child["id"], {"current_state": "已核对许可出处"}, actor="human")
    assert updated["relation_aliases"] == aliases
    assert updated["current_state"] == "已核对许可出处"
    with pytest.raises(ValueError, match="继承父范围别名"):
        p.monitoring_plan.update(child["id"], {"relation_aliases": ["无关别名"]}, actor="human")


def test_body_arrival_wakes_once_without_waking_parent(sample):
    p, pid, root, child = sample
    plan = p.monitoring_plan
    item, _ = add_event(p, pid, "later", "", "unavailable")
    plan.explorations.refresh(pid)
    plan.explorations.stop(child, "answered")
    p.store.save_body(item, "article_body", "合成手部系统获得许可。")
    p.questions.route_existing_events(pid, [root["id"], child["id"]], focused=True)
    plan.explorations.refresh(pid)
    state = plan.explorations.view(pid)[child["id"]]
    assert state["state"] == "active" and state["reason"] == "new_evidence"
    plan.explorations.stop(child, "answered")
    plan.explorations.refresh(pid)
    assert plan.explorations.view(pid)[child["id"]]["state"] == "stopped"
    assert root["id"] not in plan.explorations.view(pid)


def test_withdrawn_match_not_shown_or_reused(sample):
    p, pid, root, child = sample
    item, eid = add_event(p, pid, "withdraw", "合成手部系统获得许可")
    p.questions.route_existing_events(pid, [root["id"], child["id"]], focused=True)
    p.store.save_body(item, "article_body", "另一家公司的产品取得进展")
    assert not p.questions.public_event_match(child, {}, {"id": eid})
    assert p.monitoring_plan.view(pid)["relation_updates"][root["id"]]["count"] == 0
    assert not p.monitoring_plan.explorations.tokens(child)


def test_full_saved_body_beyond_preview_reaches_dsh_context(sample):
    from project_watch.studio.dsh_monitor import PublicMonitor
    from project_watch.studio.execution import JobExecution

    p, pid, root, child = sample
    _, eid = add_event(p, pid, "long", "背景段落。" * 4000 + "合成手部系统获得许可。")
    p.store.enqueue(pid, "external")
    run = PublicMonitor(p, JobExecution(p.store, p.store.claim()), None, None)
    ctx = run.tool("pw_context", {})
    entry = next(e for e in ctx["numbered"].values() if e.get("id") == eid)
    assert {root["id"], child["id"]} <= set(entry["topic_ids"])
    assert len(entry["association_excerpts"]) == 1  # Shared excerpt, not duplicated per branch.
    assert "合成手部系统获得许可" in entry["association_excerpts"][0]["excerpt"]
    assert len(entry["association_excerpts"][0]["excerpt"]) < 600
    assert p.monitoring_plan.explorations.tokens(child)


def test_changed_body_becomes_pending_once_after_previous_assessment(sample):
    p, pid, _root, child = sample
    item, _ = add_event(p, pid, "revision", "合成手部系统正在申请许可。")
    p.questions.route_existing_events(pid, [child["id"]], focused=True)
    pending = p.questions.new_events(pid, child["id"])
    with p.monitor._db() as db, db:
        p.questions._consume(db, pid, child["id"], pending)
    assert not p.questions.new_events(pid, child["id"])
    p.store.save_body(item, "article_body", "合成手部系统已获许可。")
    with p.monitor._db() as db, db, pytest.raises(ValueError, match="changed before commit"):
        p.questions._consume(db, pid, child["id"], pending)
    assert p.questions.route_existing_events(pid, [child["id"]], focused=True) == 1
    assert p.questions.new_events(pid, child["id"])
    assert p.questions.route_existing_events(pid, [child["id"]], focused=True) == 0


def test_dismissed_and_other_project_events_do_not_route(sample):
    p, pid, root, child = sample
    _, eid = add_event(p, pid, "dismissed", "合成手部系统获得许可。")
    other = p.repository.create_project("其他合成公司").id
    p.store.upsert_project(other, {"legal_name": "其他合成公司"}, consent=True)
    _, other_event = add_event(p, other, "other", "合成手部系统获得许可。")
    with p.monitor._db() as db, db:
        db.execute("UPDATE studio_events SET status='dismissed' WHERE id=?", (eid,))
    assert p.questions.route_existing_events(pid, [child["id"]], focused=True) == 0
    assert not p.questions.public_event_match(child, {}, {"id": other_event})
    assert p.monitoring_plan.view(pid)["relation_updates"][root["id"]]["count"] == 0


@pytest.mark.parametrize("aliases", ["bad", ["A"], ["xx"], [None], ["正常别名"] * 9])
def test_invalid_aliases_rejected(sample, aliases):
    p, _, root, _ = sample
    with pytest.raises(ValueError, match="别名"):
        p.monitoring_plan.update(root["id"], {"relation_aliases": aliases}, actor="human")


@pytest.mark.parametrize("target,body", [
    ("合成接口相关价格项目", "合成接口相关医疗服务价格项目暂不纳入基本医保支付。"),
    ("合成接口相关医疗服务价格项目", "合成接口相关价格项目明确医保支付范围。"),
    ("合成接口相关价格项目", "合成接口相关服务纳入医疗服务价格项目，将符合条件的项目纳入医保支付范围。"),
    ("合成接口相关价格项目", "合成接口医疗服务价格项目明确最高限价，并不意味着已纳入医保支付。"),
])
def test_policy_wording_variant_wakes_stopped_child_once(tmp_path, target, body):
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project("合成公司").id
    p.store.upsert_project(pid, {"legal_name": "合成公司"}, consent=True)
    root = p.monitoring_plan.add(pid, {
        "text": "政策项目监控", "scope_kind": "relation", "relation_type": "policy",
        "relation_target": target, "reason": "影响合成产品收费", "sources": "https://example.com/policy",
        "queries": [target], "tier": "background", "cadence": "high", "axis": "rules",
    }, actor="human", origin="human", visibility="public")
    child = p.monitoring_plan.add(pid, {
        "text": "合成接口的基本医保支付范围如何？", "scope_kind": "question", "parent_id": root["id"],
        "queries": ["合成接口 支付"], "axis": "rules", "tier": "topic", "exit_rule": "明确当前支付范围",
    }, actor="human", origin="human", visibility="public")
    p.monitoring_plan.explorations.refresh(pid)
    p.monitoring_plan.explorations.stop(child, "answered")
    _, eid = add_event(p, pid, "policy", body, "search_summary")
    assert p.questions.route_existing_events(pid, [root["id"], child["id"]], focused=True) == 2
    p.monitoring_plan.explorations.refresh(pid)
    state = p.monitoring_plan.explorations.view(pid)[child["id"]]
    assert (state["state"], state["reason"], state["generation"]) == ("active", "new_evidence", 2)
    assert p.questions.public_event_match(child, {}, {"id": eid})["basis"] == "search_summary"
    p.monitoring_plan.explorations.stop(child, "answered")
    for _ in range(3):
        p.questions.route_existing_events(pid, [child["id"]], focused=True)
        p.monitoring_plan.explorations.refresh(pid)
    assert p.monitoring_plan.explorations.view(pid)[child["id"]]["state"] == "stopped"
    assert root["id"] not in p.monitoring_plan.explorations.view(pid)
    assert next(w for w in p.monitor.watchpoints(pid) if w["id"] == child["id"])["relation_aliases"] == []


def test_policy_variant_alias_and_negative_boundaries():
    watch = {"scope_kind": "question", "relation_type": "policy", "relation_target": "合成接口相关价格项目",
             "relation_aliases": ["示例治疗相关价格项目"], "text": "基本医保支付是否适用"}
    def match(text, w=watch):
        return QuestionLedger.public_row_match(w, {}, {"body_status": "search_summary", "body": text})
    assert match("示例治疗相关医疗服务价格项目已明确医保支付范围")
    assert not match("另一治疗相关医疗服务价格项目已明确医保支付范围")
    assert not match("合成接口产品团队获奖。其他公司医保支付项目获批")
    assert not match("合成接口相关医疗服务价格项目讨论。" + "背景内容。" * 100 + "其他项目医保支付")
    assert not match("合成接口相关医疗服务价格项目已明确医保支付范围", {**watch, "relation_type": "product"})
