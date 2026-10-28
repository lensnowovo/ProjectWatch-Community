"""Synthetic relation-first scope, without networking or desktop state."""
import pytest

from project_watch.studio.app import create_studio_app
from project_watch.studio.questions import QuestionLedger

RELATION = {
    "text": "关注合成光学路线", "scope_kind": "relation", "relation_type": "technology",
    "relation_target": "合成光学路线", "reason": "产品采用该路线，关注路线演进",
    "sources": "https://example.com/route", "queries": ["合成光学路线 新进展"],
    "axis": "science", "tier": "background", "cadence": "high",
}


@pytest.fixture
def scope(tmp_path):
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = p.repository.create_project("合成公司")
    p.store.upsert_project(project.id, {"legal_name": "合成公司"}, consent=True)
    return p, project.id


def add(plan, pid, fields):
    return plan.add(pid, fields, actor="system", origin="bootstrap", visibility="public")


def test_relation_is_persistent_scope_not_temporary_question(scope):
    p, pid = scope
    plan = p.monitoring_plan
    relation = add(plan, pid, RELATION)
    assert relation["scope_kind"] == "relation"
    assert not plan.explorations.managed(relation)
    for _ in range(4):
        plan.explorations.refresh(pid)
    assert relation["id"] not in plan.explorations.view(pid)
    assert relation["id"] in {w["id"] for w in plan.due(pid)}
    assert plan.view(pid)["scope"]["relation_ids"] == [relation["id"]]
    assert QuestionLedger.public_focus_matches(relation, {}, "其他公司采用合成光学路线")
    assert not QuestionLedger.public_focus_matches(relation, {}, "合成公司取得医疗器械注册")


@pytest.mark.parametrize("patch", [
    {"relation_type": "invented"}, {"relation_target": ""},
    {"reason": ""}, {"sources": ""}, {"queries": []},
    {"tier": "topic"}, {"cadence": "event"}, {"exit_rule": "取得许可"},
])
def test_incomplete_or_question_shaped_relation_rejected(scope, patch):
    p, pid = scope
    with pytest.raises(ValueError):
        add(p.monitoring_plan, pid, {**RELATION, **patch})


def test_duplicate_target_cannot_be_hidden_by_different_title(scope):
    p, pid = scope
    plan = p.monitoring_plan
    add(plan, pid, RELATION)
    with pytest.raises(ValueError, match="关联"):
        add(plan, pid, {**RELATION, "text": "同路线的新标题"})


def test_new_questions_need_parent_and_exit_legacy_unchanged(scope):
    p, pid = scope
    plan = p.monitoring_plan
    legacy = add(plan, pid, {"text": "历史关注问题", "tier": "topic"})
    assert legacy["scope_kind"] == "" and plan.explorations.managed(legacy)
    assert legacy["id"] in plan.view(pid)["scope"]["legacy_ids"]
    relation = add(plan, pid, RELATION)
    question = {"text": "合成光学路线是否获得许可", "scope_kind": "question",
                "tier": "topic", "queries": ["合成光学路线 许可"]}
    with pytest.raises(ValueError):
        add(plan, pid, question)
    child = add(plan, pid, {**question, "parent_id": relation["id"], "exit_rule": "许可公告出现"})
    assert child["relation_target"] == relation["relation_target"]
    assert not QuestionLedger.public_focus_matches(child, {}, "其他药物获得许可")
    plan.explorations.refresh(pid)
    assert plan.explorations.stop(child, "answered")
    assert plan.explorations.view(pid)[child["id"]]["state"] == "stopped"
    assert relation["id"] in {w["id"] for w in plan.due(pid)}
    assert child["id"] not in {w["id"] for w in plan.due(pid)}
    for _ in range(3):
        plan.explorations.refresh(pid)
    assert plan.explorations.view(pid)[child["id"]]["state"] == "stopped"
    assert relation["id"] not in plan.explorations.view(pid)
    with pytest.raises(ValueError, match="分支"):
        plan.update(relation["id"], {"relation_target": "其他路线"}, actor="human")


def test_new_evidence_wakes_question_not_whole_relation_graph(scope):
    p, pid = scope
    plan = p.monitoring_plan
    parent = add(plan, pid, RELATION)
    child = add(plan, pid, {"text": "合成光学路线是否取得许可", "scope_kind": "question",
        "parent_id": parent["id"], "tier": "topic", "exit_rule": "许可公告",
        "queries": ["合成光学路线 许可"]})
    plan.explorations.refresh(pid)
    plan.explorations.stop(child, "answered")

    def event(key, title):
        p.store.add_items(pid, "company", [{"key": key, "title": title, "url": "https://example.com/" + key,
                                         "source": "synthetic", "snippet": title, "published_at": None}])
        item = p.store.item_by_key(pid, key)
        p.store.create_event(pid, {"title": title, "importance": "notable", "category": "company"}, [item["id"]])
        p.questions.route_existing_events(pid, {child["id"], parent["id"]}, focused=True)
        plan.explorations.refresh(pid)

    event("other", "另一家公司取得许可")
    assert plan.explorations.view(pid)[child["id"]]["state"] == "stopped"
    event("related", "合成光学路线取得许可")
    state = plan.explorations.view(pid)[child["id"]]
    assert state["state"] == "active" and state["reason"] == "new_evidence"
    for _ in range(3):
        p.questions.route_existing_events(pid, {child["id"], parent["id"]}, focused=True)
        plan.explorations.refresh(pid)
    assert plan.explorations.view(pid)[child["id"]]["generation"] == state["generation"]
    assert parent["id"] not in plan.explorations.view(pid)


def test_relation_cannot_be_retyped_implicitly_by_legacy_editor(scope):
    p, pid = scope
    plan = p.monitoring_plan
    relation = add(plan, pid, RELATION)
    with pytest.raises(ValueError):
        plan.update(relation["id"], {"scope_kind": ""}, actor="human")
    with pytest.raises(ValueError):
        plan.update(relation["id"], {"tier": "topic"}, actor="human")
    changed = plan.update(relation["id"], {"relation_target": "合成声学路线"}, actor="human")
    assert int(changed["lifecycle_version"]) > int(relation["lifecycle_version"])
