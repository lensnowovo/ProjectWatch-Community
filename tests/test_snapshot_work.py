"""Real queue and worker entry, isolated storage and synthetic model transport."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
import test_collection_ingest as fixtures
from fastapi.testclient import TestClient
from test_collection_ingest import trial
from test_research_budget import contract

from project_watch.studio import material_snapshot, snapshot_work
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.pipeline import StudioWorker
from project_watch.studio.settings import ModelConfig


@pytest.fixture
def saved(tmp_path, request):
    research, unit, ingest = fixtures.host.__wrapped__(tmp_path)
    settings = research.pipeline.settings_store.load()
    settings.model = ModelConfig(base_url='https://model.invalid/v1', model='synthetic', api_key='synthetic', max_output_tokens=4096)
    research.pipeline.settings_store.save(settings)
    ingest(unit, [trial()])
    snapshot = material_snapshot.capture(research.execution)
    if getattr(request, 'param', False):
        from project_watch.studio import acceptance_batch
        policy = contract(1_000_000)
        policy['tariffs'][0].update(endpoint='https://model.invalid/v1/chat/completions',
                                    context_tokens=32000, output_tokens=4096, wire_bytes=131072)
        caps = dict.fromkeys(acceptance_batch.KINDS, 80)
        acceptance_batch.create(research.store, 'snapshot-cost',
            deadline=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), caps=caps,
            journeys=[dict(project_id=research.project_id, roots=['first'], caps=caps)],
            plan_sha256='d' * 64, cost_policy=policy)
        acceptance_batch.bind_root(research.store, 'snapshot-cost', research.execution.job['id'], 'first')
    research.store.finish(research.execution.job['id'], lease_token=research.execution.job['lease_token'])
    return research, snapshot


@pytest.mark.parametrize('saved', [False, True], indirect=True)
def test_terminal_failure_reopen_new_worker_same_material_no_collection(saved, tmp_path):
    research, snapshot = saved
    p = research.pipeline
    first = snapshot_work.enqueue(p, research.project_id, snapshot['id'], 'click-first')
    assert snapshot_work.enqueue(p, research.project_id, snapshot['id'], 'click-first')['id'] == first['id']
    calls = []
    def transport(method, url, headers, body, timeout):
        calls.append((method, url))
        assert method == 'POST' and url == 'https://model.invalid/v1/chat/completions'
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': '[]'}}],
                                           'usage': {'prompt_tokens': 7, 'completion_tokens': 2}}).encode())
    p.http_factory = lambda _: Http(transport)
    worker = StudioWorker(p)
    worker.schedule = lambda: None
    assert worker.step()
    job = first
    before = material_snapshot.read(p.store, research.project_id, snapshot['id'])
    with closing(p.store._connect()) as db:
        assert db.execute('SELECT status FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'failed'
        old_calls = [dict(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=?', (job['id'],))]
        old_costs = [dict(r) for r in db.execute('SELECT * FROM studio_research_costs WHERE job_id=?', (job['id'],))]
    assert old_calls
    # New application composition and a newly claimed job, not a second callback.
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    next_job = snapshot_work.enqueue(p, research.project_id, snapshot['id'], 'click-second', parent_attempt_id=job['id'])
    def success(method, url, headers, body, timeout):
        calls.append((method, url))
        assert method == 'POST' and url == 'https://model.invalid/v1/chat/completions'
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': '{"operations":{"verdict":"unclear"}}'}}],
                                           'usage': {'prompt_tokens': 9, 'completion_tokens': 3}}).encode())
    p.http_factory = lambda _: Http(success)
    worker = StudioWorker(p)
    worker.schedule = lambda: None
    assert worker.step()
    claimed = next_job
    result = JobExecution(p.store, claimed).read_checkpoint(snapshot_work.RESULT)
    assert result['status'] == 'draft' and result['snapshot_id'] == snapshot['id']
    assert result['publication'] == 'not_reviewed'
    assert material_snapshot.read(p.store, research.project_id, snapshot['id']) == before
    with closing(p.store._connect()) as db:
        assert db.execute('SELECT status FROM studio_jobs WHERE id=?', (claimed['id'],)).fetchone()[0] == 'done'
        assert old_calls == [dict(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=?', (job['id'],))]
        assert old_costs == [dict(r) for r in db.execute('SELECT * FROM studio_research_costs WHERE job_id=?', (job['id'],))]
        if old_costs:
            costs = [dict(r) for r in db.execute('SELECT * FROM studio_research_costs WHERE job_id=?', (claimed['id'],))]
            assert len(costs) == 1 and costs[0]['charged'] == 30 and costs[0]['phase'] == 'analyze'
            assert all(row['charged'] == 22 and row['settled'] == 1 for row in old_costs)
        assert db.execute('SELECT count(*) FROM studio_job_budget_links WHERE job_id=?', (claimed['id'],)).fetchone()[0] == len(old_calls)
        assert not db.execute("SELECT 1 FROM studio_job_calls WHERE kind!='model'").fetchone()
    assert p.store.list_events(project_id=research.project_id) == []


def test_unknown_blocks_new_attempt_without_changing_old_receipt(saved):
    research, snapshot = saved
    p = research.pipeline
    job = snapshot_work.enqueue(p, research.project_id, snapshot['id'], 'first')
    claimed = p.store.claim()
    execution = JobExecution(p.store, claimed)
    def timeout(*args, **kwargs):
        raise HttpError('timeout')
    with pytest.raises(HttpError):
        execution.http(Http(timeout), lambda _: ('model', 'synthetic')).post_json('https://model.invalid/v1/chat/completions', {})
    p.store.finish(job['id'], error='request_outcome_unknown', lease_token=claimed['lease_token'])
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        snapshot_work.enqueue(p, research.project_id, snapshot['id'], 'second', parent_attempt_id=job['id'])
    with closing(p.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE status='unknown'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM studio_jobs WHERE input_version LIKE 'snapshot:%'").fetchone()[0] == 1


def test_concurrent_clicks_are_one_admission_and_api_cannot_change_input(saved, tmp_path):
    research, snapshot = saved
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    path = f'/api/studio/projects/{research.project_id}/saved-analysis'
    body = dict(snapshot_id=snapshot['id'], request_id='same-click')
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: client.post(path, json=body), range(2)))
    assert [r.status_code for r in replies] == [200, 200]
    assert replies[0].json()['id'] == replies[1].json()['id']
    assert client.post(path, json=body | {'snapshot_id': 'different'}).status_code == 409
    assert client.post(path, json=body | {'request_id': 'new-click'}).status_code == 409


def test_changed_model_or_withdrawn_material_stops_before_transport(saved):
    research, snapshot = saved
    p = research.pipeline
    snapshot_work.enqueue(p, research.project_id, snapshot['id'], 'first')
    settings = p.settings_store.load()
    settings.model.model = 'changed'
    p.settings_store.save(settings)
    p.http_factory = lambda _: pytest.fail('no HTTP on changed inputs')
    claimed = p.store.claim()
    with pytest.raises(ValueError, match='inputs changed'):
        p.run(claimed)
    p.store.finish(claimed['id'], error='changed', lease_token=claimed['lease_token'])
    p.evidence.dependencies.withdraw(research.project_id, snapshot['materials'][0]['evidence_version_id'],
                                    'synthetic withdrawal', actor='test')
    with pytest.raises(ValueError, match='withdrawn'):
        snapshot_work.enqueue(p, research.project_id, snapshot['id'], 'second', parent_attempt_id=claimed['id'])


def test_ordinary_update_snapshots_sources_before_event_admission_failure(tmp_path, monkeypatch):
    from test_decoupled_update import setup

    from project_watch.studio import progressive_bootstrap
    pipeline, seed, _, worker, _ = setup(tmp_path, monkeypatch)
    reached = []
    def fail(*args):
        reached.append(True)
        raise ValueError('synthetic event admission failure')
    monkeypatch.setattr(progressive_bootstrap, 'run', fail)
    assert worker.step()
    assert reached == [True]
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT status FROM studio_jobs WHERE id=?', (seed.job['id'],)).fetchone()[0] == 'failed'
        row = db.execute('SELECT record FROM studio_insight_versions WHERE project_id=? AND artifact_key=?',
                         (seed.job['project_id'], material_snapshot.KEY + ':' + seed.job['id'])).fetchone()
    assert row
    snapshot = json.loads(row[0])
    assert snapshot['materials']
    assert material_snapshot.read(pipeline.store, seed.job['project_id'], snapshot['id'])['materials']
    assert pipeline.store.list_events(project_id=seed.job['project_id']) == []
