"""Sidebar signals ignore routine unread traffic; all fixtures stay synthetic."""

from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app


def test_routine_unread_and_old_read_major_do_not_alert(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成目标"}, "consent": True}).json()["id"]
    store = app.state.studio_store
    routine = store.create_event(pid, {"title": "普通行业资讯", "category": "industry", "importance": "routine"}, [])
    major = store.create_event(pid, {"title": "合成重大变化", "category": "company", "importance": "major"}, [])
    assert routine
    assert store.nav_alerts(pid) == ["有新的重大事件"]
    store.set_event_status(major, "read")
    assert store.nav_alerts(pid) == []
    [project] = client.get("/api/studio/projects").json()
    # Routine industry news is not "未读"; the read major item no longer counts either.
    assert project["unread"] == 0 and project["major_recent"] == 1 and project["nav_alerts"] == []
    store.create_event(pid, {"title": "竞品获批", "category": "competitor", "importance": "notable"}, [])
    store.create_event(pid, {"title": "行业政策解读", "category": "policy", "importance": "notable"}, [])
    store.create_event(pid, {"title": "旧的重大事件", "category": "company", "importance": "major",
                             "event_at": "2024-01-01T00:00:00+00:00"}, [])
    [project] = client.get("/api/studio/projects").json()
    assert project["unread"] == 1  # only the recent notable competitor news


def test_failure_remains_visible_until_same_kind_recovers(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    store = app.state.studio_store
    client = TestClient(app)
    pid = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成目标"}, "consent": True}).json()["id"]
    # Complete the initial external job with a failure, then let another kind succeed.
    store.finish(store.jobs(pid)[0]["id"], error="synthetic error")
    other = store.enqueue(pid, "brief")
    store.finish(other["id"])
    assert store.nav_alerts(pid) == ["外部更新未完成"]
    retry = store.enqueue(pid, "external")
    assert store.nav_alerts(pid) == ["外部更新未完成"]
    store.finish(retry["id"])
    assert store.nav_alerts(pid) == []


def test_important_open_crosscheck_alerts_until_resolved(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成目标"}, "consent": True}).json()["id"]
    signals = app.state.studio_pipeline.signals
    signals.replace_crosschecks(pid, [{"fingerprint": "synthetic", "kind": "mismatch", "severity": "high",
        "topic": "合成差异", "public_says": "a", "internal_says": "b", "question": "请核实", "public_refs": [], "internal_refs": []}])
    assert app.state.studio_store.nav_alerts(pid) == ["有重要信息差异，系统依据不足"]
    check = signals.crosschecks(pid)[0]
    signals.set_status(check["id"], "resolved")
    assert app.state.studio_store.nav_alerts(pid) == []
