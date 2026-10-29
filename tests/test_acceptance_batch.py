"""Finite opt-in batches through real journal/admission paths; synthetic I/O only."""
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from project_watch.studio import acceptance_batch as batch
from project_watch.studio import public_stage_work
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response
from project_watch.studio.store import StudioStore


def caps(n):
    return dict.fromkeys(batch.KINDS, n)


def admit(pipeline, job, n=4):
    batch.create(pipeline.store, 'synthetic-batch', deadline=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                 caps=caps(n), journeys=[dict(project_id=job['project_id'], caps=caps(n), roots=['first', 'next'])],
                 plan_sha256=batch.digest({'public_inputs': ['https://example.invalid/'], 'rubric': 'synthetic'}))
    batch.bind_root(pipeline.store, 'synthetic-batch', job['id'], 'first')


def setup(tmp_path, n=4):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Batch').id
    pipeline.store.upsert_project(pid, {'legal_name': 'Synthetic Batch', 'website': 'https://example.invalid'}, consent=True)
    job = pipeline.store.enqueue(pid, 'external')
    admit(pipeline, job, n)
    return pipeline, JobExecution(pipeline.store, pipeline.store.claim())


def finish(pipeline, execution):
    assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])


def call(execution, key, kind='model'):
    return execution.call(kind, key, lambda: Response(200, {}, b'{}'))


def usage(store, kind='model'):
    with closing(store._connect()) as db:
        return batch.usage(db, 'synthetic-batch', kind)


