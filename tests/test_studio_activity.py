"""Recent-activity counts that drive the header line field (synthetic data)."""

from __future__ import annotations


def test_activity_counts_recent_events_by_importance(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    assert store.activity(project.id) == {"days": 30, "major": 0, "notable": 0, "routine": 0}
    for n, (importance, when) in enumerate((("major", "2099-01-01T00:00:00Z"), ("notable", "2099-01-01T00:00:00Z"),
                                            ("major", "2000-01-01T00:00:00Z"))):
        store.create_event(project.id, {"title": f"事件{n}", "category": "company", "importance": importance,
                                        "summary": "", "event_at": when}, [])
    assert store.activity(project.id) == {"days": 30, "major": 1, "notable": 1, "routine": 0}
