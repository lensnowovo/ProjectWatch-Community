"""Synthetic read-only tree signals. No providers or background workers."""
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from project_watch.studio.plan_tree import evidence_signal


def test_no_receipt_never_lights_a_note_or_configuration_edit():
    assert evidence_signal([])["count"] == 0
    assert not evidence_signal([])["fresh"]


def test_distinct_receipts_and_recorded_clock_not_publication_or_edit_time():
    now = datetime.now(UTC)
    old = (now - timedelta(days=30)).isoformat()
    rows = [{"id": "e1", "title": "old event", "recorded_at": old, "date": now.isoformat()},
            {"id": "e1", "title": "duplicate", "recorded_at": now.isoformat()}]
    value = evidence_signal(rows)
    assert value["count"] == 1
    assert not value["fresh"]
    assert value["recorded_at"] == old


def test_future_invalid_and_missing_dates_do_not_claim_new():
    for date in ("", "invalid", (datetime.now(UTC) + timedelta(days=1)).isoformat()):
        assert not evidence_signal([{"id": "e", "title": "stored", "recorded_at": date}])["fresh"]
    value = evidence_signal([{"id": "e", "title": "stored", "recorded_at": datetime.now(UTC).isoformat()}])
    assert value["count"] == 1 and value["fresh"]


def test_current_state_without_hits_does_not_become_new_after_cadence_edit():
    from project_watch.studio.monitoring_plan import MonitoringPlan
    p = object.__new__(MonitoringPlan)
    p.monitor = SimpleNamespace(_db=lambda: nullcontext(None))
    p._hits = lambda *args, **kwargs: []
    p._focused = lambda *args: []
    w = {"id": "synthetic", "current_state": "尚无材料，待核实", "updated_at": datetime.now(UTC).isoformat()}
    assert p.latest("p", w)["fresh"] is False
    assert p.latest("p", w)["date"] == ""


def test_tree_projection_preserves_stopped_branch_and_excludes_dismissed_events(tmp_path):
    from project_watch.studio.app import create_studio_app

    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project("合成公司").id
    p.store.upsert_project(pid, {"legal_name": "合成公司"}, consent=True)
    plan = p.monitoring_plan
    parent = plan.add(pid, {"text": "关注合成光学路线", "scope_kind": "relation",
        "relation_type": "technology", "relation_target": "合成光学路线", "reason": "采用该路线",
        "sources": "https://example.com/route", "queries": ["合成光学路线"],
        "axis": "science", "tier": "background", "cadence": "high"},
        actor="system", origin="bootstrap", visibility="public")
    child = plan.add(pid, {"text": "合成光学路线是否取得许可", "scope_kind": "question",
        "parent_id": parent["id"], "tier": "topic", "exit_rule": "许可公告出现",
        "queries": ["合成光学路线 许可"]}, actor="system", origin="bootstrap", visibility="public")
    title = "合成光学路线取得许可"
    p.store.add_items(pid, "company", [{"key": "synthetic", "title": title,
        "url": "https://example.com/e", "source": "synthetic", "snippet": title, "published_at": None}])
    item = p.store.item_by_key(pid, "synthetic")
    p.store.create_event(pid, {"title": title, "importance": "notable", "category": "company"}, [item["id"]])
    p.questions.route_existing_events(pid, {parent["id"], child["id"]}, focused=True)
    plan.explorations.refresh(pid)
    plan.explorations.stop(child, "answered")
    with p.store._connect() as db:
        before = list(db.iterdump())
    value = plan.view(pid)
    assert value["tree_signals"][parent["id"]]["count"] == 1
    assert value["tree_signals"][child["id"]]["fresh"]
    assert value["explorations"][child["id"]]["state"] == "stopped"
    with p.store._connect() as db:
        assert before == list(db.iterdump())
        db.execute("UPDATE studio_events SET status='dismissed' WHERE project_id=?", (pid,))
    value = plan.view(pid)
    assert value["tree_signals"][parent["id"]]["count"] == 0
    assert value["tree_signals"][child["id"]]["count"] == 0
