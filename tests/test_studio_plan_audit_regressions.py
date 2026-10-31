"""Independent audit; synthetic temp databases only, workers disabled, no paid services."""
from types import SimpleNamespace

import pytest

from project_watch.studio.app import create_studio_app
from project_watch.studio.llm import ModelError
from project_watch.studio.monitoring_plan import _same_story


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
