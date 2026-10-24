"""Synthetic public monitoring, no paid services or running desktop state."""
import json
import os
from pathlib import Path

import pytest

from project_watch.studio.app import create_studio_app
from project_watch.studio.dsh_monitor import PublicMonitor
from project_watch.studio.execution import JobExecution


class Search:
    def web_search(self, query, count=6):
        return [{"url": "https://example.com/product", "title": "激光模组取得许可",
                 "snippet": "合成公司激光模组取得许可，量产仍在计划中。", "source": "Synthetic",
                 "published_at": "2026-01-01"}]


@pytest.fixture
def loop(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    p = pipeline.repository.create_project("合成公司")
    pipeline.store.upsert_project(p.id, {"legal_name": "合成公司"}, consent=True)
    pipeline.monitoring_plan.add(p.id, {"text": "激光模组是否取得许可", "objects": ["激光模组"],
        "axis": "asset", "queries": ["激光模组 许可"], "tier": "topic", "exit_rule": "明确取得许可"},
        actor="system", origin="system", visibility="public")
    pipeline.store.enqueue(p.id, "external")
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    return PublicMonitor(pipeline, execution, Search(), None)


def test_monitor_context_exposes_only_public_scope(loop):
    loop.pipeline.monitoring_plan.add(loop.project_id, {"text": "秘密客户是否签约"},
        actor="human", origin="human", visibility="internal")
    ctx = loop.tool("pw_context", {})
    assert len(ctx["watches"]) == 1
    assert "秘密客户" not in str(ctx)


def test_context_excludes_sleeping_topics_from_assessable_and_revision_scope(loop):
    watch = loop.watches()[0]
    loop.plan.explorations.stop(watch, "answered")
    ctx = loop.tool("pw_context", {})
    assert ctx["sleeping_question_ids"] == [watch["id"]]
    assert ctx["revision_blocked_ids"] == [watch["id"]]
    assert watch["id"] not in ctx["assessable_question_ids"]
    assert not ctx["required_assessments"]


def test_finish_requires_judgment_and_revision_review(loop):
    with pytest.raises(ValueError, match="judgment"):
        loop.tool("pw_finish", {})


def test_model_budget_hold_is_not_source_failure_or_no_gain(loop):
    from project_watch.studio.dsh_runtime import Gateway
    from project_watch.studio.http import Http, HttpError
    from project_watch.studio.settings import ModelConfig

    watch, _, _ = collect(loop)
    loop.execution.budgets["model"] = 0
    def forbidden(*args):
        pytest.fail("no HTTP expected")
    gateway = Gateway(loop, ModelConfig(base_url="https://example.invalid/v1",
                                       model="synthetic", api_key="synthetic"),
                      loop.execution.http(Http(forbidden), lambda _: ("model", "synthetic")))
    # Exercise budget exhaustion with a valid loaded-plugin request. An absent
    # tool list now correctly stops earlier as a local runtime failure.
    with pytest.raises(HttpError, match="request_budget"):
        gateway.model_call({"model": "synthetic", "messages": [],
                            "tools": [{"type": "function", "function": {"name": name}}
                                      for name in loop.tools]})
    state = loop.plan.explorations.view(loop.project_id)[watch["id"]]
    assert state["reason"] == "request_budget" and state["state"] == "waiting"
    assert state["no_gain"] == 0 and loop.plan.explorations.runnable(watch)
    loop.plan.explorations.finish(loop.project_id, loop.execution.job["id"])
    assert loop.plan.explorations.view(loop.project_id)[watch["id"]]["reason"] == "request_budget"


def test_missing_finish_text_is_actionable_and_retryable_before_writes(loop):
    from project_watch.studio.dsh_bootstrap import ToolValidationError

    watch, _, ref = collect(loop)
    judge(loop, ref)
    rows = [{"action": "note", "watch_id": watch["id"], "refs": [ref],
             "why": "证据补齐", "text": "激光模组取得许可"},
            {"action": "finish", "watch_id": watch["id"], "refs": [ref], "why": "已满足条件"}]
    before = loop.plan._get(loop.project_id, watch["id"])
    with pytest.raises(ToolValidationError) as error:
        loop.tool("pw_revise", {"changes_json": json.dumps(rows), "reason": "本轮结束"})
    assert error.value.feedback()["issues"][0]["field"] == "changes[1].text"
    assert not loop.state.get("reviewed")
    assert loop.plan._get(loop.project_id, watch["id"]) == before
    rows[1]["text"] = "激光模组取得许可，量产仍在计划中"
    result = loop.tool("pw_revise", {"changes_json": json.dumps(rows[1:]), "reason": "回答退出条件"})
    assert result["counts"]["applied"] == 1
    assert loop.tool("pw_finish", {"reason": "全部已结束"})["stopped_topic_ids"] == [watch["id"]]


def test_finish_reason_cannot_claim_active_topics_ended(loop):
    watch, _, ref = collect(loop)
    judge(loop, ref)
    loop.tool("pw_revise", {"changes_json": "[]", "reason": "继续跟踪"})
    result = loop.tool("pw_finish", {"reason": "全部专题已经结束"})
    assert result["active_topic_ids"] == [watch["id"]]
    assert result["topics_all_stopped"] is False
    assert result["reason"] != "全部专题已经结束"
    assert result["agent_reason"] == "全部专题已经结束"


def test_business_rejection_has_item_reason_and_real_closure_status(loop):
    watch, _, ref = collect(loop)
    loop.plan.update(watch["id"], {"exit_rule": ""}, actor="system", reason="legacy")
    loop.tool("pw_context", {})
    judge(loop, ref)
    revision = loop.tool("pw_revise", {"changes_json": json.dumps([{
        "action": "finish", "watch_id": watch["id"], "why": "done",
        "refs": [ref], "text": "激光模组取得许可"}]), "reason": "结束"})
    assert revision["items"][0]["error"] == "explicit exit rule required"
    result = loop.tool("pw_finish", {"reason": "已结束"})
    assert result["status"] == "partial"
    assert result["unfulfilled_finish_ids"] == [watch["id"]]


def test_analysis_only_can_reuse_exact_saved_judgment(loop, monkeypatch):
    _, _, ref = collect(loop)
    judge(loop, ref)
    loop.store.finish(loop.execution.job["id"], lease_token=loop.execution.job["lease_token"])
    loop.store.enqueue(loop.project_id, "insight")
    fresh = PublicMonitor(loop.pipeline, JobExecution(loop.store, loop.store.claim()),
                          Search(), None, analysis_only=True)
    monkeypatch.setattr(fresh.pipeline.insight_generator, "normalize",
                        lambda *a: pytest.fail("must not regenerate same judgment"))
    ctx = fresh.tool("pw_context", {})
    assert ctx["judgment"]["reused"] is True
    assert ctx["next_tool"] == "pw_revise"
    fresh.tool("pw_revise", {"changes_json": "[]", "reason": "无需更改"})
    assert fresh.tool("pw_finish", {"reason": "本轮完成"})["executor"] == "dsh"


@pytest.mark.parametrize("change", ["evidence", "scope", "policy", "settings", "analysis", "no_proof"])
def test_saved_judgment_reuse_is_strictly_invalidated(loop, change):
    from project_watch.studio import insight

    watch, _, ref = collect(loop)
    judge(loop, ref)
    loop.store.finish(loop.execution.job["id"], lease_token=loop.execution.job["lease_token"])
    if change == "evidence":
        add_public_event(loop, "new", "激光模组许可范围扩大")
    elif change == "scope":
        loop.plan.update(watch["id"], {"text": "激光模组何时量产"}, actor="human", reason="new")
    elif change == "settings":
        settings = loop.pipeline.settings_store.load()
        settings.model.model = "another-synthetic-model"
        loop.pipeline.settings_store.save(settings)
    elif change == "analysis":
        saved = insight.load(loop.store, loop.project_id)
        saved["analysis"]["headline"] = "人工修改后的判断"
        loop.store.set_state(insight.STATE_KEY.format(loop.project_id), json.dumps(saved))
    elif change == "no_proof":
        loop.store.set_state("dsh-judgment-proof:" + loop.project_id, "")
    loop.store.enqueue(loop.project_id, "insight")
    fresh = PublicMonitor(loop.pipeline, JobExecution(loop.store, loop.store.claim()),
                          Search(), None, analysis_only=True)
    if change == "policy":
        fresh.prompt += "\nchanged policy"
    ctx = fresh.tool("pw_context", {})
    assert ctx["judgment"]["reused"] is False
    assert not fresh.state.get("judged")


def test_collection_job_does_not_reuse_judgment_before_search(loop):
    _, _, ref = collect(loop)
    judge(loop, ref)
    loop.store.finish(loop.execution.job["id"], lease_token=loop.execution.job["lease_token"])
    loop.store.enqueue(loop.project_id, "external")
    fresh = PublicMonitor(loop.pipeline, JobExecution(loop.store, loop.store.claim()), Search(), None)
    assert fresh.tool("pw_context", {})["judgment"]["reused"] is False
    fresh.collecting()  # Context cannot prematurely lock collection.
    result = fresh.tool("pw_judge", {"analysis_json": "{}"})
    assert result["reused"] is True


def test_human_finish_proposal_is_not_reported_as_topic_stop(loop):
    watch, _, ref = collect(loop)
    loop.plan.update(watch["id"], {"text": "激光模组许可是否已确认"}, actor="human", reason="keep")
    loop.tool("pw_context", {})
    judge(loop, ref)
    loop.tool("pw_revise", {"changes_json": json.dumps([{
        "watch_id": watch["id"], "action": "finish", "why": "许可依据",
        "refs": [ref], "text": "激光模组取得许可"}]), "reason": "建议结束"})
    result = loop.tool("pw_finish", {"reason": "已停止"})
    assert result["unfulfilled_finish_ids"] == [watch["id"]]
    assert result["status"] == "partial" and not result["topics_all_stopped"]
    assert loop.plan._get(loop.project_id, watch["id"])["current_state"] != "激光模组取得许可"


@pytest.mark.parametrize("action", [None, [], {}, "delete"])
def test_invalid_revision_action_is_repairable_feedback(loop, action):
    from project_watch.studio.dsh_bootstrap import ToolValidationError

    watch, _, ref = collect(loop)
    judge(loop, ref)
    with pytest.raises(ToolValidationError) as error:
        loop.tool("pw_revise", {"changes_json": json.dumps([{
            "action": action, "watch_id": watch["id"], "refs": [ref], "why": "test"}]), "reason": "test"})
    assert error.value.feedback()["issues"][0]["code"] == "unsupported_revision"
    assert not loop.state.get("reviewed")


@pytest.mark.parametrize("kind,analysis_only", [
    ("external", False), ("external_followup", False), ("insight", True),
])
def test_pipeline_monitor_routes_without_legacy_fallback(loop, monkeypatch, kind, analysis_only):
    from project_watch.studio import dsh_runtime
    from project_watch.studio.http import HttpError

    monkeypatch.setenv("PROJECT_WATCH_DSH_RUNTIME", "synthetic-runtime")
    monkeypatch.setenv("PROJECT_WATCH_DSH_MONITOR", "1")
    loop.store.touch(loop.project_id, "last_external_at")
    calls = []
    def runner(pipeline, execution, root, **kwargs):
        calls.append((root.name, kwargs.get("analysis_only", False)))
        raise HttpError("request_outcome_unknown")
    monkeypatch.setattr(dsh_runtime, "run_monitor", runner)
    monkeypatch.setattr(loop.pipeline, "collect",
                        lambda *a, **k: pytest.fail("must not fall back to legacy collection"))
    with loop.store._connect() as db, db:
        db.execute("UPDATE studio_jobs SET kind=? WHERE id=?", (kind, loop.execution.job["id"]))
    job = {**loop.execution.job, "kind": kind}
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        loop.pipeline.run(job)
    assert calls == [("synthetic-runtime", analysis_only)]


def test_first_bootstrap_automatically_enqueues_monitor_followup(loop, monkeypatch):
    from project_watch.studio import dsh_runtime

    monkeypatch.setenv("PROJECT_WATCH_DSH_RUNTIME", "synthetic-runtime")
    monkeypatch.setenv("PROJECT_WATCH_DSH_MONITOR", "1")
    calls = []
    def bootstrap(pipeline, execution, root):
        calls.append("bootstrap")
        pipeline.store.touch(loop.project_id, "last_external_at")
        return {"status": "partial"}
    monkeypatch.setattr(dsh_runtime, "run_bootstrap", bootstrap)
    loop.pipeline.run(loop.execution.job)
    with loop.store._connect() as db:
        pending = db.execute("SELECT kind,status FROM studio_jobs WHERE id != ?",
                             (loop.execution.job["id"],)).fetchall()
    assert calls == ["bootstrap"]
    assert [tuple(row) for row in pending] == [("external_followup", "queued")]


def test_monitor_mode_does_not_duplicate_legacy_automatic_analysis(loop, monkeypatch):
    monkeypatch.setenv("PROJECT_WATCH_DSH_RUNTIME", "synthetic-runtime")
    monkeypatch.setenv("PROJECT_WATCH_DSH_MONITOR", "1")
    assert loop.pipeline.schedule_insight(loop.project_id) is None
    with loop.store._connect() as db:
        assert db.execute("SELECT count(*) FROM studio_jobs").fetchone()[0] == 1


def test_sleeping_topic_cannot_search(loop):
    watch = loop.tool("pw_context", {})["watches"][0]
    loop.pipeline.monitoring_plan.explorations.stop(watch, "answered")
    with pytest.raises(ValueError, match="not due"):
        loop.tool("pw_explore", {"watch_id": watch["id"], "query": watch["queries"][0]})


def collect(loop):
    watch = loop.tool("pw_context", {})["watches"][0]
    loop.tool("pw_explore", {"watch_id": watch["id"], "query": watch["queries"][0]})
    loop.tool("pw_record", {"kind": "company", "label": "激光模组许可", "source_id": "S1",
        "quote": "合成公司激光模组取得许可，量产仍在计划中。",
        "text": "合成公司激光模组取得许可，量产仍在计划中。"})
    ctx = loop.tool("pw_context", {})
    ref = next(k for k, e in ctx["numbered"].items() if e.get("id"))
    return watch, ctx, ref


@pytest.mark.parametrize("at_capacity", [False, True])
def test_relation_evidence_enters_judgment_without_forced_question_assessment(loop, monkeypatch, at_capacity):
    watch, _, _ = collect(loop)
    loop.plan.update(watch["id"], {"status": "retired"}, actor="human")
    loop.tool("pw_record", {
        "kind": "relation", "label": "激光模组", "source_id": "S1",
        "quote": "合成公司激光模组取得许可，量产仍在计划中。",
        "text": "合成公司激光模组取得许可，量产仍在计划中。",
        "relation_type": "product", "relation_target": "激光模组",
        "reason": "公司产品，跟踪量产进展", "axis": "asset", "query": "激光模组 量产"})
    ctx = loop.tool("pw_context", {})
    [rid] = ctx["scope"]["relation_ids"]
    assert ctx["required_assessments"] == [] and ctx["judge_ready"]
    assert rid not in ctx["assessable_question_ids"]
    assert ctx["research_branch_readiness"][rid]["status"] == "evidence_available"
    ref = ctx["watch_evidence"][rid][0]
    with pytest.raises(ValueError, match="standing relation"):
        loop.tool("pw_assess", {"watch_id": rid, "refs": [ref], "status": "progress", "note": "许可"})
    judge(loop, ref)
    if at_capacity:
        monkeypatch.setattr("project_watch.studio.dsh_monitor.MAX_ITEMS", 1)
    result = loop.tool("pw_revise", {"changes_json": json.dumps([{
        "watch_id": rid, "action": "explore", "text": "激光模组量产是否开始",
        "axis": "asset", "queries": ["激光模组 量产"], "exit_rule": "取得量产公告",
        "refs": [ref], "why": "公开信息仍为量产计划，核实是否兑现"}]), "reason": "从产品关联派生研究"})
    if at_capacity:
        assert result["counts"]["dropped"] == 1
        assert "standing monitoring remains enabled" in result["items"][0]["error"]
        assert len(loop.watches()) == 1
    else:
        assert result["counts"]["added"] == 1
        child = next(w for w in loop.watches() if w["id"] != rid)
        assert child["scope_kind"] == "question" and child["parent_id"] == rid
    finished = loop.tool("pw_finish", {"reason": "本轮结束"})
    assert finished["standing_relation_ids"] == [rid]
    assert rid not in finished["pending_question_ids"]
    assert rid not in loop.plan.explorations.view(loop.project_id)


def test_empty_relation_routing_explains_defer_not_retry(loop):
    from project_watch.studio.dsh_bootstrap import ToolValidationError
    _, _, ref = collect(loop)
    relation = loop.plan.add(loop.project_id, {
        "text": "另一产品", "scope_kind": "relation", "relation_type": "product",
        "relation_target": "另一产品", "reason": "合成对照", "axis": "asset",
        "sources": "https://example.com/other",
        "queries": ["另一产品 许可"], "tier": "background",
    }, actor="system", origin="system", visibility="public")
    ctx = loop.tool("pw_context", {})
    rid = relation["id"]
    assert ctx["watch_evidence"][rid] == []
    assert ctx["research_branch_readiness"][rid]["status"] == "missing_routed_evidence"
    with pytest.raises(ToolValidationError) as error:
        loop.tool("pw_assess", {"watch_id": rid, "refs": [ref], "note": "测试", "status": "insufficient"})
    assert error.value.issues[0]["code"] == "relation_not_assessable"
    judge(loop, ref)
    with pytest.raises(ToolValidationError) as error:
        loop.tool("pw_revise", {"changes_json": json.dumps([{
            "watch_id": rid, "action": "explore", "text": "另一产品是否许可", "axis": "asset",
            "queries": ["另一产品 许可"], "exit_rule": "许可公告", "refs": [ref], "why": "需要核实",
        }]), "reason": "测试"})
    issue = next(i for i in error.value.issues if i["code"] == "missing_routed_evidence")
    assert issue["allowed_refs"] == [] and issue["retry_with_same_context"] is False
    assert "reason" in issue["hint"]
    assert not any(w.get("parent_id") == rid for w in loop.watches())
    result = loop.tool("pw_revise", {"changes_json": "[]", "reason": "另一产品缺少关联事件，下轮补查"})
    assert not result["counts"]["added"]


def judge(loop, ref):
    for row in loop.pending_assessments(loop.frozen()):
        loop.tool("pw_assess", {"watch_id": row["watch_id"], "refs": row["refs"],
                               "status": "insufficient", "note": "合成测试：仍需后续公开材料"})
    return loop.tool("pw_judge", {"analysis_json": json.dumps({
        "headline": "取得许可，量产仍在计划中", "operations": {
            "verdict": "improving", "summary": "取得许可，量产仍在计划中",
            "for": [{"text": "取得许可，量产仍在计划中", "refs": [ref]}]}})})


def test_judgment_cannot_skip_pending_public_assessments(loop):
    watch, ctx, ref = collect(loop)
    assert ctx["required_assessments"] == [
        {"watch_id": watch["id"], "question": watch["text"], "refs": [ref]}]
    blocked = loop.tool("pw_judge", {"analysis_json": "{}"})
    assert blocked["saved"] is False and blocked["next_tool"] == "pw_assess"
    assert not loop.state.get("judged")
    # Insufficient is a valid assessment; no forced positive/negative claim.
    assert ctx["judge_ready"] is False
    result = loop.tool("pw_assess", {"watch_id": watch["id"], "refs": [ref],
                                   "status": "insufficient", "note": "仍需后续公开材料"})
    assert result["judge_ready"] is True and result["required_assessments"] == []
    assert result["next_tool"] == "pw_judge"
    assert judge(loop, ref)["saved"] is True


@pytest.mark.parametrize(('text', 'code'), [
    ('收入连续增长：2022-2024年营收8000万、7000万、1.2亿元', 'financial_series_trend_conflict'),
    ('甲公司2024年前三季度收入1亿元，同期乙公司全年收入1.2亿元', 'financial_comparison_period_mismatch'),
])
def test_financial_contradiction_rejected_before_save_then_corrected(loop, text, code):
    from project_watch.studio import insight
    from project_watch.studio.dsh_bootstrap import ToolValidationError

    _, ctx, ref = collect(loop)
    for row in loop.pending_assessments(ctx):
        loop.tool("pw_assess", {"watch_id": row["watch_id"], "refs": row["refs"],
                               "status": "insufficient", "note": "合成测试：仍需后续公开材料"})
    raw = {"operations": {"verdict": "improving", "for": [{
        "text": text, "refs": [ref]}]}}
    before = insight.load(loop.store, loop.project_id)
    with pytest.raises(ToolValidationError) as error:
        loop.tool("pw_judge", {"analysis_json": json.dumps(raw)})
    feedback = error.value.feedback()
    assert feedback['saved'] is False and feedback['next_tool'] == 'pw_judge'
    assert feedback['issues'][0]['field'] == 'operations.for.0.text'
    assert feedback['issues'][0]['code'] == code
    assert not loop.state.get('judged')
    assert insight.load(loop.store, loop.project_id) == before
    # The correction uses the already supplied evidence; no search/retry is needed.
    assert judge(loop, ref)['saved'] is True
    assert insight.load(loop.store, loop.project_id) is not None


def test_assessment_feedback_explains_routing_and_numeric_failures(loop):
    from project_watch.studio.dsh_bootstrap import ToolValidationError

    watch, ctx, ref = collect(loop)
    args = {"watch_id": watch["id"], "status": "progress", "note": "量产计划完成900台", "refs": ["W999"]}
    with pytest.raises(ToolValidationError) as error:
        loop.tool("pw_assess", args)
    assert error.value.feedback()["issues"][0]["allowed_refs"] == [ref]
    with pytest.raises(ToolValidationError) as error:
        loop.tool("pw_assess", {**args, "refs": [ref]})
    assert error.value.feedback()["issues"][0]["unsupported"] == ["900台"]
    assert loop.pending_assessments(ctx)  # Neither failure consumed evidence.


def test_assess_returns_remaining_refs_without_refetching_context(loop):
    watch, _, ref = collect(loop)
    add_public_event(loop, "second", "合成公司激光模组许可范围扩大")
    ctx = loop.tool("pw_context", {})
    result = loop.tool("pw_assess", {"watch_id": watch["id"], "refs": [ref],
                                   "status": "insufficient", "note": "仍需后续公开材料"})
    assert not result["judge_ready"] and result["next_tool"] == "pw_assess"
    assert result["required_assessments"] == loop.pending_assessments(ctx)
    assert ref not in result["required_assessments"][0]["refs"]


def test_invisible_old_pending_events_do_not_block_judgment(loop):
    watch, ctx, _ref = collect(loop)
    assert loop.pending_assessments(ctx)
    bounded = {**ctx, "numbered": {}}
    assert loop.pending_assessments(bounded) == []
    assert loop.pipeline.questions.new_events(loop.project_id, watch["id"])


def test_actual_stop_and_same_evidence_does_not_wake(loop):
    watch, _ctx, ref = collect(loop)
    loop.tool("pw_assess", {"watch_id": watch["id"], "refs": [ref],
                          "status": "progress", "change_basis": "object_change", "note": "激光模组取得许可，量产仍在计划中"})
    judge(loop, ref)
    changes = [{"action": "finish", "watch_id": watch["id"], "refs": [ref],
                "text": "激光模组取得许可，量产仍在计划中", "why": "退出条件已满足"}]
    receipt = loop.tool("pw_revise", {"changes_json": json.dumps(changes), "reason": "已有许可依据"})
    assert receipt["counts"]["applied"] == 1
    result = loop.tool("pw_finish", {"reason": "本轮完成"})
    assert result["explorations"][watch["id"]]["state"] == "stopped"
    assert result["explorations"][watch["id"]]["reason"] == "answered"
    loop.refresh()
    assert not loop.plan.explorations.runnable(watch)
    assert not loop.plan.due(loop.project_id)
    assert loop.tool("pw_finish", {}) == result
    with pytest.raises(ValueError, match="finished"):
        loop.tool("pw_search", {"query": "again", "category": "company"})


def test_new_routed_content_wakes_once(loop):
    watch, _ctx, _ref = collect(loop)
    loop.plan.explorations.stop(watch, "answered")
    old = loop.plan.explorations.view(loop.project_id)[watch["id"]]["generation"]
    loop.store.add_items(loop.project_id, "company", [{"key": "new", "title": "激光模组许可范围更新",
        "snippet": "新增许可适用范围", "url": "https://example.com/new",
        "source": "Synthetic", "published_at": "2026-01-02"}])
    item = loop.store.item_by_key(loop.project_id, "new")
    loop.store.save_triage([{"id": item["id"], "summary": "激光模组许可范围更新", "relevant": True}])
    loop.store.create_event(loop.project_id, {"title": "激光模组许可范围更新",
        "summary": "激光模组许可范围更新", "category": "company", "importance": "notable"}, [item["id"]])
    loop.refresh()
    state = loop.plan.explorations.view(loop.project_id)[watch["id"]]
    assert state["reason"] == "new_evidence" and state["generation"] == old + 1
    loop.refresh()
    assert loop.plan.explorations.view(loop.project_id)[watch["id"]]["generation"] == old + 1
    assert watch["id"] in {w["id"] for w in loop.plan.due(loop.project_id)}


def test_stopped_round_new_publication_wakes_then_naturally_stops_again(loop):
    """No SQL lifecycle manipulation: tool publication, routing, review, exit."""
    watch, _, ref = collect(loop)
    judge(loop, ref)
    def finish_round(research, citation):
        result = research.tool("pw_revise", {"changes_json": json.dumps([{
            "action": "finish", "watch_id": watch["id"], "refs": [citation],
            "text": "激光模组许可已取得", "why": "公开记录明确回答退出条件"}]), "reason": "已有公开依据"})
        assert result["counts"]["applied"] == 1
        return research.tool("pw_finish", {"reason": "本轮已回答"})
    first = finish_round(loop, ref)["explorations"][watch["id"]]
    assert first["state"] == "stopped" and first["reason"] == "answered"
    loop.store.finish(loop.execution.job["id"], lease_token=loop.execution.job["lease_token"])
    loop.store.enqueue(loop.project_id, "external")
    fresh = PublicMonitor(loop.pipeline, JobExecution(loop.store, loop.store.claim()), Search(), None)
    for _ in range(3):
        ctx = fresh.tool("pw_context", {})
        assert not ctx["due_ids"]
        assert ctx["explorations"][watch["id"]]["generation"] == first["generation"]
    # Ordinary incremental collection, not forcing a sleeping topic to explore.
    fresh.collectors.web_search = lambda *a, **kw: [{
        "url": "https://example.com/new-permit", "title": "合成公司激光模组许可范围扩大",
        "snippet": "合成公司激光模组许可范围扩大，覆盖新增适用场景。",
        "source": "Synthetic", "published_at": None}]
    sources = fresh.tool("pw_search", {"query": "合成公司 新进展", "category": "company"})
    sid = sources["sources"][0]["id"]
    fresh.tool("pw_record", {"kind": "company", "label": "许可范围更新", "source_id": sid,
        "quote": "合成公司激光模组许可范围扩大，覆盖新增适用场景。",
        "text": "合成公司激光模组许可范围扩大，覆盖新增适用场景。"})
    ctx = fresh.tool("pw_context", {})
    new = ctx["explorations"][watch["id"]]
    assert new["reason"] == "new_evidence" and new["generation"] == first["generation"] + 1
    again = fresh.tool("pw_context", {})
    assert again["explorations"][watch["id"]]["generation"] == new["generation"]
    ref = ctx["required_assessments"][0]["refs"][0]
    judge(fresh, ref)
    final = finish_round(fresh, ref)["explorations"][watch["id"]]
    assert final["state"] == "stopped" and final["reason"] == "answered"
    fresh.refresh()
    assert fresh.plan.due(fresh.project_id) == []


def add_public_event(loop, key, title):
    loop.store.add_items(loop.project_id, "company", [{"key": key, "title": title,
        "snippet": title, "url": f"https://example.com/{key}", "source": "Synthetic", "published_at": None}])
    item = loop.store.item_by_key(loop.project_id, key)
    loop.store.save_triage([{"id": item["id"], "summary": title, "relevant": True}])
    loop.store.create_event(loop.project_id, {"title": title, "summary": title,
        "category": "company", "importance": "notable"}, [item["id"]])
    return item["id"]


def test_industry_only_hit_neither_blocks_judgment_nor_wakes_topic(loop):
    watch = loop.plan.add(loop.project_id, {"text": "脑机接口医保支付落地进展",
        "axis": "rules", "queries": ["脑机接口 医保 支付标准"], "tier": "topic",
        "exit_rule": "公开支付标准和适用范围"}, actor="system", origin="system", visibility="public")
    loop.refresh()
    loop.plan.explorations.stop(watch, "answered")
    old = loop.plan.explorations.view(loop.project_id)[watch["id"]]["generation"]
    item = add_public_event(loop, "funding", "海外脑机接口公司完成融资")
    # A historical broad match stays in the ledger, but no longer wakes or
    # forces an irrelevant assessment. No user history is deleted.
    with loop.store._connect() as db, db:
        db.execute("INSERT INTO studio_watch_hits(watch_id,item_id,created_at) VALUES (?,?,?)",
                   (watch["id"], item, "2026-01-01"))
    ctx = loop.tool("pw_context", {})
    assert not loop.pending_events(watch)
    assert not ctx["watch_evidence"][watch["id"]]
    assert watch["id"] not in {r["watch_id"] for r in ctx["required_assessments"]}
    assert loop.plan.explorations.view(loop.project_id)[watch["id"]]["state"] == "stopped"
    # A fresh controller / ordinary finish path must not bypass the DSH policy.
    loop.plan.explorations.finish(loop.project_id, "another-cycle")
    assert loop.plan.explorations.view(loop.project_id)[watch["id"]]["state"] == "stopped"
    add_public_event(loop, "payment", "脑机接口医保支付标准发布")
    loop.refresh()
    state = loop.plan.explorations.view(loop.project_id)[watch["id"]]
    assert state["generation"] == old + 1 and state["reason"] == "new_evidence"
    loop.refresh()
    assert loop.plan.explorations.view(loop.project_id)[watch["id"]]["generation"] == old + 1


def test_legacy_exit_rule_can_be_supplied_but_not_claimed_answered(loop):
    watch, _, _ = collect(loop)
    loop.plan.update(watch["id"], {"exit_rule": ""}, actor="system", reason="synthetic legacy")
    ctx = loop.tool("pw_context", {})
    ref = next(k for k, e in ctx["numbered"].items() if e.get("id"))
    judge(loop, ref)
    result = loop.tool("pw_revise", {"changes_json": json.dumps([{
        "action": "set_exit", "watch_id": watch["id"], "refs": [ref],
        "why": "旧专题缺退出条件", "exit_rule": "公开许可适用范围可供核对"}]), "reason": "补齐明确条件"})
    assert result["counts"]["applied"] == 1
    current = loop.plan._get(loop.project_id, watch["id"])
    assert current["exit_rule"] == "公开许可适用范围可供核对"
    loop.refresh()
    assert loop.plan.explorations.view(loop.project_id)[watch["id"]]["reason"] != "answered"


def test_incremental_publication_keeps_unmentioned_competitors(loop):
    original = [{"name": f"Peer {i}", "reason": "已有依据"} for i in range(9)]
    loop.store.put_plan(loop.project_id, {"competitors": original, "custom": "keep"})
    collect(loop)
    assert loop.store.get_plan(loop.project_id)["competitors"] == original
    assert loop.store.get_plan(loop.project_id)["custom"] == "keep"


@pytest.mark.parametrize("question,query,irrelevant,relevant", [
    ("脑机接口医保支付", "脑机接口 医保", "脑机接口公司融资", "脑机接口报销标准公布"),
    ("科创板IPO注册节奏", "脑机接口 IPO", "脑机接口产品获批注册证", "公司提交科创板ipo申请"),
    ("销售费用率与亏损", "脑机接口 销售费用率", "脑机接口产品开始销售", "公司销售费用率下降"),
    ("激光模组量产许可", "激光模组 许可", "激光模组研发团队扩张", "激光模组许可已授予"),
])
def test_question_focus_not_industry_keyword(loop, question, query, irrelevant, relevant):
    watch = {"text": question, "label": question, "queries": [query], "criteria": ""}
    assert not loop.matches(watch, {"summary": irrelevant})
    assert loop.matches(watch, {"summary": relevant})


def test_unknown_cannot_be_bypassed_by_new_job(loop):
    from project_watch.studio.http import HttpError

    # Use the real durable call journal to create an ambiguous receipt.
    def lost():
        raise TimeoutError("synthetic unknown")
    with pytest.raises(TimeoutError):
        loop.execution.call("search", "synthetic", lost)
    loop.store.enqueue(loop.project_id, "insight")
    job = loop.store.claim()
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        PublicMonitor(loop.pipeline, JobExecution(loop.store, job), Search(), None)


def test_received_model_500_also_blocks_a_new_paid_round(loop):
    from project_watch.studio.http import HttpError, Response

    loop.execution.call("model", "server-error", lambda: Response(500, {}, b'{"error":"synthetic"}'))
    loop.store.enqueue(loop.project_id, "insight")
    job = loop.store.claim()
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        PublicMonitor(loop.pipeline, JobExecution(loop.store, job), Search(), None)


def test_stale_public_context_cannot_overwrite_human_edit(loop):
    watch, _ctx, ref = collect(loop)
    loop.plan.update(watch["id"], {"text": "人工更改的问题"}, actor="human", reason="test")
    with pytest.raises(ValueError, match="inputs changed"):
        judge(loop, ref)


def test_search_after_judgment_is_rejected(loop):
    _, _, ref = collect(loop)
    judge(loop, ref)
    with pytest.raises(ValueError, match="judgment"):
        loop.tool("pw_search", {"query": "more", "category": "company"})


def test_human_owned_scope_revision_is_only_proposal(loop):
    watch, _, ref = collect(loop)
    loop.plan.update(watch["id"], {"text": "激光模组何时量产"}, actor="human", reason="keep")
    loop.tool("pw_context", {})
    judge(loop, ref)
    receipt = loop.tool("pw_revise", {"changes_json": json.dumps([{
        "watch_id": watch["id"], "action": "reduce", "why": "等待量产",
        "refs": [ref]}]), "reason": "建议降频"})
    assert receipt["counts"]["proposed"] == 1
    assert loop.plan._get(loop.project_id, watch["id"])["status"] != "reduced"


def test_unrelated_ref_cannot_finish_topic(loop):
    watch, _ctx, ref = collect(loop)
    judge(loop, ref)
    with pytest.raises(ValueError):
        loop.tool("pw_revise", {"changes_json": json.dumps([{
            "watch_id": watch["id"], "action": "finish", "why": "done", "text": "done",
            "refs": ["N999"]}]), "reason": "done"})
    assert loop.plan.explorations.runnable(watch)


def test_known_search_failure_does_not_count_as_no_gain(loop):
    from project_watch.studio.http import HttpError

    watch = loop.tool("pw_context", {})["watches"][0]
    def denied(*args, **kwargs):
        raise HttpError("search_key_missing")
    loop.collectors.web_search = denied
    result = loop.tool("pw_explore", {"watch_id": watch["id"], "query": watch["queries"][0]})
    assert result["error"] == "search_key_missing"
    loop.plan.explorations.finish(loop.project_id, loop.execution.job["id"])
    state = loop.plan.explorations.view(loop.project_id)[watch["id"]]
    assert state["reason"] == "source_failure" and state["no_gain"] == 0


def test_assessment_consumes_only_the_cited_event_revision(loop):
    watch, _, ref = collect(loop)
    assert loop.pipeline.questions.new_events(loop.project_id, watch["id"])
    loop.tool("pw_assess", {"watch_id": watch["id"], "refs": [ref],
                         "status": "progress", "change_basis": "object_change", "note": "取得许可"})
    assert not loop.pipeline.questions.new_events(loop.project_id, watch["id"])


def test_relation_boundary_stops_instead_of_extending(loop):
    from project_watch.studio.monitoring_plan import MAX_HOP

    watch, _, ref = collect(loop)
    # Synthetic legacy root at its allowed depth; no live project is modified.
    with loop.store._connect() as db, db:
        db.execute("UPDATE studio_watchpoints SET hop=? WHERE id=?", (str(MAX_HOP), watch["id"]))
    loop.tool("pw_context", {})
    judge(loop, ref)
    result = loop.tool("pw_revise", {"changes_json": json.dumps([{
        "watch_id": watch["id"], "action": "explore", "refs": [ref], "why": "跟踪产业采用",
        "text": "激光模组量产是否完成", "axis": "asset", "queries": ["激光模组 量产"],
        "exit_rule": "取得量产公开记录"}]), "reason": "检查关联边界"})
    assert result["counts"]["added"] == 0
    assert loop.plan.explorations.view(loop.project_id)[watch["id"]]["reason"] == "relation_limit"


def test_dsh_stage_labels_project_to_real_phases():
    from project_watch.studio.runtime_health import stage_phase

    for tool, phase in (("pw_explore", "collect"), ("pw_context", "read"),
                        ("pw_assess", "judge"), ("pw_judge", "judge"), ("pw_revise", "revise")):
        assert stage_phase(PublicMonitor.labels[tool]) == phase


def test_refreshing_context_cannot_reuse_a_stale_judgment(loop):
    watch, _, ref = collect(loop)
    judge(loop, ref)
    loop.plan.update(watch["id"], {"text": "激光模组量产的新问题"}, actor="human", reason="new scope")
    loop.tool("pw_context", {})
    with pytest.raises(ValueError, match="judgment inputs changed"):
        loop.tool("pw_judge", {"analysis_json": "{}"})
    with pytest.raises(ValueError, match="judgment inputs changed"):
        loop.tool("pw_revise", {"changes_json": "[]", "reason": "no change"})


def test_incremental_collection_keeps_bootstrap_coverage(loop):
    original = {"people": {"status": "covered", "count": 5, "reason": ""}}
    loop.store.put_plan(loop.project_id, {"bootstrap_coverage": original, "bootstrap_status": "complete"})
    collect(loop)
    plan = loop.store.get_plan(loop.project_id)
    assert plan["bootstrap_coverage"] == original
    assert plan["bootstrap_status"] == "complete"


def test_search_summary_is_not_labelled_original_body(loop):
    _, ctx, ref = collect(loop)
    assert ctx["numbered"][ref]["context_kind"] == "搜索摘要"


def test_three_complete_empty_rounds_stop_not_partial_queries(loop):
    from datetime import UTC, datetime, timedelta

    watch, _, ref = collect(loop)
    loop.plan.explorations.finish(loop.project_id, loop.execution.job["id"])
    loop.store.finish(loop.execution.job["id"], lease_token=loop.execution.job["lease_token"])
    class Empty:
        def web_search(self, *args, **kwargs):
            return []
    for index in range(3):
        # Advance only the isolated cadence clock, never exploration counters.
        with loop.store._connect() as db, db:
            db.execute("UPDATE studio_watchpoints SET last_run_at=? WHERE id=?",
                       ((datetime.now(UTC) - timedelta(days=100)).isoformat(), watch["id"]))
        loop.store.enqueue(loop.project_id, "external", input_version=str(index))
        execution = JobExecution(loop.store, loop.store.claim())
        current = PublicMonitor(loop.pipeline, execution, Empty(), None)
        ctx = current.tool("pw_context", {})
        current.tool("pw_explore", {"watch_id": watch["id"], "query": watch["queries"][0]})
        ctx = current.tool("pw_context", {})
        ref = next(k for k, e in ctx["numbered"].items() if e.get("id"))
        judge(current, ref)
        current.tool("pw_revise", {"changes_json": "[]", "reason": "没有新线索"})
        current.tool("pw_finish", {"reason": "本轮查询集已完成，无新增线索"})
        current.store.finish(execution.job["id"], lease_token=execution.job["lease_token"])
    state = current.plan.explorations.view(loop.project_id)[watch["id"]]
    assert state["reason"] == "no_gain" and state["state"] == "stopped"
    assert state["no_gain"] == 3
    assert current.plan.due(loop.project_id) == []


@pytest.mark.skipif(not os.environ.get("PW_DSH_TEST_RUNTIME"), reason="requires pinned official DSH")
@pytest.mark.parametrize("repair_missing_text", [False, True])
def test_official_dsh_monitor_tool_loop(loop, tmp_path, repair_missing_text):
    from project_watch.studio.dsh_runtime import Gateway, run_dsh
    from project_watch.studio.http import Http, Response
    from project_watch.studio.settings import ModelConfig, SearchConfig
    from project_watch.studio.sources import Collectors

    calls = []
    searches = []
    def search_transport(*args):
        searches.append(1)
        return Response(200, {}, json.dumps({"results": [{"title": "激光模组取得许可",
            "url": "https://example.com/product", "summary": "合成公司许可进展",
            "content": "合成公司激光模组取得许可，量产仍在计划中。"}]}).encode())
    loop.collectors = Collectors(loop.execution.http(Http(search_transport),
        lambda _: ("search", "synthetic-search")), SearchConfig("deepseek", "synthetic"))
    watch = loop.watches()[0]
    def scripted(method, url, headers, raw, timeout):
        payload = json.loads(raw)
        assert {t["function"]["name"] for t in payload["tools"]} == set(loop.tools)
        record_schema = next(t["function"]["parameters"] for t in payload["tools"]
                             if t["function"]["name"] == "pw_record")
        kinds = record_schema["properties"]["kind"]["enum"]
        assert "company" in kinds and not set(kinds) & {"fact", "news", "watch"}
        ctx = loop.execution.read_checkpoint("dsh_monitor_context") or {}
        ref = next((k for k, e in ctx.get("numbered", {}).items() if e.get("id")), "N1")
        operations = [
            ("pw_context", {}),
            ("pw_explore", {"watch_id": watch["id"], "query": watch["queries"][0]}),
            ("pw_read", {"source_id": "S1"}),
            ("pw_record", {"kind": "company", "label": "许可", "source_id": "S1",
                          "quote": "合成公司激光模组取得许可，量产仍在计划中。",
                          "text": "合成公司激光模组取得许可，量产仍在计划中。"}),
            ("pw_context", {}),
            ("pw_assess", {"watch_id": watch["id"], "refs": [ref], "status": "progress", "change_basis": "object_change", "note": "取得许可"}),
            ("pw_judge", {"analysis_json": json.dumps({"headline": "取得许可", "operations": {
                "verdict": "improving", "for": [{"text": "取得许可", "refs": [ref]}]}})}),
            ("pw_revise", {"changes_json": json.dumps([{"action": "finish", "watch_id": watch["id"],
                "refs": [ref], "text": "取得许可", "why": "退出条件已满足"}]), "reason": "已有依据"}),
            ("pw_finish", {"reason": "本轮完成，专题答案已记录"}),
        ]
        if repair_missing_text:
            operations.insert(7, ("pw_revise", {"changes_json": json.dumps([{
                "action": "finish", "watch_id": watch["id"], "refs": [ref],
                "why": "退出条件已满足"}]), "reason": "已有依据"}))
        index = len(calls)
        if repair_missing_text and index == 8:
            feedback = json.loads(payload["messages"][-1]["content"])
            assert feedback["error"] == "revision_validation"
            assert feedback["issues"][0]["field"] == "changes[0].text"
            assert feedback["next_tool"] == "pw_revise"
        assert index < len(operations)
        calls.append(payload)
        tool, args = operations[index]
        return Response(200, {}, json.dumps({"choices": [{"message": {"role": "assistant",
            "content": None, "tool_calls": [{"id": f"step-{index}", "type": "function",
            "function": {"name": tool, "arguments": json.dumps(args)}}]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode())
    model = ModelConfig(api_key="synthetic", model="synthetic", base_url="https://example.invalid/v1")
    gateway = Gateway(loop, model, loop.execution.http(Http(scripted), lambda _: ("model", "synthetic")))
    result = run_dsh(loop, gateway, Path(os.environ["PW_DSH_TEST_RUNTIME"]), tmp_path / "sdk", timeout=120)
    assert len(calls) == 9 + int(repair_missing_text) and len(searches) == 1
    assert result["explorations"][watch["id"]]["state"] == "stopped"
    assert loop.execution.usage()["model"]["completed"] == len(calls)
    assert sum(e["tool"] == "pw_judge" for e in loop.state["events"]) == 1
