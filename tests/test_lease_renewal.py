"""A job keeps its lease through its own long write transaction; a lapsed lease stays lapsed."""
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
import test_dispatch_rule as fixtures
from test_dispatch_rule import job

from project_watch.studio.store import now


@pytest.fixture
def env(tmp_path, monkeypatch):
    return fixtures.env.__wrapped__(tmp_path, monkeypatch)


def lease(store, job_id):
    with closing(store._connect()) as db:
        return db.execute('SELECT lease_until FROM studio_jobs WHERE id=?', (job_id,)).fetchone()[0]


def set_lease(store, job_id, seconds):
    with closing(store._connect()) as db, db:
        db.execute('UPDATE studio_jobs SET lease_until=? WHERE id=?',
                   ((datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(), job_id))


def guarded_writes(store, work, pause):
    """Two writes in one transaction, as a long apply makes, `pause` seconds apart."""
    with store.write_scope(work._guard), closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO studio_state VALUES ('lease-test-1','x') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        time.sleep(pause)
        db.execute("INSERT INTO studio_state VALUES ('lease-test-2','x') ON CONFLICT(key) DO UPDATE SET value=excluded.value")


def test_a_write_transaction_outlasting_the_lease_renews_it_and_completes(env):
    # The heartbeat cannot renew while this transaction holds the write lock; a live business
    # job failed its next write after holding the lock past its lease.
    store, pid = env
    work = job(store, pid, 'business')
    set_lease(store, work.job['id'], 1)
    guarded_writes(store, work, 1.5)
    assert lease(store, work.job['id']) > (datetime.now(UTC) + timedelta(seconds=80)).isoformat()
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM studio_state WHERE key LIKE 'lease-test-%'").fetchone()[0] == 2


def test_a_lease_that_lapses_while_this_transaction_holds_the_lock_stays_ours(env, monkeypatch):
    # A live apply recomputed its inputs inside the lock for over two minutes without a write;
    # nobody could reclaim the job meanwhile, so its next write continues and renews.
    from project_watch.studio import execution
    monkeypatch.setattr(execution, 'LEASE_RENEW_BELOW', timedelta(0))
    store, pid = env
    work = job(store, pid, 'business')
    set_lease(store, work.job['id'], 1)
    guarded_writes(store, work, 1.5)
    assert lease(store, work.job['id']) > (datetime.now(UTC) + timedelta(seconds=80)).isoformat()


def test_a_read_only_transaction_is_not_fenced_at_commit(env):
    store, pid = env
    work = job(store, pid, 'business')
    set_lease(store, work.job['id'], -1)
    with store.write_scope(work._guard), closing(store._connect()) as db, db:
        db.execute('BEGIN')
        db.execute('SELECT COUNT(*) FROM studio_jobs').fetchone()
    with pytest.raises(RuntimeError, match='lease expired'), store.write_scope(work._guard),             closing(store._connect()) as db, db:
        db.execute('BEGIN')
        db.execute("INSERT INTO studio_state VALUES ('lease-test-1','x')")


def test_a_lease_with_time_left_is_not_rewritten_on_every_write(env):
    store, pid = env
    work = job(store, pid, 'business')
    before = lease(store, work.job['id'])
    guarded_writes(store, work, 0)
    assert lease(store, work.job['id']) == before


def test_a_lapsed_lease_is_never_revived(env):
    store, pid = env
    work = job(store, pid, 'business')
    set_lease(store, work.job['id'], -1)
    before = lease(store, work.job['id'])
    with pytest.raises(RuntimeError, match='lease expired'):
        guarded_writes(store, work, 0)
    assert lease(store, work.job['id']) == before
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM studio_state WHERE key LIKE 'lease-test-%'").fetchone()[0] == 0


def test_a_replaced_lease_is_never_renewed(env):
    store, pid = env
    work = job(store, pid, 'business')
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET lease_token='other' WHERE id=?", (work.job['id'],))
    set_lease(store, work.job['id'], 1)
    before = lease(store, work.job['id'])
    with pytest.raises(RuntimeError, match='replaced'):
        guarded_writes(store, work, 0)
    assert lease(store, work.job['id']) == before


def test_a_lease_check_outside_a_transaction_stays_a_read(env):
    # Read-only checks must not wait for the write lock another lane's long apply holds.
    store, pid = env
    work = job(store, pid, 'business')
    set_lease(store, work.job['id'], 30)
    before = lease(store, work.job['id'])
    with closing(store._connect()) as holder:
        holder.execute('BEGIN IMMEDIATE')
        with closing(store._connect()) as db:
            started = time.monotonic()
            work._guard(db)
            assert time.monotonic() - started < 2
            assert not db.in_transaction
        holder.rollback()
    assert lease(store, work.job['id']) == before


def test_a_renewal_at_commit_is_saved_with_the_transaction(env):
    # FencedConnection checks the lease again at commit; a renewal there commits with the writes.
    store, pid = env
    work = job(store, pid, 'business')
    set_lease(store, work.job['id'], 30)
    with store.write_scope(work._guard), closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO studio_state VALUES ('lease-test-1','x')")
    assert lease(store, work.job['id']) > (datetime.now(UTC) + timedelta(seconds=80)).isoformat()


def test_an_overdue_request_is_left_to_the_watchdog(env):
    store, pid = env
    work = job(store, pid, 'business')
    with closing(store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at,deadline_at) "
                   "VALUES (?,?,?,?,?,?,?)", (work.job['id'], 'k', 'model', 'synthetic', 'started', now(),
                                              (datetime.now(UTC) - timedelta(seconds=1)).isoformat()))
    set_lease(store, work.job['id'], 30)
    before = lease(store, work.job['id'])
    guarded_writes(store, work, 0)
    assert lease(store, work.job['id']) == before


