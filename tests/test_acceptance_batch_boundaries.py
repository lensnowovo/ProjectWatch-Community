"""Release never resets old work; coalescing never invents a parent."""
from contextlib import closing
from datetime import UTC, datetime

import pytest
import test_acceptance_batch as fixtures

from project_watch.studio import acceptance_batch as batch
from project_watch.studio import event_insight_work, public_stage_work
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response
from project_watch.studio.store import StudioStore


def snapshot(store):
    with closing(store._connect()) as db:
        return {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('studio_jobs', 'studio_job_calls', 'studio_job_checkpoints',
                              'studio_job_budget_links', 'studio_acceptance_jobs')}


@pytest.mark.parametrize('end', ['closed', 'expired'])
def test_safe_release_keeps_old_fence_and_allows_only_new_independent_rounds(tmp_path, end):
    p, root = fixtures.setup(tmp_path, 4)
    fixtures.call(root, 'saved')
    fixtures.finish(p, root)
    if end == 'closed':
        batch.close(p.store, 'synthetic-batch')
    else:
        with closing(p.store._connect()) as db, db:
            db.execute("UPDATE studio_acceptance_batches SET deadline='2000-01-01T00:00:00+00:00'")
    before = snapshot(p.store)
    batch.release(p.store, 'synthetic-batch')
    assert snapshot(p.store) == before
    p.store = StudioStore(p.store.db_path)  # persisted release, not process memory
    with closing(p.store._connect()) as db:
        released = tuple(db.execute('SELECT * FROM studio_acceptance_releases').fetchone())
        with pytest.raises(HttpError, match='acceptance_' + end):
            batch.guard_send(db, root.job, 'model')
    ordinary = p.store.enqueue(root.job['project_id'], 'external')
    batch.release(p.store, 'synthetic-batch')  # cannot move the fence past a new round
    with closing(p.store._connect()) as db:
        assert tuple(db.execute('SELECT * FROM studio_acceptance_releases').fetchone()) == released
    run = JobExecution(p.store, p.store.claim())
    assert run.job['id'] == ordinary['id'] and batch.remaining(p.store, run.job) is None
    fixtures.call(run, 'new-ordinary')
    fixtures.finish(p, run)
    child = p.store.enqueue(root.job['project_id'], 'scope', parent_job_id=ordinary['id'])
    tail = JobExecution(p.store, p.store.claim())
    assert tail.job['id'] == child['id']
    fixtures.call(tail, 'new-ordinary-child')
    fixtures.finish(p, tail)
    stable = snapshot(p.store)
    with pytest.raises(HttpError, match='acceptance_' + end):
        p.store.enqueue(root.job['project_id'], 'assessment', parent_job_id=root.job['id'])
    assert snapshot(p.store) == stable
    with closing(p.store._connect()) as db, db:
        with pytest.raises(HttpError, match='acceptance_old_successor'):
            batch.guard_transfer(db, root.job['id'], ordinary['id'])
        # Even an imported old ledger cannot turn a new ID into a fresh allowance.
        public_stage_work.inherit(db, ordinary['id'], [root.job['id']])
        with pytest.raises(HttpError, match='acceptance_' + end):
            batch.guard_send(db, ordinary, 'model')
    assert fixtures.usage(p.store) == 1  # daily requests never inflate/reset the closed batch


@pytest.mark.parametrize('blocker', ['open', 'running', 'queued', 'unknown', 'started', '500', 'hold'])
def test_release_requires_explicit_end_drain_and_settled_protection(tmp_path, blocker):
    p, root = fixtures.setup(tmp_path)
    if blocker == 'unknown':
        with pytest.raises(HttpError):
            root.call('model', 'lost', lambda: (_ for _ in ()).throw(HttpError('connection_failed')))
    if blocker == '500':
        root.call('model', 'bad', lambda: Response(500, {}, b'{}'))
    if blocker == 'started':
        fixtures.call(root, 'synthetic-interrupted')
        with closing(p.store._connect()) as db, db:
            db.execute("UPDATE studio_job_calls SET status='started'")
    if blocker == 'hold':
        root.checkpoint('dsh_hold', {'code': 'synthetic_manual_hold'})
    if blocker != 'running':
        fixtures.finish(p, root)
    if blocker == 'queued':
        p.store.enqueue(root.job['project_id'], 'scope', parent_job_id=root.job['id'])
    if blocker != 'open':
        batch.close(p.store, 'synthetic-batch')
    before = snapshot(p.store)
    with pytest.raises(HttpError):
        batch.release(p.store, 'synthetic-batch')
    assert snapshot(p.store) == before
    with closing(p.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_acceptance_releases').fetchone()[0] == 0


def test_release_preserves_project_pause_and_old_nonmember_jobs(tmp_path):
    p, root = fixtures.setup(tmp_path)
    fixtures.finish(p, root)
    old = p.store.enqueue(root.job['project_id'], 'scope')
    fixtures.finish(p, JobExecution(p.store, p.store.claim()))
    p.store.set_status(root.job['project_id'], 'paused')
    batch.close(p.store, 'synthetic-batch')
    batch.release(p.store, 'synthetic-batch')
    assert p.store.get_project(root.job['project_id'])['status'] == 'paused'
    with closing(p.store._connect()) as db, pytest.raises(HttpError, match='acceptance_closed'):
        batch.guard_send(db, old, 'model')


def test_safe_release_does_not_reuse_closed_failure_as_new_periodic_root(tmp_path):
    p, root = fixtures.setup(tmp_path)
    batch.close(p.store, 'synthetic-batch')
    assert p.store.finish(root.job['id'], error='acceptance_closed', lease_token=root.job['lease_token'])
    batch.release(p.store, 'synthetic-batch')
    before = snapshot(p.store)
    with pytest.raises(HttpError, match='acceptance_closed'):
        p.store.enqueue(root.job['project_id'], 'external', automatic=True, parent_job_id=root.job['id'])
    assert snapshot(p.store) == before
    new = p.store.enqueue(root.job['project_id'], 'external', automatic=True)
    assert new['id'] != root.job['id'] and new['status'] == 'queued'
    fixtures.call(JobExecution(p.store, p.store.claim()), 'independent-next')
    with closing(p.store._connect()) as db:
        assert dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (root.job['id'],)).fetchone())['error'] == 'acceptance_closed'


