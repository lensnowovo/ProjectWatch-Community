"""Migration rollback and ordinary-path failure/report boundaries, offline only."""
import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_event_insight import install
from test_monitor_completion_journey import bind_cost, call_rows, cost_window
from test_update_handoff import finish_old

from project_watch.studio import acceptance_batch, update_handoff, update_run
from project_watch.studio.http import Http, HttpError


def frozen_rows(store):
    with closing(store._connect()) as db:
        return {table: [tuple(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('studio_jobs', 'studio_job_calls', 'studio_job_checkpoints',
                              'studio_job_budget_links', 'studio_acceptance_batches',
                              'studio_acceptance_jobs', 'studio_research_costs')}


@pytest.mark.parametrize('failure', ['expired', 'late_validation', 'paused', 'changed_input'])
def test_rejected_handoff_preserves_closed_account_and_old_work(tmp_path, monkeypatch, failure):
    pipeline, root, _ = install(tmp_path, monkeypatch)
    pid = root.job['project_id']
    cost_window(pipeline, pid)
    bind_cost(pipeline, root.job['id'], 'monitor')
    finish_old(pipeline, root)
    acceptance_batch.close(pipeline.store, 'journey-cost')
    with closing(pipeline.store._connect()) as db, db:
        if failure == 'expired':
            db.execute("UPDATE studio_acceptance_batches SET deadline='2000-01-01T00:00:00+00:00'")
        elif failure == 'late_validation':
            # Fails after the code provisionally opens the account and enqueues:
            # both changes must roll back with the missing source scope.
            db.execute("DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage='collection_scope_v1'", (root.job['id'],))
        elif failure == 'paused':
            db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (pid,))
        else:
            db.execute("UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage='recovery_inputs'",
                       (json.dumps({'binding': 'changed'}), root.job['id']))
    before = frozen_rows(pipeline.store)
    with pytest.raises((ValueError, HttpError)):
        update_handoff.admit(pipeline, root.job['id'], authorization='synthetic explicit handoff')
    assert frozen_rows(pipeline.store) == before


def test_undeclared_handoff_is_not_claimable(tmp_path, monkeypatch):
    pipeline, root, _ = install(tmp_path, monkeypatch)
    finish_old(pipeline, root)
    job = pipeline.store.enqueue(root.job['project_id'], 'external_followup', automatic=True,
                                 input_version='update-handoff:' + root.job['id'])
    with closing(pipeline.store._connect()) as db, db:
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                   (job['id'], update_handoff.STAGE, json.dumps({'origin_job_id': root.job['id']})))
    assert pipeline.store.claim() is None


def test_unknown_in_inline_analysis_preserves_events_and_stops_redispatch(tmp_path, monkeypatch):
    pipeline, root, _ = install(tmp_path, monkeypatch)
    original = pipeline.http_factory(None).transport
    attempts = []

    def fail_analysis(method, url, headers, raw, timeout):
        body = json.loads(raw) if raw and 'model.example.invalid' in url else {}
        if any(t['function']['name'] == 'pw_judge' for t in body.get('tools', [])):
            attempts.append(1)
            raise HttpError('connection_failed')
        return original(method, url, headers, raw, timeout)

    pipeline.http_factory = lambda _: Http(fail_analysis)
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        pipeline.run(root.job)
    state = root.read_checkpoint(update_run.STATE)
    assert state['phases']['events']['output']['event_versions']
    assert state['phases']['analysis']['status'] == 'blocked'
    assert 'reports' not in state['phases']
    events = pipeline.store.list_events(project_id=root.job['project_id'])
    assert events
    receipts = call_rows(pipeline.store, root.job['id'])
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        pipeline.run(root.job)
    assert attempts == [1]
    assert call_rows(pipeline.store, root.job['id']) == receipts
    assert pipeline.store.list_events(project_id=root.job['project_id']) == events


def test_paused_project_cannot_advance_saved_stage(tmp_path, monkeypatch):
    pipeline, root, _ = install(tmp_path, monkeypatch)
    update_run.stage(pipeline, root, 'sources', {}, lambda: ({'saved': True}, True))
    before = root.read_checkpoint(update_run.STATE)
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (root.job['project_id'],))
    calls = []
    with pytest.raises(HttpError, match='project_paused'):
        update_run.stage(pipeline, root, 'events', {}, lambda: calls.append(1))
    assert not calls and root.read_checkpoint(update_run.STATE) == before


def test_shared_round_keeps_its_lower_cap_inside_larger_cost_account(tmp_path, monkeypatch):
    from project_watch.studio import event_insight_work

    pipeline, root, _ = install(tmp_path, monkeypatch)
    cost_window(pipeline, root.job['project_id'])  # Account permits 80 requests.
    bind_cost(pipeline, root.job['id'], 'monitor')
    state = event_insight_work.begin(root, cap=13, shared=True)
    assert state['cap'] == 13 and state['collection_cap'] == 7
    assert root.budgets['model'] == 7 and not call_rows(pipeline.store, root.job['id'])


def test_report_projection_matches_actual_today_and_weekly_read_endpoints(tmp_path, monkeypatch):
    pipeline, root, _ = install(tmp_path, monkeypatch)
    pipeline.run(root.job)
    assert pipeline.store.finish(root.job['id'], lease_token=root.job['lease_token'])
    pid = root.job['project_id']
    reports = root.read_checkpoint(update_run.STATE)['phases']['reports']['output']
    before = call_rows(pipeline.store, root.job['id'])
    client = TestClient(pipeline._test_app)
    for label, days in (('today', 1), ('week', 7)):
        response = client.get('/api/studio/events', params={'project_id': pid, 'days': days})
        assert response.status_code == 200
        assert {e['id'] for e in response.json()} == set(reports['windows'][label]['event_ids'])
    weekly = client.get('/api/studio/digest?days=7')
    assert weekly.status_code == 200
    data = weekly.json()
    assert data['days'] == 7 and data['markdown']
    assert data['totals']['major'] + data['totals']['notable'] == len(reports['windows']['week']['important_ids'])
    assert call_rows(pipeline.store, root.job['id']) == before
