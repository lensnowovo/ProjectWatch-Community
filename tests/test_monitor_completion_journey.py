"""Ordinary API/worker, source admission, events and reviewed analysis.

Only provider boundaries are synthetic. No real data, providers or schedulers.
"""
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_event_insight import install
from test_research_budget import contract

from project_watch.studio import event_insight_work, insight, snapshot_work
from project_watch.studio.execution import JobExecution
from project_watch.studio.pipeline import StudioWorker
from project_watch.studio.store import now


def cost_window(pipeline, pid):
    from project_watch.studio import acceptance_batch

    caps = dict.fromkeys(acceptance_batch.KINDS, 80)
    policy = contract(1_000_000)
    policy['tariffs'][0].update(context_tokens=32000, output_tokens=20000, wire_bytes=131072)
    policy['tariffs'].append(dict(kind='search', endpoint='https://api.tavily.com/search',
                                 per_request=10, source='https://example.invalid/synthetic-search-price'))
    acceptance_batch.create(pipeline.store, 'journey-cost',
        deadline=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), caps=caps,
        journeys=[dict(project_id=pid, roots=['bootstrap', 'monitor'], caps=caps)],
        plan_sha256='c' * 64, cost_policy=policy)


def bind_cost(pipeline, jid, slot):
    from project_watch.studio import acceptance_batch

    acceptance_batch.bind_root(pipeline.store, 'journey-cost', jid, slot)


def old_hold(store, pid):
    job = store.enqueue(pid, 'details')
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='failed',finished_at=? WHERE id=?", (now(), job['id']))
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                   (job['id'], 'detail_inputs', json.dumps({'input_version': 'frozen-old', 'binding': 'frozen-old'})))
        db.execute('INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) VALUES(?,?,?,?,?,?)',
                   (job['id'], 'old-do-not-retry', 'model', 'synthetic', 'unknown', now()))
    return job['id']


def call_rows(store, jid):
    with closing(store._connect()) as db:
        return [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=? ORDER BY call_key', (jid,))]


def finish_direct_children(pipeline, worker, pid, seen):
    """Finish the real first-round tail before requesting a new increment."""
    for _ in range(8):
        pipeline.schedule_insight(pid)
        with closing(pipeline.store._connect()) as db:
            pending = db.execute("SELECT id FROM studio_jobs WHERE project_id=? "
                                 "AND status IN ('queued','running')", (pid,)).fetchall()
        if not pending:
            return
        seen['baseline_events'] = pipeline.store.list_events(project_id=pid)
        seen['baseline_watches'] = pipeline.monitoring_plan.items(pid)
        seen['baseline_material_count'] = len(seen['materials'])
        assert worker.step()
        with closing(pipeline.store._connect()) as db:
            assert not db.execute("SELECT id,error FROM studio_jobs WHERE project_id=? AND status='failed'",
                                  (pid,)).fetchall()
    pytest.fail('direct first-round children did not finish')


@pytest.mark.parametrize('previous_hold', [False, True])
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('cost_enabled', [False, True])
def test_update_runs_to_reviewed_analysis_without_replaying_old_request(tmp_path, monkeypatch, previous_hold, native, cost_enabled):
    pipeline, seed, seen = install(tmp_path, monkeypatch, monitor=True, native=native)
    pid = seed.job['project_id']
    # The shared fixture reserves an untouched seed job; finish it before the
    # user action, without claiming that reservation performed any research.
    assert not seed.usage()
    assert pipeline.store.finish(seed.job['id'], lease_token=seed.job['lease_token'])
    prior = old_hold(pipeline.store, pid) if previous_hold else None
    before = call_rows(pipeline.store, prior) if prior else []
    if cost_enabled:
        cost_window(pipeline, pid)
    client = TestClient(pipeline._test_app)
    response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
    assert response.status_code == 202
    jid = response.json()['id']
    if cost_enabled:
        bind_cost(pipeline, jid, 'monitor')
    worker = StudioWorker(pipeline)
    # Manual action and its directly enqueued children only, no periodic jobs.
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    root = JobExecution(pipeline.store, {'id': jid})
    state = root.read_checkpoint(event_insight_work.ROUND)
    assert state and state['phase'] == 'settled', root.read_checkpoint('independent_public_collection_result_v1')
    assert pipeline.store.list_events(project_id=pid)
    with closing(pipeline.store._connect()) as db:
        job = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (jid,)).fetchone())
        assert job['status'] == 'done', job['error']
        assert not db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind='insight'", (pid,)).fetchone()
    result = root.read_checkpoint(snapshot_work.PUBLICATION)
    assert result['status'] == 'completed' and len(result['published']) == 5
    assert all(s['status'] == 'approved' and s['review_receipts'] for s in result['sections'].values())
    saved = insight.load(pipeline.store, pid)
    assert saved['status'] == 'current' and len(saved['section_versions']) == 5
    assert result['snapshot_id'] and seen['materials']
    assert not seen['insight']  # No legacy event-gated analysis is dispatched.
    if cost_enabled:
        assert root.budget_used('model') <= state['cap']
    else:
        assert state['cap'] is None  # No legacy per-round soft model partition.
    pipeline.schedule_insight(pid)  # The worker's ordinary terminal-round dispatcher.
    assert root.read_checkpoint(event_insight_work.ROUND)['phase'] == 'settled'
    if prior:
        assert call_rows(pipeline.store, prior) == before
    detail = client.get(f'/api/studio/projects/{pid}')
    assert detail.status_code == 200
    if cost_enabled:
        from project_watch.studio.research_budget import summary

        receipt = summary(pipeline.store, 'journey-cost')
        assert 0 < receipt['committed'] < receipt['ceiling']
        assert receipt['price_mismatches'] == 0


