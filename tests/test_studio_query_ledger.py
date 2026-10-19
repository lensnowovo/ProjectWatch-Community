"""Project-only public query reuse; no real credentials or network."""

from contextlib import closing

import pytest

from project_watch.repository import ProjectRepository
from project_watch.studio.http import HttpError
from project_watch.studio.query_ledger import ProjectQueryCache
from project_watch.studio.sources import SearchCache
from project_watch.studio.store import StudioStore

SIG = ("tavily", "synthetic-account-fingerprint", "public company product", False, 5)


def make_store(tmp_path):
    repo = ProjectRepository(tmp_path / "query.sqlite3")
    return StudioStore(repo.db_path), repo.create_project("Synthetic public company").id


def test_same_project_survives_restart_and_reuses_between_task_purposes(tmp_path):
    store, pid = make_store(tmp_path)
    calls = []
    original = [{"title": "Original", "url": "https://public.example/report", "extra": {}}]
    first = ProjectQueryCache(store, pid, purpose="external")
    hits, cached = first.get(SIG, lambda: calls.append(1) or original)
    assert not cached
    hits[0]["title"] = "Caller mutation"
    second = ProjectQueryCache(StudioStore(store.db_path), pid, purpose="competition")
    hits, cached = second.get(SIG, lambda: pytest.fail("should use completed public result"))
    assert cached and hits[0]["title"] == "Original" and calls == [1]
    with closing(store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_query_uses").fetchone()[0] == 2


def test_project_account_provider_shape_and_expiry_isolation(tmp_path):
    store, pid = make_store(tmp_path)
    front = SearchCache()
    a = ProjectQueryCache(store, pid, purpose="external", front=front)
    assert a.get(SIG, lambda: [])[1] is False
    other = ProjectRepository(store.db_path).create_project("Another public company").id
    assert ProjectQueryCache(store, other, purpose="external", front=front).get(SIG, lambda: [])[1] is False
    for signature in [("bocha", *SIG[1:]), (SIG[0], "rotated-fingerprint", *SIG[2:]), (*SIG[:-1], 10)]:
        assert a.get(signature, lambda: [])[1] is False
    expired = ProjectQueryCache(store, pid, purpose="external", ttl=0)
    assert expired.get(SIG, lambda: [])[1] is False


def test_failed_query_is_logged_not_cached_as_empty_result(tmp_path):
    store, pid = make_store(tmp_path)
    cache = ProjectQueryCache(store, pid, purpose="external")

    def uncertain():
        raise HttpError("request_outcome_unknown")

    with pytest.raises(HttpError):
        cache.get(SIG, uncertain)
    with closing(store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_query_cache").fetchone()[0] == 0
        assert db.execute("SELECT outcome FROM studio_query_uses").fetchone()[0] == "unknown"
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        ProjectQueryCache(StudioStore(store.db_path), pid, purpose="competition").get(
            SIG, lambda: pytest.fail("an uncertain paid query must not be automatically repeated"))


def test_stale_lease_guard_prevents_serving_or_writing_query_results(tmp_path):
    store, pid = make_store(tmp_path)
    ProjectQueryCache(store, pid, purpose="external").get(SIG, lambda: [])

    def expired(_db):
        raise RuntimeError("expired lease")

    with pytest.raises(RuntimeError, match="expired lease"):
        ProjectQueryCache(store, pid, purpose="competition", guard=expired).get(SIG, lambda: [])


def test_no_commercial_or_model_results_can_enter_search_cache(tmp_path):
    store, pid = make_store(tmp_path)
    cache = ProjectQueryCache(store, pid, purpose="external")
    for provider in ("wind", "xiniu", "pharmcube", "glm", "model"):
        with pytest.raises(ValueError, match="public search"):
            cache.get((provider, *SIG[1:]), lambda: pytest.fail("must not call provider"))
