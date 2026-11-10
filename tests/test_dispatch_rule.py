"""The rewritten pipeline's single dispatch rule (studio/dispatch.py). No network."""
import hashlib
import json
import socket

import pytest
from synthetic_providers import legacy_job

from project_watch.studio import dispatch
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Response
from project_watch.studio.store import now


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network prohibited'))
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project('Synthetic Subject')
    pipeline.store.upsert_project(project.id, {'legal_name': 'Synthetic Subject Ltd'}, consent=True)
    with pipeline.store._connect() as db:  # the creation round is not part of these checks
        db.execute("UPDATE studio_jobs SET status='done' WHERE project_id=?", (project.id,))
    return pipeline.store, project.id


def job(store, pid, kind):
    store.enqueue(pid, kind, input_version='test:' + kind + now())
    claimed = store.claim()
    assert claimed['kind'] == kind
    return JobExecution(store, claimed)


def journal_key(kind, key):
    return hashlib.sha256((kind + ':' + key).encode()).hexdigest()


def receipt(store, job_id, kind, key, status):
    with store._connect() as db:
        db.execute('INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) VALUES (?,?,?,?,?,?)',
                   (job_id, journal_key(kind, key), kind, 'synthetic', status, now()))


def rows(store):
    with store._connect() as db:
        return [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls ORDER BY rowid')]


def ok(sent):
    def send():
        sent.append(1)
        return Response(200, {}, b'{}')
    return send


def test_an_old_round_unknown_is_never_settled_and_a_new_round_is_a_new_request(env):
    store, pid = env
    legacy = legacy_job(store, pid, 'external', status='failed')
    receipt(store, legacy['id'], 'search', 'same-query', 'unknown')
    before, sent = rows(store), []
    work = job(store, pid, 'collect')
    work.call('search', 'same-query', ok(sent))  # undated query, next round: sent and counted
    assert sent == [1]
    assert [r for r in rows(store) if r in before] == before  # the old unknown row is untouched
    with store._connect() as db:
        assert dispatch.spent(db, pid, 'search') == 2  # both count


def test_analyses_of_one_batch_never_resend_an_unknown_request(env):
    store, pid = env
    first = job(store, pid, 'analyze')
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET input_version='collect:batch-1',status='failed' WHERE id=?", (first.job['id'],))
    receipt(store, first.job['id'], 'model', 'draft', 'unknown')
    store.enqueue(pid, 'analyze', input_version='collect:batch-1')
    again, sent = JobExecution(store, store.claim()), []
    with pytest.raises(dispatch.NotSent, match='unknown_not_resent'):
        again.call('model', 'draft', ok(sent))
    again.call('model', 'another', ok(sent))
    assert sent == [1]
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE id=?", (again.job['id'],))
    store.enqueue(pid, 'analyze', input_version=dispatch.REDO_PREFIX + 'batch-1')
    redo = JobExecution(store, store.claim())
    redo.call('model', 'draft', ok(sent))  # the owner's explicit redo may resend
    assert sent == [1, 1]


def test_unknown_in_the_same_job_stops_only_that_request(env):
    store, pid = env
    work, sent = job(store, pid, 'analyze'), []

    def lost():
        raise TimeoutError('response lost')
    with pytest.raises(TimeoutError):
        work.call('model', 'first', lost)
    with pytest.raises(dispatch.NotSent, match='unknown_not_resent'):
        work.call('model', 'first', ok(sent))
    work.call('model', 'second', ok(sent))
    assert sent == [1]


@pytest.mark.parametrize("legacy_limit", [None, 0, 2, 400])
def test_legacy_monthly_limits_do_not_block_and_accounting_is_preserved(env, legacy_limit):
    store, pid = env
    # Existing database settings remain audit history, not active admission limits.
    with store._connect() as db:
        db.execute(dispatch.SCHEMA)
        if legacy_limit is not None:
            db.execute('INSERT INTO studio_project_budgets VALUES (?,?,?)',
                       (pid, json.dumps({'model': legacy_limit}), now()))
    work, sent = job(store, pid, 'analyze'), []
    for i in range(401):
        receipt(store, work.job['id'], 'model', f'old-{i}', 'completed')
    receipt(store, work.job['id'], 'model', 'unclear', 'unknown')
    receipt(store, work.job['id'], 'model', 'never-sent', 'not_sent')
    before = rows(store)
    work.call('model', 'new-request', ok(sent))
    assert sent == [1]
    assert [r for r in rows(store) if r in before] == before
    with store._connect() as db:
        assert dispatch.spent(db, pid, 'model') == 403
        saved = db.execute('SELECT monthly FROM studio_project_budgets WHERE project_id=?', (pid,)).fetchone()
        assert (json.loads(saved[0]) if saved else None) == (None if legacy_limit is None else {'model': legacy_limit})
    with pytest.raises(dispatch.NotSent, match='unknown_not_resent'):
        work.call('model', 'unclear', ok(sent))
    assert sent == [1]



def test_pause_is_a_definite_not_sent(env):
    store, pid = env
    work, sent = job(store, pid, 'collect'), []
    with store._connect() as db:
        db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (pid,))
    before = rows(store)
    with pytest.raises(dispatch.NotSent, match='project_paused'):
        work.call('open_data', 'paper', ok(sent))
    assert sent == [] and rows(store) == before