def test_an_overdue_request_ends_a_lease_that_lapsed_under_the_lock(env, monkeypatch):
    from project_watch.studio import execution
    monkeypatch.setattr(execution, 'LEASE_RENEW_BELOW', timedelta(0))
    store, pid = env
    work = job(store, pid, 'business')
    with closing(store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at,deadline_at) "
                   "VALUES (?,?,?,?,?,?,?)", (work.job['id'], 'k', 'model', 'synthetic', 'started', now(),
                                              (datetime.now(UTC) - timedelta(seconds=1)).isoformat()))
    set_lease(store, work.job['id'], 1)
    with pytest.raises(RuntimeError, match='lease expired'):
        guarded_writes(store, work, 1.5)


def test_a_transaction_ended_through_a_cursor_does_not_carry_its_lease_into_the_next(env, monkeypatch):
    from project_watch.studio import execution
    monkeypatch.setattr(execution, 'LEASE_RENEW_BELOW', timedelta(0))
    store, pid = env
    work = job(store, pid, 'business')
    set_lease(store, work.job['id'], 1)
    with store.write_scope(work._guard), closing(store._connect()) as db:
        db.execute("INSERT INTO studio_state VALUES ('lease-test-1','x')")
        db.cursor().execute('ROLLBACK')
        time.sleep(1.5)
        with pytest.raises(RuntimeError, match='lease expired'):
            db.execute("INSERT INTO studio_state VALUES ('lease-test-2','x')")


def test_a_with_write_keeps_the_commit_check(env):
    store, pid = env
    work = job(store, pid, 'business')
    set_lease(store, work.job['id'], 1)
    with pytest.raises(RuntimeError, match='lease expired'), store.write_scope(work._guard),             closing(store._connect()) as db, db:
        db.execute('BEGIN')
        db.execute("WITH v(k) AS (SELECT 'lease-test-1') INSERT INTO studio_state SELECT k,'x' FROM v")
        time.sleep(1.5)
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM studio_state WHERE key LIKE 'lease-test-%'").fetchone()[0] == 0
