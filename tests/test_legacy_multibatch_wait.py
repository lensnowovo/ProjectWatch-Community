"""Actual extractor, worker, journal and per-request consent; synthetic transport only."""
import hashlib
import json
import socket

import pytest
from fastapi.testclient import TestClient
from test_studio_unknown_recovery import authorize, endpoint

from project_watch.studio import recovery, taskboard
from project_watch.studio.app import create_studio_app
from project_watch.studio.http import HttpError, Response
from project_watch.studio.llm import LLM
from project_watch.studio.pipeline import StudioWorker
from project_watch.studio.store import now


@pytest.mark.parametrize('after_settlement', ['complete', 'budget', 'binding', 'new_unknown'])
def test_actual_three_batches_wait_and_resume_same_job(tmp_path, monkeypatch, after_settlement):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('network forbidden'))
    app = create_studio_app(tmp_path, start_worker=False)
    p = app.state.studio_pipeline
    web = TestClient(app)
    p.settings_store.update({'model': {'base_url': 'https://synthetic.invalid/v1',
                                     'model': 'synthetic', 'api_key': 'synthetic'}})
    pid = p.repository.create_project('Synthetic Multi-batch').id
    p.store.upsert_project(pid, {}, consent=True)
    binding = p.model_binding()
    inputs = {'input_version': 'frozen-inputs', 'binding': binding,
              'parts': [{'marker': str(i), 'text': 'Synthetic evidence'} for i in range(25)]}
    sends, results = [], []
    failed_once = False
    limit = 4

    class SyntheticJournalHTTP:
        def post_json(self, url, payload, **kw):
            nonlocal failed_once
            system = payload['messages'][0]['content'].split('\n', 1)[0]
            batch = json.loads(payload['messages'][1]['content'])
            key = system + ':' + batch['parts'][0]['marker']
            execution = p._execution_state.current
            if system == 'business':
                execution.budgets['model'] = limit

            def send():
                nonlocal failed_once
                sends.append(key)
                if key == 'business:12' and not failed_once:
                    failed_once = True
                    raise HttpError('connection_failed')
                if key == 'business:24' and after_settlement == 'new_unknown' and sends.count(key) == 1:
                    raise HttpError('connection_failed')
                return Response(200, {}, json.dumps({'choices': [{'message': {
                    'content': json.dumps({'batch': key})}, 'finish_reason': 'stop'}]}).encode())
            return execution.call('model', key, send, provider='synthetic')

    def extract(job, prefix, manifest):
        # Exercise the real splitter and saved proposal receipts, not a two-call stub.
        value = p._extract_all_parts(pid, manifest, LLM(p.settings_store.load().model, SyntheticJournalHTTP()), prefix,
            lambda batch: batch, prefix, lambda: manifest)
        results.append((job['id'], value))

    p.run_business = lambda job: extract(job, 'business', inputs)
    detail_inputs = {**inputs, 'parts': inputs['parts'][:1]}
    p.run_details = lambda job: extract(job, 'detail', detail_inputs)
    worker = StudioWorker(p)
    worker.schedule = lambda: None
    business = p.store.enqueue(pid, 'business')
    assert worker.step()
    assert sends == ['business:0', 'business:12']

    # A separate historical started receipt. Never settle it by changing status;
    # it must go through the original explicit recovery endpoint below.
    details = p.store.enqueue(pid, 'details')
    old_key = hashlib.sha256(b'model:detail:0').hexdigest()
    with p.store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed',error='request_outcome_unknown',"
                   "retry_after='9999-12-31' WHERE id=?", (details['id'],))
        db.execute('INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) '
                   'VALUES(?,?,?,?,?,?)', (details['id'], old_key, 'model',
                   hashlib.sha256(b'synthetic').hexdigest(), 'started', now()))
        db.executemany('INSERT INTO studio_job_checkpoints VALUES(?,?,?)', [
            (details['id'], 'recovery_inputs', json.dumps({'binding': recovery.input_binding(p, pid)})),
            (details['id'], 'detail_inputs', json.dumps({'binding': binding, 'input_version': 'frozen-inputs'}))])
        original = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls ORDER BY job_id,call_key')]

    _, approved, old_consent = authorize(web, endpoint(pid, business), key='business-explicit')
    assert approved.status_code == 202
    assert worker.step()
    assert sends == ['business:0', 'business:12', 'business:12']
    assert results == []
    with p.store._connect() as db:
        waiting = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (business['id'],)).fetchone())
        assert waiting['status'] == 'queued' and waiting['retry_after'] is None and not waiting['error']
        saved = dict(db.execute('SELECT stage,data FROM studio_job_checkpoints WHERE job_id=?', (business['id'],)))
        assert json.loads(saved['business_proposal_0']) == {'batch': 'business:0'}
        assert json.loads(saved['business_proposal_1']) == {'batch': 'business:12'}
        assert 'business_proposal_2' not in saved
        assert not recovery.project_request_holds(db, pid, exclude_job=details['id'])
    projected = next(j for j in taskboard.board(p.store, worker_state='running', model_ready=True)['jobs']
                     if j['id'] == business['id'])
    assert projected['runtime']['state'] == 'blocked'
    assert projected['runtime']['continuation_wait'] and projected['runtime']['blocking_jobs'] == [details['id']]
    assert '后续批次尚未发送' in taskboard.advice(projected)['why']
    assert taskboard.advice(projected)['action'] == 'none' and projected['unknown'] == 0
    # Repeated worker polls don't claim/send/increment attempts while held.
    for _ in range(3):
        assert not worker.step()
    assert len(sends) == 3
    assert web.get(endpoint(pid, details)).json()['can_recover']
    assert authorize(web, endpoint(pid, details), key='details-explicit')[1].status_code == 202
    assert worker.step()
    assert sends[-1] == 'detail:0' and len(sends) == 4
    assert results[0][0] == details['id']
    p.store.set_status(pid, 'paused')
    assert p.store.claim() is None
    p.store.set_status(pid, 'active')

    if after_settlement == 'budget':
        limit = 3  # Original unknown + saved success + recovered success already spent.
    elif after_settlement == 'binding':
        p.settings_store.update({'model': {'model': 'changed'}})
    elif after_settlement == 'new_unknown':
        limit = 5  # Fixed total includes both explicit recovery attempts.
    assert worker.step()
    if after_settlement == 'new_unknown':
        assert sends[-1] == 'business:24' and len(sends) == 5
        with p.store._connect() as db:
            assert not db.execute("SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage='legacy_dispatch_wait'",
                                  (business['id'],)).fetchone()
            assert len(recovery.project_request_holds(db, pid)) == 1
        # Old idempotent confirmation cannot release the new unknown.
        assert web.post(endpoint(pid, business), json=old_consent).status_code == 202
        assert p.store.claim() is None and len(sends) == 5
        assert authorize(web, endpoint(pid, business), key='new-batch-explicit')[1].status_code == 202
        assert worker.step() and len(sends) == 6 and sends[-1] == 'business:24'
    with p.store._connect() as db:
        final = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (business['id'],)).fetchone())
        assert db.execute('SELECT count(*) FROM studio_jobs').fetchone()[0] == 2
        current = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls ORDER BY job_id,call_key')]
        assert all(row in current for row in original)  # All original receipts immutable.
        assert db.execute('SELECT count(*) FROM studio_call_recoveries WHERE consumed_at IS NOT NULL').fetchone()[0] == (3 if after_settlement == 'new_unknown' else 2)
        assert not recovery.project_request_holds(db, pid)
        if after_settlement in ('complete', 'new_unknown'):
            assert final['status'] == 'done' and sends[-1] == 'business:24'
            assert len(sends) == (6 if after_settlement == 'new_unknown' else 5)
            assert results[-1] == (business['id'], [{'batch': 'business:0'}, {'batch': 'business:12'}, {'batch': 'business:24'}])
            assert db.execute('SELECT count(*) FROM studio_job_calls WHERE job_id=?', (business['id'],)).fetchone()[0] == (5 if after_settlement == 'new_unknown' else 4)
        else:
            assert final['status'] == 'failed' and len(sends) == 4
            assert ('请求已暂停' if after_settlement == 'budget' else '监控依据已变化') in final['error']
    assert p.store.claim() is None
