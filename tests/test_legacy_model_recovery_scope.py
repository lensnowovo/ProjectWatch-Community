"""Independent frozen extractors: explicit one-request consent with other holds.

All receipts/transports are synthetic. No scheduler or provider connection.
"""
import hashlib
import json
import socket

import pytest
from fastapi.testclient import TestClient
from test_studio_unknown_recovery import authorize, endpoint

from project_watch.studio import recovery
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response
from project_watch.studio.store import now


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network prohibited'))
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'synthetic-runtime')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    app = create_studio_app(tmp_path, start_worker=False)
    p = app.state.studio_pipeline
    p.settings_store.update({'model': {'base_url':'https://synthetic.invalid/v1','model':'synthetic','api_key':'synthetic'}})
    pid = p.repository.create_project('Synthetic Independent Recovery').id
    p.store.upsert_project(pid, {}, consent=True)
    jobs=[]
    for kind, prefix, status in [('business','business','unknown'), ('details','detail','started')]:
        job = p.store.enqueue(pid, kind)
        with p.store._connect() as db:
            db.execute("UPDATE studio_jobs SET status='failed',error='request_outcome_unknown',retry_after='9999-12-31' WHERE id=?", (job['id'],))
            for key, outcome in [('saved','completed'),('pending',status)]:
                db.execute('INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,response_status,response_headers,response_body,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (job['id'], hashed('model:'+key), 'model', hashed('synthetic'), outcome,
                     200 if outcome=='completed' else None, '{}', b'{}', now()))
            db.executemany('INSERT INTO studio_job_checkpoints VALUES(?,?,?)', [
                (job['id'], 'recovery_inputs', json.dumps({'binding':recovery.input_binding(p,pid)})),
                (job['id'], prefix+'_inputs', json.dumps({'binding':'synthetic','input_version':'frozen-inputs'}))])
        jobs.append(job)
    return TestClient(app),p,pid,jobs


def pending(p,pid):
    with p.store._connect() as db:
        return [(r['job_id'], r['status']) for r in recovery.project_request_holds(db,pid)]


def run_once(p, job, send):
    execution = p._execution_state.current
    assert execution.call('model','saved',lambda:pytest.fail('completed replay'),provider='synthetic').status==200
    return execution.call('model','pending',send,provider='synthetic')


def test_two_independent_requests_recover_sequentially_without_unlocking_other(case):
    web,p,pid,jobs=case
    old=pending(p,pid)
    assert len(old)==2
    for job in jobs:
        view=web.get(endpoint(pid,job)).json()
        assert view['can_recover'] and view['mode']=='resume'
        assert '只恢复此任务' in view['notice'] or len(pending(p,pid))==1
    sends=[]
    p.run_business=lambda job:run_once(p,job,lambda:(sends.append('business'),Response(200,{},b'{}'))[1])
    p.run_details=lambda job:run_once(p,job,lambda:(sends.append('details'),Response(200,{},b'{}'))[1])
    for index,job in enumerate(jobs):
        _,result,body=authorize(web,endpoint(pid,job),key='explicit-'+job['id'])
        assert result.status_code==202,result.text
        assert web.post(endpoint(pid,job),json=body).json()==result.json()
        claimed=p.store.claim()
        assert claimed['id']==job['id']
        p.run(claimed)
        p.store.finish(claimed['id'],lease_token=claimed['lease_token'])
        assert sends==['business','details'][:index+1]
        assert len(pending(p,pid))==1-index
        if index==0:
            # The remaining details hold belongs to an independent extractor: a public
            # refresh is admitted without sending, settling or unlocking that request.
            calls=pending(p,pid)
            refresh=web.post(f'/api/studio/projects/{pid}/refresh',json={'kind':'external'})
            assert refresh.status_code==202 and refresh.json()['kind']=='external'
            assert pending(p,pid)==calls and sends==['business']
            with p.store._connect() as db:  # outside this test's recovery-order scope
                db.execute("UPDATE studio_jobs SET status='done' WHERE id=?",(refresh.json()['id'],))
    with p.store._connect() as db:
        for job in jobs:
            # No history is overwritten when a successor request succeeds.
            row=db.execute('SELECT status FROM studio_job_calls WHERE job_id=? AND call_key=?',
                           (job['id'],hashed('model:pending'))).fetchone()
            assert row[0] in ('unknown','started')
        assert db.execute('SELECT count(*) FROM studio_job_calls').fetchone()[0]==6
    assert p.store.claim() is None


@pytest.mark.parametrize('change',['payload','provider','kind','budget','lease','binding','no_consent','stale_version','missing_inputs'])
def test_exact_dispatch_and_approval_fences(case,change):
    web,p,pid,jobs=case
    job=jobs[0]
    before=pending(p,pid)
    view=web.get(endpoint(pid,job)).json()
    body={'expected_version':view['version'],'idempotency_key':'synthetic-fenced','accept_duplicate_cost':True}
    if change=='no_consent':
        body['accept_duplicate_cost']=False
    if change=='stale_version':
        body['expected_version']='stale'
    if change=='missing_inputs':
        with p.store._connect() as db:
            db.execute("DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage='business_inputs'",(job['id'],))
    result=web.post(endpoint(pid,job),json=body)
    if change in ('no_consent','stale_version','missing_inputs'):
        assert result.status_code==409 and p.store.claim() is None
    else:
        assert result.status_code==202,result.text
        claimed=p.store.claim()
        if change=='binding':
            p.settings_store.update({'model':{'model':'changed'}})
            p.run_business=lambda _:pytest.fail('binding changed')
            with pytest.raises(ValueError,match='监控依据已变化'):
                p.run(claimed)
        else:
            execution=JobExecution(p.store,claimed,budgets={'model':2} if change=='budget' else None)
            if change=='lease':
                with p.store._connect() as db:
                    db.execute("UPDATE studio_jobs SET lease_token='different' WHERE id=?",(job['id'],))
            with pytest.raises((HttpError,RuntimeError)):
                execution.call('search' if change=='kind' else 'model','different' if change=='payload' else 'pending',
                    lambda:pytest.fail('fenced dispatch'),provider='different' if change=='provider' else 'synthetic')
    assert pending(p,pid)==before


def test_new_failure_cannot_reuse_consent_and_new_payload_cannot_bypass(case):
    web,p,pid,jobs=case
    job=jobs[0]
    _,accepted,_=authorize(web,endpoint(pid,job))
    assert accepted.status_code==202
    claimed=p.store.claim()
    execution=JobExecution(p.store,claimed)
    calls=[]
    def fail():
        calls.append(1)
        raise HttpError('connection_failed')
    for key in ('pending','pending','unapproved-next-batch'):
        with pytest.raises(HttpError):
            execution.call('model',key,fail,provider='synthetic')
    assert calls==[1] and len(pending(p,pid))==2
    with p.store._connect() as db:
        assert recovery.model_continuation_slot(db,claimed) is None


def test_new_legacy_job_does_not_bypass_existing_unknown(case):
    _,p,pid,_=case
    p.store.enqueue(pid,'business')
    claimed=p.store.claim()
    with pytest.raises(HttpError,match='request_outcome_unknown'):
        JobExecution(p.store,claimed).call('model','new',lambda:pytest.fail('unapproved new task'),provider='synthetic')
