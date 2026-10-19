"""Independent audit; synthetic temp databases only, workers disabled, no paid services."""
from types import SimpleNamespace

import pytest

from project_watch.studio.app import create_studio_app
from project_watch.studio.llm import ModelError
from project_watch.studio.monitoring_plan import _same_story
from project_watch.studio.pipeline import PLAN_QUERY_LIMIT


@pytest.fixture
def env(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipe = app.state.studio_pipeline
    pid = pipe.repository.create_project("合成审计公司").id
    pipe.store.upsert_project(pid, {"legal_name": "合成审计公司"}, consent=True)
    return pipe, pid, pipe.monitoring_plan


def add(plan, pid, text, **kwargs):
    return plan.add(pid, {"text": text, "axis": "technology", "queries": [text],
                          "tier": "background", "cadence": "high", **kwargs}, actor="human", origin="human")


class Collector:
    has_search = False
    def __init__(self):
        self.seen = []
    def news(self, query, search_count=0):
        self.seen.append(query)
        return []


def test_background_eventually_gets_a_turn(env):
    pipe, pid, plan = env
    for n in range((PLAN_QUERY_LIMIT + 3) // 4):
        add(plan, pid, f"核心问题{n}", tier="core", queries=[f"核心{n}-{i}" for i in range(4)])
    add(plan, pid, "背景问题", queries=["背景检索"])
    collector = Collector()
    for _ in range(5):
        pipe._collect_monitor(pid, pipe.store.get_project(pid), collector, lambda _: None)
    assert "背景检索" in collector.seen, f"background starved; {len(collector.seen)} calls all core"


def test_partial_query_batch_does_not_mark_weekly_item_complete(env):
    pipe, pid, plan = env
    sizes = [4] * ((PLAN_QUERY_LIMIT - 1) // 4)
    if (PLAN_QUERY_LIMIT - 1) % 4:
        sizes.append((PLAN_QUERY_LIMIT - 1) % 4)
    for n, size in enumerate(sizes):
        add(plan, pid, f"核心问题{n}", tier="core", queries=[f"核心{n}-{i}" for i in range(size)])
    weekly = add(plan, pid, "每周问题", cadence="medium", queries=[f"周查询{i}" for i in range(4)])
    collector = Collector()
    pipe._collect_monitor(pid, pipe.store.get_project(pid), collector, lambda _: None)
    done = [q for q in collector.seen if q.startswith("周查询")]
    due = [w["id"] for w in plan.due(pid)]
    assert len(done) == 4 or weekly["id"] in due, f"only {done} searched but remaining queries deferred for days"


def test_human_confirmation_protects_system_trial(env):
    _, pid, plan = env
    trial = plan.add(pid, {"text": "系统发现的问题", "tier": "background", "queries": ["公开产品"]},
                     actor="system", origin="system")
    plan.update(trial["id"], {"status": "active"}, actor="human", reason="确认系统建议")
    with pytest.raises(PermissionError):
        plan.update(trial["id"], {"status": "retired"}, actor="system", reason="系统建议结束")


def test_revert_first_section_change_removes_added_sections(env):
    _, pid, plan = env
    plan.set_meta(pid, {"sections": {"technology": {"known": "合成技术", "coverage": "partial"}}},
                  actor="human", reason="首版")
    change = next(c for c in plan.view(pid)["changes"] if c["action"] == "meta")
    plan.revert(change["id"])
    assert not plan.meta(pid).get("sections"), "revert left the new section content behind"


def test_distinct_same_day_milestones_both_survive(env):
    pipe, pid, plan = env
    item = add(plan, pid, "技术进展", view="timeline")
    titles = ["合成资产完成首例受试者给药", "研究发现新的靶点机制与耐药通路"]
    pipe.store.add_items(pid, "industry", [{"key": str(n), "title": t, "url": f"https://audit.example/{n}",
        "source": "synthetic", "snippet": "", "published_at": "2026-10-14"} for n, t in enumerate(titles)])
    rows = pipe.store.untriaged(pid)
    pipe.store.save_triage([{"id": r["id"], "relevant": True, "importance": "notable", "summary": "", "impact": ""} for r in rows])
    plan.monitor.add_hits([(item["id"], r["id"]) for r in rows])
    points = plan.module(pid, item)["points"]
    assert len(points) == 2, f"different same-day events collapsed: {points}"


def test_replan_unknown_request_is_not_swallowed(env):
    pipe, pid, plan = env
    add(plan, pid, "需修订的问题")
    def unknown(*args, **kwargs):
        raise ModelError("synthetic unknown outcome", code="request_outcome_unknown")
    pipe.plan_reviser = SimpleNamespace(run=unknown, since_last=lambda _: "2026-01-01", mark=lambda *_: None)
    # Use the actual two-argument signature.
    pipe.plan_reviser.since_last = lambda *_: "2026-01-01"
    with pytest.raises(ModelError):
        pipe._replan(pid, SimpleNamespace(ready=True), lambda _: None, trigger="external")


def test_dedup_does_not_erase_a_later_negative_update():
    assert not _same_story("合成药物临床试验获批启动", "合成药物临床试验因安全问题暂停")


def test_partial_queries_resume_after_reopen_without_repeating_successes(env, monkeypatch):
    from project_watch.studio import pipeline as pipeline_module
    from project_watch.studio.monitor import MonitorStore
    from project_watch.studio.monitoring_plan import MonitoringPlan

    pipe, pid, plan = env
    watch = add(plan, pid, "每周续跑", cadence="medium", queries=["查询一", "查询二", "查询三"])
    monkeypatch.setattr(pipeline_module, "PLAN_QUERY_LIMIT", 1)
    collector = Collector()
    for index in range(3):
        reopened = MonitoringPlan(MonitorStore(pipe.store))
        assert reopened.query_jobs(pid, 1)[0][1] == f"查询{'一二三'[index]}"
        pipe._collect_monitor(pid, pipe.store.get_project(pid), collector, lambda _: None)
    assert collector.seen == ["查询一", "查询二", "查询三"]
    assert watch["id"] not in {w["id"] for w in plan.due(pid)}
    assert plan._get(pid, watch["id"])["last_run_at"]


def test_failed_query_remains_pending_but_successful_siblings_do_not(env):
    pipe, pid, plan = env
    watch = add(plan, pid, "失败后补齐", cadence="medium", queries=["正常查询", "失败查询"])
    collector = Collector()
    def fail_once(query, search_count=0):
        collector.seen.append(query)
        if query == "失败查询":
            raise ValueError("synthetic failure")
        return []
    collector.news = fail_once
    pipe._collect_monitor(pid, pipe.store.get_project(pid), collector, lambda _: None)
    assert not plan._get(pid, watch["id"])["last_run_at"]
    assert [q for _, q in plan.query_jobs(pid, 30)] == ["失败查询"]
    plan.update(watch["id"], {"queries": ["新的查询"]}, actor="human")
    assert [q for _, q in plan.query_jobs(pid, 30)] == ["新的查询"]


def test_failed_core_does_not_starve_background_with_one_slot(env, monkeypatch):
    from project_watch.studio import pipeline as pipeline_module

    pipe, pid, plan = env
    add(plan, pid, "失败核心", tier="core")
    add(plan, pid, "可用背景")
    monkeypatch.setattr(pipeline_module, "PLAN_QUERY_LIMIT", 1)
    collector = Collector()
    def failing(query, search_count=0):
        collector.seen.append(query)
        raise ValueError("synthetic failure")
    collector.news = failing
    for _ in range(2):
        pipe._collect_monitor(pid, pipe.store.get_project(pid), collector, lambda _: None)
    assert collector.seen == ["失败核心", "可用背景"]


@pytest.mark.parametrize("code", ["request_outcome_unknown", "request_budget", "provider_backoff"])
def test_collection_control_signal_stops_dispatch(env, code):
    from project_watch.studio.http import HttpError

    pipe, pid, plan = env
    watch = add(plan, pid, "中断查询", queries=["未知查询", "不要执行"])
    collector = Collector()
    def stop(query, search_count=0):
        collector.seen.append(query)
        raise HttpError(code)
    collector.news = stop
    with pytest.raises(HttpError):
        pipe._collect_monitor(pid, pipe.store.get_project(pid), collector, lambda _: None)
    assert collector.seen == ["未知查询"]
    assert not plan._get(pid, watch["id"])["last_run_at"]


@pytest.mark.parametrize("code", ["request_budget", "provider_backoff", "model_timeout"])
def test_replan_control_signal_stops_without_marking_success(env, code):
    pipe, pid, plan = env
    add(plan, pid, "恢复控制")
    marks = []
    def stop(*args, **kwargs):
        raise ModelError("synthetic hold", code=code)
    pipe.plan_reviser = SimpleNamespace(run=stop, since_last=lambda *_: "", mark=lambda *_: marks.append(True))
    with pytest.raises(ModelError):
        pipe._replan(pid, SimpleNamespace(ready=True), lambda _: None, trigger="internal")
    assert marks == []


def test_confirmation_keeps_origin_and_routes_system_retirement_to_proposal(env):
    from project_watch.studio import plan_replan

    _, pid, plan = env
    trial = plan.add(pid, {"text": "系统建议的问题", "queries": ["测试"]}, actor="system", origin="system")
    confirmed = plan.update(trial["id"], {"status": "active", "cadence": "high"}, actor="human")
    assert confirmed["plan_origin"] == "system" and confirmed["origin"] == "ai" and confirmed["reviewed_at"]
    with pytest.raises(PermissionError):
        plan.update(trial["id"], {"cadence": "low"}, actor="system")
    result = plan_replan.apply(plan, pid, [{"action": "retire", "item": "W1", "refs": ["E1"], "why": "test"}],
        {"items": {"W1": confirmed}, "numbered": {"E1": {"title": "合成依据"}}}, trigger="external")
    assert result["proposed"] == 1 and plan._get(pid, trial["id"])["status"] == "active"


def test_meta_revert_preserves_unrelated_new_fields_and_rejects_stale_change(env):
    _, pid, plan = env
    plan.set_meta(pid, {"method": "旧方法"}, actor="human", reason="初版")
    change = plan.view(pid)["changes"][0]
    plan.set_meta(pid, {"understanding": "新理解"}, actor="human", reason="补充")
    plan.revert(change["id"])
    assert plan.meta(pid)["understanding"] == "新理解" and "method" not in plan.meta(pid)
    plan.set_meta(pid, {"method": "方法一"}, actor="human", reason="初版")
    change = plan.view(pid)["changes"][0]
    plan.set_meta(pid, {"method": "方法二"}, actor="human", reason="后续修改")
    with pytest.raises(ValueError, match="后续修改"):
        plan.revert(change["id"])
    assert plan.meta(pid)["method"] == "方法二"


@pytest.mark.parametrize("value", ["", "NaN", "Infinity", None, True])
def test_metric_api_rejects_missing_or_non_numeric_values(env, value):
    from pydantic import ValidationError

    from project_watch.studio.monitor_api import MetricPointIn

    _, pid, plan = env
    watch = add(plan, pid, "数值测试", view="metric_series")

    with pytest.raises(ValidationError):
        MetricPointIn(period="2026-10", value=value)
    assert plan.module(pid, watch)["points"] == []


def test_metric_zero_is_valid_and_persists(env):
    from project_watch.studio.monitor_api import MetricPointIn

    _, pid, plan = env
    watch = add(plan, pid, "零值测试", view="metric_series")
    body = MetricPointIn(period="2026-10", value=0)
    plan.add_point(watch["id"], **body.model_dump())
    assert plan.module(pid, watch)["points"][0]["value"] == 0


def test_upgrade_preserves_previous_human_confirmations(env):
    from project_watch.studio.monitor import MonitorStore
    from project_watch.studio.monitoring_plan import MonitoringPlan

    pipe, pid, plan = env
    trial = plan.add(pid, {"text": "升级前已确认的问题"}, actor="system", origin="system")
    plan.update(trial["id"], {"status": "active"}, actor="human")
    with plan.monitor._db() as db, db:
        db.execute("ALTER TABLE studio_watchpoints DROP COLUMN reviewed_at")
    upgraded = MonitoringPlan(MonitorStore(pipe.store))
    watch = upgraded._get(pid, trial["id"])
    assert watch["reviewed_at"] and watch["plan_origin"] == "system"
    with pytest.raises(PermissionError):
        upgraded.update(watch["id"], {"status": "retired"}, actor="system")


def test_same_title_on_different_dates_is_not_hidden(env):
    _, pid, plan = env
    watch = add(plan, pid, "每月进展")
    hits = [{"title": "合成项目季度进展", "date": day} for day in ("2026-09-01", "2026-10-01")]
    assert len(plan._focused(pid, watch, hits)) == 2
