"""Live finding: scheduling and page-summary checks slowed to minutes on a real-sized project."""
import contextlib
import sqlite3
from contextlib import closing

from test_studio_bilateral_competition import reviewer, setup_pair

from project_watch.studio.collection_ingest import SCHEMA as COLLECTION_SCHEMA
from project_watch.studio.store import StudioStore


def _connections(monkeypatch):
    opened = []
    original = sqlite3.connect

    def counting(*args, **kwargs):
        opened.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', counting)
    return opened


def test_a_relationship_view_reads_through_one_connection(tmp_path, monkeypatch):
    from project_watch.studio.competition import assess_relation

    service, pid, scope, _ = setup_pair(tmp_path)
    service.relationships.save_assessment(pid, assess_relation(service, pid, scope, 'direct', review=reviewer))
    # Baseline from the old per-lookup connections, not from the new path itself.
    monkeypatch.setattr(service.store, 'read_session', contextlib.nullcontext)
    expected = service.relationships.view(pid)
    page = service.relationships.page(pid)
    monkeypatch.undo()
    opened = _connections(monkeypatch)
    assert service.relationships.view(pid) == expected
    assert len(opened) == 1
    opened.clear()
    assert service.relationships.page(pid) == page
    assert len(opened) == 1


def test_a_view_inside_a_read_session_reuses_it(tmp_path, monkeypatch):
    service, pid, _, _ = setup_pair(tmp_path)
    with service.store.read_session():
        opened = _connections(monkeypatch)
        service.relationships.view(pid)
        service.relationships.page(pid)
    assert opened == []


def test_snapshot_permission_checks_use_an_index_on_existing_databases(tmp_path):
    service, _, _, _ = setup_pair(tmp_path)
    path = service.store.db_path
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(COLLECTION_SCHEMA[1])  # an instance created before the indexes existed
        db.execute('DROP INDEX IF EXISTS studio_collection_observations_evidence')
        db.execute('DROP INDEX IF EXISTS studio_collection_observations_item')
    StudioStore(path)
    with closing(sqlite3.connect(path)) as db:
        plan = ' '.join(r[3] for r in db.execute(
            'EXPLAIN QUERY PLAN SELECT 1 FROM studio_collection_observations o JOIN studio_items i ON i.id=o.item_id '
            "WHERE i.project_id=? AND o.evidence_version_id=? AND (i.status='dismissed' OR i.relevant=0)", ('p', 'e')))
        assert 'studio_collection_observations_evidence' in plan
        plan = ' '.join(r[3] for r in db.execute(
            'EXPLAIN QUERY PLAN SELECT evidence_version_id FROM studio_collection_observations WHERE item_id=?', ('i',)))
        assert 'studio_collection_observations_item' in plan


def test_a_page_on_a_callers_transaction_opens_no_extra_connection(tmp_path, monkeypatch):
    from project_watch.studio.competition import assess_relation

    service, pid, scope, _ = setup_pair(tmp_path)
    service.relationships.save_assessment(pid, assess_relation(service, pid, scope, 'direct', review=reviewer))
    expected = service.relationships.page(pid)
    with closing(service.store._connect()) as db:
        db.execute('BEGIN')
        opened = _connections(monkeypatch)
        assert service.relationships.page(pid, db=db) == expected
    assert opened == []


def test_a_view_under_a_write_scope_still_matches(tmp_path):
    service, pid, _, _ = setup_pair(tmp_path)
    expected = service.relationships.view(pid)
    with service.store.write_scope(lambda db: None):
        assert service.relationships.view(pid) == expected