@pytest.mark.parametrize('journey', ['update', 'first_increment'])
def test_shared_work_ordinary_journey(tmp_path, monkeypatch, journey):
    from project_watch.studio import research_work_budget

    original = cost_window

    def shared(pipeline, pid):
        original(pipeline, pid)
        research_work_budget.enable(pipeline.store, 'journey-cost',
                                    authorization='synthetic shared work admission')

    monkeypatch.setitem(globals(), 'cost_window', shared)
    if journey == 'update':
        test_update_runs_to_reviewed_analysis_without_replaying_old_request(
            tmp_path, monkeypatch, True, True, True)
    else:
        test_new_project_first_round_then_increment_keeps_event_identity(
            tmp_path, monkeypatch, True, True)


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('cost_enabled', [False, True])
def test_new_project_first_round_then_increment_keeps_event_identity(tmp_path, monkeypatch, native, cost_enabled):
    pipeline, seed, seen = install(tmp_path, monkeypatch, monitor=False, native=native, user_entry=True)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    pid = seed.job['project_id']
    if cost_enabled:
        cost_window(pipeline, pid)
    client = TestClient(pipeline._test_app)
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    first_ids = None
    first_job = None
    for expected_entry in ('bootstrap', 'monitor'):
        seen['baseline_events'] = pipeline.store.list_events(project_id=pid)
        seen['baseline_watches'] = pipeline.monitoring_plan.items(pid)
        seen['baseline_material_count'] = len(seen['materials'])
        if expected_entry == 'bootstrap':
            jid = seed.job['id']
        else:
            response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external'})
            assert response.status_code == 202
            jid = response.json()['id']
            assert response.json()['kind'] == 'external' and jid != first_job
        first_job = first_job or jid
        if cost_enabled:
            bind_cost(pipeline, jid, expected_entry)
        assert worker.step()
        root = JobExecution(pipeline.store, {'id': jid})
        with closing(pipeline.store._connect()) as db:
            status = db.execute('SELECT status,error FROM studio_jobs WHERE id=?', (jid,)).fetchone()
        assert status[0] == 'done', status[1]
        assert root.read_checkpoint('material_pipeline_v1')['entry'] == expected_entry
        state = root.read_checkpoint(event_insight_work.ROUND)
        assert state['publication_complete']
        result = root.read_checkpoint(snapshot_work.PUBLICATION)
        assert result and result['status'] == 'completed' and len(result['published']) == 5
        assert all(s['status'] == 'approved' and s['review_receipts'] for s in result['sections'].values())
        assert 'insight_job_id' not in state
        pipeline.schedule_insight(pid)
        assert root.read_checkpoint(event_insight_work.ROUND)['phase'] == 'settled'
        events = pipeline.store.list_events(project_id=pid)
        ids = {e['id'] for e in events}
        assert ids
        if first_ids is not None:
            assert ids == first_ids
        first_ids = ids
        saved = insight.load(pipeline.store, pid)
        assert saved['status'] == 'current' and len(saved['section_versions']) == 5
        assert client.get(f'/api/studio/projects/{pid}').status_code == 200
        finish_direct_children(pipeline, worker, pid, seen)
