"""Competition/industry views refresh when related news arrives, not only weekly. Synthetic."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta


def _setup(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {"legal_name": "光启视界科技有限公司"}, consent=True)
    return store, project.id, Pipeline(repo, store, SettingsStore(tmp_path / "s.json"))


def _event(store, project_id, title, category, importance="notable"):
    store.add_items(project_id, category, [{"key": title, "title": title, "url": f"https://n.example/{title}",
                                            "source": "示例", "snippet": "", "published_at": None}])
    item = next(i for i in store.untriaged(project_id) if i["title"] == title)
    store.save_triage([{"id": item["id"], "relevant": True, "importance": importance, "summary": title}])
    store.create_event(project_id, {"title": title, "category": category, "importance": importance,
                                    "summary": title, "event_at": None}, [item["id"]])


def _age_landscape(store, project_id, hours):
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_landscape SET updated_at=? WHERE project_id=?",
                   ((datetime.now(UTC) - timedelta(hours=hours)).isoformat(), project_id))


def test_landscape_refreshes_after_new_competitor_or_industry_news(tmp_path):
    store, pid, pipeline = _setup(tmp_path)
    assert pipeline.landscape_stale(pid)  # never generated
    store.put_landscape(pid, {"summary": "旧版"})
    _age_landscape(store, pid, 30)
    assert not pipeline.landscape_stale(pid)  # nothing new since
    _event(store, pid, "竞品甲降价 20%", "competitor")
    assert pipeline.landscape_stale(pid)
    store.put_landscape(pid, {"summary": "新版"})
    _event(store, pid, "行业装车量增长", "industry")
    assert not pipeline.landscape_stale(pid)  # just regenerated: wait a few hours
    _age_landscape(store, pid, 7)
    assert pipeline.landscape_stale(pid)


def test_routine_or_company_news_does_not_trigger_a_refresh(tmp_path):
    store, pid, pipeline = _setup(tmp_path)
    store.put_landscape(pid, {"summary": "旧版"})
    _age_landscape(store, pid, 30)
    _event(store, pid, "公司参加论坛", "company")
    _event(store, pid, "竞品例行公告", "competitor", importance="routine")
    assert not pipeline.landscape_stale(pid)