def stage(p, db, root, kind):
    if kind == 'insight':
        return event_insight_work._enqueue(p, db, root.job['project_id'], root.job['id'],
                                           dict(cap=4, objects=[{'fingerprint': 'synthetic-input'}]))
    return public_stage_work.enqueue(p, db, root.job['project_id'], kind, {'synthetic': True}, origin=root.job['id'])


def version(root, kind):
    if kind == 'insight':
        return 'event-insight:' + event_insight_work.digest([root.job['id'], ['synthetic-input'], None])
    return kind + ':' + public_stage_work.digest([root.job['id'], {'synthetic': True}])


@pytest.mark.parametrize('kind', ['insight', 'scope', 'assessment'])
@pytest.mark.parametrize('conflict', ['input', 'unproven_parent', 'different_parent'])
def test_real_stage_coalescing_checks_input_and_parent_before_any_write(tmp_path, kind, conflict):
    p, root = fixtures.setup(tmp_path)
    kwargs = dict(input_version='unrelated-existing-input' if conflict == 'input' else version(root, kind))
    if conflict == 'different_parent':
        alternate = p.store.enqueue(root.job['project_id'], 'brief', parent_job_id=root.job['id'])
        kwargs['parent_job_id'] = alternate['id']
    existing = p.store.enqueue(root.job['project_id'], kind, **kwargs)
    before = snapshot(p.store)
    if conflict == 'input':
        with closing(p.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            assert stage(p, db, root, kind) is None
    else:
        with pytest.raises(HttpError, match='acceptance_parent_'), closing(p.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            assert stage(p, db, root, kind)['id'] == existing['id']
    assert snapshot(p.store) == before


@pytest.mark.parametrize('kind', ['insight', 'scope', 'assessment'])
def test_exact_stage_replay_keeps_input_parent_and_budget(tmp_path, kind):
    p, root = fixtures.setup(tmp_path)
    with closing(p.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        job = stage(p, db, root, kind)
    before = snapshot(p.store)
    with closing(p.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        assert stage(p, db, root, kind)['id'] == job['id']
    assert snapshot(p.store) == before


def test_legacy_brief_coalescing_rejects_unproven_parent_atomically(tmp_path):
    p, root = fixtures.setup(tmp_path)
    p.store.enqueue(root.job['project_id'], 'brief')
    before = snapshot(p.store)
    with pytest.raises(HttpError, match='acceptance_parent_unproven'):
        p.store.queue_external_followup(root.job, {'synthetic': True})
    assert snapshot(p.store) == before


def test_normal_pipeline_after_release_keeps_old_materials_without_blocking_new_cycle(tmp_path, monkeypatch):
    import test_material_handoff_pipeline as path

    journey = path.journey.__wrapped__(tmp_path, monkeypatch)
    p, root, state = journey
    fixtures.admit(p, root.job, 100)
    pending = path.old_round(journey)
    batch.close(p.store, 'synthetic-batch')
    with pytest.raises(HttpError, match='acceptance_not_drained'):
        batch.release(p.store, 'synthetic-batch')
    worker = path.StudioWorker(p)
    for _ in range(5):
        # Drain already queued work through the real worker, without scheduling
        # extra periodic jobs into a deliberately closed batch.
        worker._last_schedule = datetime.now(UTC)
        if not worker.step():
            break
    else:
        pytest.fail('finite closeout did not drain')
    batch.release(p.store, 'synthetic-batch')
    previous_calls = len(state['model'])
    current = path.scheduled(p, root.job['project_id'])
    p.run(current.job)
    receipt = current.read_checkpoint(path.material_handoff.STAGE)
    assert not receipt['transfers']
    assert receipt['retained'] == [dict(origin=root.job['id'], reason='acceptance_old_successor')]
    with closing(p.store._connect()) as db:
        assert dict(db.execute('SELECT id,job_id FROM studio_evidence_tasks WHERE status=\'pending\'')) == dict.fromkeys(pending, root.job['id'])
        assert db.execute('SELECT count(*) FROM studio_material_transfers').fetchone()[0] == 0
    assert len(state['model']) >= previous_calls  # only the independent new cycle may send
    fixtures.finish(p, current)
