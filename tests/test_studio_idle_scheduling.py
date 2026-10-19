"""Synthetic invalidation and evidence lookup tests; no provider requests."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app
from project_watch.studio.pipeline import StudioWorker
from project_watch.studio.settings import ModelConfig, StudioSettings


@pytest.fixture
def scheduled(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    p = app.state.studio_pipeline
    pid = p.repository.create_project('Synthetic Idle Co').id
    p.store.upsert_project(pid, {'legal_name': 'Synthetic Idle Co'}, consent=True)
    settings = StudioSettings(model=ModelConfig(base_url='https://example.invalid', api_key='synthetic', model='synthetic'))
    monkeypatch.setattr(p.settings_store, 'load', lambda: settings)
    monkeypatch.setattr(p, 'snapshot_ready_documents', lambda: None)
    monkeypatch.setattr('project_watch.studio.signals.due_external', lambda *a: [])
    for method in ('unanalyzed_ready_projects', 'missing_public_profile_projects', 'due_landscape_projects'):
        monkeypatch.setattr(p.store, method, lambda *a, **kw: [])
    calls = []
    for kind in ('business', 'details', 'competition'):
        monkeypatch.setattr(p, 'schedule_' + kind, lambda pid, kind=kind: calls.append(kind))
    monkeypatch.setattr(p, 'schedule_insight', lambda *_: None)
    worker = StudioWorker(p)
    try:
        yield p, pid, settings, worker, calls
    finally:
        worker.stop()


def test_idle_repeated_schedule_skips_unchanged_input_gathering(scheduled):
    p, _pid, _, worker, calls = scheduled
    worker.schedule()
    worker.schedule()
    assert calls == ['business', 'details', 'competition']
    p.store.set_state('synthetic-evidence-arrival', '1')
    worker.schedule()
    assert len(calls) == 6
    worker.schedule()
    assert len(calls) == 6


def test_settings_change_and_periodic_fallback_invalidate(scheduled, monkeypatch):
    p, _, settings, worker, calls = scheduled
    worker.schedule()
    monkeypatch.setattr(p.settings_store, 'load', lambda: replace(settings, model=replace(settings.model, api_key='new-synthetic')))
    worker.schedule()
    assert len(calls) == 6
    worker._analysis_checked_at -= 61
    worker.schedule()
    assert len(calls) == 9


def test_failed_check_and_concurrent_arrival_are_not_cached(scheduled, monkeypatch):
    p, _, _, worker, calls = scheduled
    def fail(*_):
        raise ValueError('synthetic preparation failure')
    monkeypatch.setattr(p, 'schedule_details', fail)
    with pytest.raises(ValueError):
        worker.schedule()
    assert worker._analysis_stamp is None
    monkeypatch.setattr(p, 'schedule_details', lambda *_: p.store.set_state('during-check', 'new'))
    worker.schedule()
    monkeypatch.setattr(p, 'schedule_details', lambda *_: calls.append('details'))
    before = len(calls)
    worker.schedule()
    assert len(calls) == before + 3


def test_evidence_lookup_indexes_cover_reverse_joins(tmp_path):
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    with p.store._connect() as db:
        plan = ' '.join(str(tuple(r)) for r in db.execute(
            'EXPLAIN QUERY PLAN SELECT item_id FROM studio_item_evidence WHERE project_id=? AND evidence_version_id=?',
            ('synthetic', 'version')))
        assert 'studio_item_evidence_version' in plan
        plan = ' '.join(str(tuple(r)) for r in db.execute(
            'EXPLAIN QUERY PLAN SELECT project_id FROM studio_evidence_heads WHERE evidence_version_id=?', ('version',)))
        assert 'studio_evidence_head_version' in plan


def test_manual_worker_observer_closed_on_app_shutdown(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    worker = app.state.studio_worker
    with TestClient(app):
        worker._analysis_input_stamp(StudioSettings())
        assert worker._analysis_db is not None
    assert worker._analysis_db is None
    assert worker._analysis_stamp is None