def test_descendants_unique_accounting_restart_cache_and_next_root(tmp_path):
    pipeline, root = setup(tmp_path, 3)
    call(root, 'root')
    finish(pipeline, root)
    a = pipeline.store.enqueue(root.job['project_id'], 'scope', parent_job_id=root.job['id'])
    child = JobExecution(pipeline.store, pipeline.store.claim())
    call(child, 'child')
    finish(pipeline, child)
    b = pipeline.store.enqueue(root.job['project_id'], 'assessment', parent_job_id=a['id'])
    with closing(pipeline.store._connect()) as db, db:
        public_stage_work.inherit(db, a['id'], [root.job['id']])
        public_stage_work.inherit(db, b['id'], [root.job['id'], a['id']])
    pipeline.store = StudioStore(pipeline.store.db_path)
    tail = JobExecution(pipeline.store, pipeline.store.claim())
    assert usage(pipeline.store) == 2
    call(tail, 'tail')
    assert usage(pipeline.store) == 3
    rebuilt = JobExecution(StudioStore(pipeline.store.db_path), tail.job)
    assert rebuilt.call('model', 'tail', lambda: pytest.fail('cache must not send')).status == 200
    with pytest.raises(HttpError, match='acceptance_budget_exhausted'):
        call(rebuilt, 'overflow')
    finish(pipeline, tail)
    next_job = pipeline.store.enqueue(root.job['project_id'], 'external', automatic=True)
    batch.bind_root(pipeline.store, 'synthetic-batch', next_job['id'], 'next')
    next_run = JobExecution(pipeline.store, pipeline.store.claim())
    with pytest.raises(HttpError, match='acceptance_budget_exhausted'):
        call(next_run, 'new-cycle')
    assert usage(pipeline.store) == 3
    # A genuinely separate interpreter reads the same persistent admission.
    script = '''import sys
sys.path.insert(0, sys.argv[2])
from project_watch.studio.store import StudioStore
from project_watch.studio import acceptance_batch as b
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
s=StudioStore(sys.argv[1]); d=s._connect()
assert b.usage(d,'synthetic-batch','model')==3
j=dict(d.execute("SELECT * FROM studio_jobs WHERE status='running'").fetchone()); d.close()
def never():
    raise AssertionError('fresh process cannot reset admission')
try:
    JobExecution(s,j).call('model','fresh-process',never)
except HttpError as e:
    assert str(e)=='acceptance_budget_exhausted'
else:
    raise AssertionError('missing admission gate')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(pipeline.store.db_path),
                             str(Path(batch.__file__).resolve().parents[2])], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_last_allowance_is_reserved_before_transport_across_connections(tmp_path):
    pipeline, root = setup(tmp_path, 1)
    pipeline.store.enqueue(root.job['project_id'], 'scope', parent_job_id=root.job['id'])
    # claim permits independent stages; use actual leases, not fabricated tokens.
    other = pipeline.store.claim()
    assert other and other['kind'] == 'scope'
    barrier = threading.Barrier(2)
    entered = []
    lock = threading.Lock()
    def worker(job):
        execution = JobExecution(StudioStore(pipeline.store.db_path), job)
        barrier.wait(timeout=20)
        def transport():
            with lock:
                entered.append(job['id'])
            return Response(200, {}, b'{}')
        try:
            execution.call('model', 'last', transport)
            return 'sent'
        except HttpError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [root.job, other]))
    assert sorted(results) == ['acceptance_budget_exhausted', 'sent']
    assert len(entered) == usage(pipeline.store) == 1


@pytest.mark.parametrize('boundary', ['expired', 'closed', 'identity', 'unbound', 'lower'])
def test_refusal_before_started_and_cached_reply_still_local(tmp_path, boundary):
    pipeline, execution = setup(tmp_path)
    call(execution, 'cached')
    with closing(pipeline.store._connect()) as db, db:
        if boundary == 'expired':
            db.execute("UPDATE studio_acceptance_batches SET deadline='2000-01-01T00:00:00+00:00'")
        elif boundary == 'closed':
            batch.close(pipeline.store, 'synthetic-batch')
        elif boundary == 'identity':
            db.execute("UPDATE studio_projects SET legal_name='Changed'")
        elif boundary == 'unbound':
            # Real new unregistered queue entry, without deleting the original binding.
            pipeline.store.enqueue(execution.job['project_id'], 'scope')
            execution = JobExecution(pipeline.store, pipeline.store.claim())
    if boundary == 'lower':
        execution = JobExecution(pipeline.store, execution.job, budgets=caps(0))
    if boundary != 'unbound':
        execution.call('model', 'cached', lambda: pytest.fail('local cache'))
    for kind in batch.KINDS:
        with pytest.raises(HttpError, match=r'acceptance_|request_budget'):
            execution.call(kind, 'blocked', lambda: pytest.fail('must not send'))
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_job_calls').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE status='started'").fetchone()[0] == 0


def test_unknown_counts_and_never_retries_without_consent(tmp_path):
    pipeline, execution = setup(tmp_path, 1)
    with pytest.raises(HttpError):
        execution.call('model', 'lost', lambda: (_ for _ in ()).throw(HttpError('connection_failed')))
    rebuilt = JobExecution(StudioStore(pipeline.store.db_path), execution.job)
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        rebuilt.call('model', 'lost', lambda: pytest.fail('old unknown'))
    with pytest.raises(HttpError, match='acceptance_budget_exhausted'):
        rebuilt.call('model', 'different', lambda: pytest.fail('new key cannot reset cap'))
    assert usage(pipeline.store) == 1
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT status FROM studio_job_calls").fetchone()[0] == 'unknown'


def test_binding_rollback_immutability_and_legacy_followup(tmp_path):
    pipeline, root = setup(tmp_path)
    before = pipeline.store.enqueue(root.job['project_id'], 'scope')
    with pytest.raises(HttpError, match='acceptance_unbound_successor'):
        pipeline.store.enqueue(root.job['project_id'], 'assessment', parent_job_id=before['id'])
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_jobs WHERE kind='assessment'").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        admit(pipeline, root.job)
    with pytest.raises(sqlite3.IntegrityError):
        batch.bind_root(pipeline.store, 'synthetic-batch', root.job['id'], 'first')
    follow = pipeline.store.queue_external_followup(root.job, {'synthetic': True})
    with closing(pipeline.store._connect()) as db:
        members = [dict(row) for row in db.execute('SELECT * FROM studio_acceptance_jobs')]
    assert len(members) == 3
    assert {m['parent_job_id'] for m in members if m['job_id'] != root.job['id']} == {root.job['id']}
    assert follow['id'] in {m['job_id'] for m in members}


def test_real_pipeline_first_analysis_followup(tmp_path, monkeypatch):
    import test_analysis_handoff_path as path
    original = path.install
    holder = []
    def install(*args, **kwargs):
        pipeline, root, seen = original(*args, **kwargs)
        admit(pipeline, root.job, 100)
        holder.append(pipeline)
        return pipeline, root, seen
    monkeypatch.setattr(path, 'install', install)
    path.test_first_publication_and_followup_share_root_and_final_predecessor_usage(tmp_path, monkeypatch)
    with closing(holder[0].store._connect()) as db:
        missing = db.execute("SELECT kind FROM studio_jobs WHERE kind IN ('external','insight','external_followup') AND id NOT IN (SELECT job_id FROM studio_acceptance_jobs)").fetchall()
        assert not missing
        assert batch.usage(db, 'synthetic-batch', 'model') <= 40


@pytest.mark.parametrize('kind', ['scope', 'assessment'])
def test_public_stage_entry_inherits(tmp_path, kind):
    pipeline, root = setup(tmp_path)
    call(root, 'before')
    finish(pipeline, root)
    with closing(pipeline.store._connect()) as db, db:
        job = public_stage_work.enqueue(pipeline, db, root.job['project_id'], kind,
                                       {'synthetic': True}, origin=root.job['id'])
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT parent_job_id FROM studio_acceptance_jobs WHERE job_id=?', (job['id'],)).fetchone()[0] == root.job['id']
    assert usage(pipeline.store) == 1


def test_old_database_migration_reopen_preserves_journal(tmp_path):
    pipeline, root = setup(tmp_path)
    call(root, 'old')
    # Synthetic pre-feature store: only remove the three newly introduced empty/opt-in tables.
    with closing(pipeline.store._connect()) as db, db:
        for name in ('releases', 'jobs', 'projects', 'batches'):
            db.execute('DROP TABLE studio_acceptance_' + name)
        original = tuple(db.execute('SELECT * FROM studio_job_calls').fetchone())
    for _ in range(2):
        store = StudioStore(pipeline.store.db_path)
        with closing(store._connect()) as db:
            assert tuple(db.execute('SELECT * FROM studio_job_calls').fetchone()) == original
            assert db.execute('SELECT count(*) FROM studio_acceptance_batches').fetchone()[0] == 0
    # No opt-in: original behavior continues.
    call(JobExecution(store, root.job), 'ordinary')


def test_terminal_recovery_new_job_inherits_without_history_reset(tmp_path, monkeypatch):
    import test_studio_terminal_recovery as terminal
    original = terminal.fixtures.loop.__wrapped__
    def loop(folder):
        result = original(folder)
        admit(result.pipeline, result.execution.job, 3)
        return result
    # Hook before the original fixture journals its first request.
    monkeypatch.setattr(terminal.fixtures.loop, '__wrapped__', loop)
    fixture = terminal.terminal.__wrapped__(tmp_path, monkeypatch)
    terminal.test_terminal_recovery_keeps_history_and_links_new_request(fixture)
    with closing(fixture.store._connect()) as db:
        members = list(db.execute('SELECT * FROM studio_acceptance_jobs'))
        assert len(members) == 2 and batch.usage(db, 'synthetic-batch', 'model') == 3
        child = next(m for m in members if m['parent_job_id'])
        assert child['parent_job_id'] == fixture.execution.job['id']
        job = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (child['job_id'],)).fetchone())
    with pytest.raises(HttpError, match='acceptance_budget_exhausted'):
        call(JobExecution(fixture.store, job), 'extra')


def test_two_journeys_static_allocations_and_cross_project_parent_rejected(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    p = app.state.studio_pipeline
    jobs = []
    for name in ('Synthetic Existing', 'Synthetic Independent'):
        pid = p.repository.create_project(name).id
        p.store.upsert_project(pid, {'legal_name': name}, consent=True)
        jobs.append(p.store.enqueue(pid, 'external'))
    deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    journeys = [dict(project_id=j['project_id'], caps=caps(1), roots=['one']) for j in jobs]
    with pytest.raises(ValueError, match='allocations exceed'):
        batch.create(p.store, 'invalid', deadline=deadline, caps=caps(1), journeys=journeys, plan_sha256='a'*64)
    batch.create(p.store, 'synthetic-batch', deadline=deadline, caps=caps(2), journeys=journeys, plan_sha256='a'*64)
    for job in jobs:
        batch.bind_root(p.store, 'synthetic-batch', job['id'], 'one')
        run = JobExecution(p.store, p.store.claim())
        call(run, 'one')
        with pytest.raises(HttpError, match='acceptance_budget_exhausted'):
            call(run, 'steal-other-allocation')
        finish(p, run)
    with pytest.raises(HttpError, match='acceptance_unbound_successor'):
        p.store.enqueue(jobs[1]['project_id'], 'scope', parent_job_id=jobs[0]['id'])
    assert usage(p.store) == 2


def test_closeout_sees_shared_remainder_and_inflight_reply_survives_close(tmp_path):
    from project_watch.studio.dsh_closeout import request_budget
    p, root = setup(tmp_path, 10)
    call(root, 'one')
    p.store.enqueue(root.job['project_id'], 'scope', parent_job_id=root.job['id'])
    child = JobExecution(p.store, p.store.claim(), budgets={'model': 2})
    assert request_budget(root)['model']['remaining'] == 9
    assert request_budget(child)['model']['remaining'] == 2
    def transport():
        assert usage(p.store) == 2  # started has already consumed the shared slot
        batch.close(p.store, 'synthetic-batch')
        return Response(200, {}, b'{}')
    child.call('model', 'inflight', transport)
    assert request_budget(child)['model']['remaining'] == 0
    child.call('model', 'inflight', lambda: pytest.fail('settled cache'))
    with closing(p.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_job_calls WHERE status='completed'").fetchone()[0] == 2
