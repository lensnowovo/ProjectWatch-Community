"""An item dated ahead of today (a journal's scheduled issue, a planned event) is never the "latest" news."""

from __future__ import annotations

from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app


def test_a_future_dated_event_does_not_become_the_projects_latest(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成药业"}, "consent": True}).json()["id"]
    store = app.state.studio_store
    store.create_event(pid, {"title": "刊期在年底的合成论文", "category": "paper", "importance": "notable",
                             "event_at": "2099-12-15T00:00:00+00:00"}, [])
    store.create_event(pid, {"title": "今天的合成新闻", "category": "company", "importance": "notable",
                             "event_at": "2000-01-02T00:00:00+00:00"}, [])
    row = next(p for p in store.list_projects() if p["id"] == pid)
    assert not row["last_event_at"].startswith("2099")  # capped at when it was recorded
