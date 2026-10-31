"""The rewritten pipeline's single dispatch rule (studio/dispatch.py). No network."""
import hashlib
import json
import socket

import pytest

from project_watch.studio import dispatch
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response
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


def test_unknown_request_elsewhere_is_never_resent_but_independent_work_proceeds(env):
    store, pid = env
    legacy = job(store, pid, 'external')
    receipt(store, legacy.job['id'], 'model', 'same-request', 'unknown')
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (legacy.job['id'],))
    before, sent = rows(store), []
    work = job(store, pid, 'collect')
    with pytest.raises(dispatch.NotSent, match='unknown_not_resent'):
        work.call('model', 'same-request', ok(sent))
    assert sent == [] and rows(store) == before  # nothing sent, nothing recorded, old row untouched
    work.call('model', 'another-request', ok(sent))  # another job's pending request blocks nothing else
    assert sent == [1]


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


def test_budget_counts_every_dispatch_including_unknown(env):
    store, pid = env
    dispatch.set_monthly(store, pid, {'model': 2})
    work, sent = job(store, pid, 'analyze'), []
    receipt(store, work.job['id'], 'model', 'unclear', 'unknown')  # counts as spent
    work.call('model', 'one', ok(sent))
    with pytest.raises(dispatch.NotSent, match='project_budget'):
        work.call('model', 'two', ok(sent))
    assert sent == [1]
    work.call('open_data', 'paper', ok(sent))  # other kinds keep their own budget


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


def test_legacy_kinds_keep_their_original_rule_until_removed(env):
    store, pid = env
    old = job(store, pid, 'external')
    receipt(store, old.job['id'], 'model', 'pending', 'unknown')
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        old.call('model', 'other', ok([]))


def test_archive_ends_retired_jobs_without_touching_requests(env):
    store, pid = env
    running = job(store, pid, 'external')
    receipt(store, running.job['id'], 'model', 'pending', 'unknown')
    store.enqueue(pid, 'external_followup', input_version='queued-followup')
    store.enqueue(pid, 'brief', input_version='side-feature')
    failed = job(store, pid, 'brief')
    with store._connect() as db:
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (failed.job['id'],))
    before = rows(store)
    archived = dispatch.archive_legacy(store)
    assert running.job['id'] in archived and len(archived) == 2
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
