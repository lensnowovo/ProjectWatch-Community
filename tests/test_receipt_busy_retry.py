"""A received answer is recorded even when another writer holds the database past the busy wait."""
import sqlite3
import threading
import time

import pytest
from test_continuation_upgrade import setup

from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response


@pytest.fixture
def short_busy_wait(monkeypatch):
    """Make 'database is locked' arrive after 0.2 s instead of 10 s."""
    from project_watch.studio.store import StudioStore

    original = StudioStore._connect

    def connect(self, *args, **kwargs):
        db = original(self, *args, **kwargs)
        db.execute('PRAGMA busy_timeout = 200')
        return db
    monkeypatch.setattr(StudioStore, '_connect', connect)


def hold_write_lock(path, seconds):
    ready = threading.Event()

    def hold():
        db = sqlite3.connect(path, isolation_level=None)
        db.execute('BEGIN IMMEDIATE')
        ready.set()
        time.sleep(seconds)
        db.execute('COMMIT')
        db.close()
    thread = threading.Thread(target=hold)
    return thread, ready


def status(store, job, key='k'):
    with store._connect() as db:
        return db.execute('SELECT status FROM studio_job_calls WHERE job_id=?', (job['id'],)).fetchone()[0]


@pytest.mark.parametrize('outcome', ['answer', 'cut'])
def test_a_receipt_waits_out_another_writers_long_transaction(tmp_path, short_busy_wait, outcome):
    store, _pid, job = setup(tmp_path)
    execution = JobExecution(store, job)
    holder, ready = hold_write_lock(store.db_path, 1.5)

    def transport():
        holder.start()
        ready.wait(5)  # the lock is taken while the request is on the wire
        if outcome == 'cut':
            raise HttpError('connection_failed', transport_phase='response_body', failure_kind='timeout')
        return Response(200, {}, b'{"ok": true}')
    if outcome == 'answer':
        assert execution.call('model', 'k', transport).status == 200
    else:
        with pytest.raises(HttpError):
            execution.call('model', 'k', transport)
    holder.join(5)
    # Recorded, not left 'started' for the watchdog to judge as an overdue request.
    assert status(store, job) == ('completed' if outcome == 'answer' else 'unknown')


def test_other_database_errors_are_not_retried():
    from project_watch.studio.execution import _busy_retry
    calls = []

    def broken():
        calls.append(1)
        raise sqlite3.OperationalError('no such table: x')
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        _busy_retry(broken)
    assert len(calls) == 1
