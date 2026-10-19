"""Industry heat is a rule over countable signals, recomputed on read. Synthetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from project_watch.studio import landscape as ls


def _setup(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {"legal_name": "光启视界"}, consent=True)
    return store, project.id


def _events(store, pid, category, days_ago, n, importance="notable"):
    for i in range(n):
        title = f"{category}-{days_ago}-{i}"
        at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        store.add_items(pid, category, [{"key": title, "title": title, "url": f"https://n.example/{title}", "source": "s",
                                         "snippet": "", "published_at": at}])
        item = next(x for x in store.untriaged(pid) if x["title"] == title)
        store.save_triage([{"id": item["id"], "relevant": True, "importance": importance, "summary": title}])
        store.create_event(pid, {"title": title, "category": category, "importance": importance, "summary": title,
                                 "event_at": at}, [item["id"]])


def test_little_information_reads_neutral_not_uncertain(tmp_path):
    store, pid = _setup(tmp_path)
    _events(store, pid, "industry", 5, 2)
    heat = ls.heat_signals(store, pid, [], [])
    assert heat["label"] == "中性" and "较少" in heat["note"]


def test_rising_activity_reads_warming(tmp_path):
    store, pid = _setup(tmp_path)
    _events(store, pid, "industry", 60, 3)
    _events(store, pid, "industry", 5, 6)
    _events(store, pid, "competitor", 70, 3)
    _events(store, pid, "competitor", 3, 5)
    today = datetime.now(UTC).date().isoformat()
    heat = ls.heat_signals(store, pid, [{"date": today}, {"date": today}, {"date": today}], [])
    by_key = {s["key"]: s for s in heat["signals"]}
    assert by_key["news"]["recent"] == 6 and by_key["news"]["baseline"] == 1.0 and by_key["news"]["trend"] == "up"
    assert heat["label"] == "升温"


def test_falling_activity_reads_cooling(tmp_path):
    store, pid = _setup(tmp_path)
    _events(store, pid, "industry", 50, 9)
    _events(store, pid, "competitor", 80, 9)
    _events(store, pid, "industry", 10, 1)
    assert ls.heat_signals(store, pid, [], [])["label"] == "降温"


def test_detail_recomputes_heat_on_read(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    c = TestClient(app)
    pid = c.post("/api/studio/projects", json={"entity": {"legal_name": "光启视界"}, "consent": True}).json()["id"]
    store = app.state.studio_store
    store.put_landscape(pid, {"industry": {"heat": "不确定", "funding": []}, "moves": []})
    detail = c.get(f"/api/studio/projects/{pid}").json()
    assert detail["landscape"]["industry"]["heat_index"]["label"] == "中性"