def test_completed_answers_replay_within_a_job_but_not_across_jobs(env):
    store, pid = env
    first, sent = job(store, pid, 'collect'), []
    first.call('search', 'news query', ok(sent))
    first.call('search', 'news query', ok(sent))
    assert sent == [1]  # same job: replayed, not paid twice
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE id=?", (first.job['id'],))
    job(store, pid, 'collect').call('search', 'news query', ok(sent))
    assert sent == [1, 1]  # a new round fetches fresh results


def test_side_jobs_follow_the_same_rule(env):
    store, pid = env
    side, sent = job(store, pid, 'business'), []
    receipt(store, side.job['id'], 'model', 'pending', 'unknown')
    with pytest.raises(dispatch.NotSent, match='unknown_not_resent'):
        side.call('model', 'pending', ok(sent))
    side.call('model', 'other', ok(sent))  # no job-wide hold
    assert sent == [1]


def test_archive_ends_retired_jobs_without_touching_requests(env):
    store, pid = env
    running = legacy_job(store, pid, 'external', status='running')
    receipt(store, running['id'], 'model', 'pending', 'unknown')
    legacy_job(store, pid, 'external_followup', input_version='queued-followup')
    store.enqueue(pid, 'brief', input_version='side-feature')
    failed = job(store, pid, 'brief')
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (failed.job['id'],))
    before = rows(store)
    archived = dispatch.archive_legacy(store)
    assert running['id'] in archived and len(archived) == 2
    assert dispatch.archive_legacy(store) == []  # idempotent
    with store._connect() as db:
        statuses = dict(db.execute('SELECT kind,status FROM studio_jobs WHERE project_id=? AND id!=?',
                                   (pid, failed.job['id'])).fetchall())
        assert db.execute('SELECT status FROM studio_jobs WHERE id=?', (failed.job['id'],)).fetchone()[0] == 'failed'
    assert statuses['external'] == 'archived' and statuses['external_followup'] == 'archived'
    assert rows(store) == before
    # The queue never hands out archived work; the unknown request still counts as spent.
    claimed = store.claim()
    assert claimed is None or claimed['status'] != 'archived'
    with store._connect() as db:
        assert dispatch.spent(db, pid, 'model') == 1
    assert json.dumps(before)  # receipts remain plain data for audit


@pytest.mark.parametrize('block', ['project_not_consented', 'provider_backoff', 'request_in_flight'])
def test_removing_quotas_keeps_dispatch_safety_gates(env, block):
    store, pid = env
    work, sent = job(store, pid, 'collect'), []
    with store._connect() as db:
        if block == 'project_not_consented':
            db.execute('UPDATE studio_projects SET consent_at=NULL WHERE project_id=?', (pid,))
        elif block == 'provider_backoff':
            db.execute('INSERT INTO studio_provider_backoff VALUES (?,?,?)',
                       (hashlib.sha256(b'synthetic').hexdigest(), '2099-01-01T00:00:00+00:00', 429))
        else:
            old = legacy_job(store, pid, 'external', status='running')
            db.execute("UPDATE studio_jobs SET lease_until='2099-01-01T00:00:00+00:00' WHERE id=?", (old['id'],))
    if block == 'request_in_flight':
        receipt(store, old['id'], 'search', 'new', 'started')
    before = rows(store)
    with pytest.raises(dispatch.NotSent, match=block):
        work.call('search', 'new', ok(sent), provider='synthetic')
    assert sent == [] and rows(store) == before
