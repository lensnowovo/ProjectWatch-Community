"""A write transaction held long enough to make other writers wait is logged with where it began."""
import logging
import sqlite3
import time

import pytest

from project_watch.studio import write_fencing


def connect(path):
    db = sqlite3.connect(path, factory=write_fencing.FencedConnection)
    db.execute('CREATE TABLE IF NOT EXISTS t (v)')
    db.commit()
    return db


def slow_writer(db):
    with db:
        db.execute('INSERT INTO t VALUES (1)')
        time.sleep(1.2)


def test_a_slow_write_transaction_is_logged_with_its_origin(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(write_fencing, 'SLOW_WRITE_SECONDS', 1.0)
    db = connect(tmp_path / 'db.sqlite3')
    with caplog.at_level(logging.WARNING, logger=write_fencing.__name__):
        with db:
            db.execute('INSERT INTO t VALUES (0)')  # quick: not logged
        slow_writer(db)
        db.execute('INSERT INTO t VALUES (2)')
        time.sleep(1.2)
        db.rollback()
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2 and all('write transaction held' in m for m in messages)
    assert 'slow_writer' in messages[0] and 'test_a_slow_write_transaction' in messages[1]


def test_the_log_says_whether_the_time_went_to_sql_or_to_code_between_statements(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(write_fencing, 'SLOW_WRITE_SECONDS', 0.5)
    db = connect(tmp_path / 'db.sqlite3')
    db.create_function('pause', 1, lambda seconds: time.sleep(seconds) or 0)
    with caplog.at_level(logging.WARNING, logger=write_fencing.__name__):
        with db:
            db.execute('INSERT INTO t VALUES (1)')
            db.execute('SELECT pause(?)', (0.6,)).fetchone()  # slow SQL, values not logged
        with db:
            db.execute('INSERT INTO t VALUES (2)')
            time.sleep(0.6)  # slow code between statements
    sql, code = [r.getMessage() for r in caplog.records]
    assert 'in SQL: 0.6 s SELECT pause(?))' in sql  # the slowest statement, without its values
    assert '0.0 s in SQL)' in code  # the time went to code between statements


def test_reads_are_not_timed(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(write_fencing, 'SLOW_WRITE_SECONDS', 0.0)
    db = connect(tmp_path / 'db.sqlite3')
    with caplog.at_level(logging.WARNING, logger=write_fencing.__name__):
        db.execute('SELECT * FROM t').fetchall()
    assert not caplog.records


def _worker_with_job(tmp_path):
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.pipeline import StudioWorker

    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project('Synthetic Busy Co').id
    p.store.upsert_project(pid, {'legal_name': 'Synthetic Busy Co'}, consent=True)
    job = p.store.enqueue(pid, 'brief', input_version='manual:busy')
    worker = StudioWorker(p)
    worker.schedules = False
    return p, worker, job


def status(p, job_id):
    from contextlib import closing
    with closing(p.store._connect()) as db:
        return db.execute('SELECT status, error FROM studio_jobs WHERE id=?', (job_id,)).fetchone()


def test_a_job_that_finds_the_database_busy_goes_back_in_the_queue(tmp_path, monkeypatch):
    p, worker, job = _worker_with_job(tmp_path)
    monkeypatch.setattr(p, 'run', lambda job: (_ for _ in ()).throw(sqlite3.OperationalError('database is locked')))
    assert worker.step()
    assert tuple(status(p, job['id'])) == ('queued', '')


def test_other_database_errors_still_fail_the_job(tmp_path, monkeypatch):
    p, worker, job = _worker_with_job(tmp_path)
    monkeypatch.setattr(p, 'run', lambda job: (_ for _ in ()).throw(sqlite3.OperationalError('no such table: x')))
    assert worker.step()
    assert tuple(status(p, job['id'])) == ('failed', '内部错误：OperationalError')


def test_a_long_read_transaction_is_not_timed(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(write_fencing, 'SLOW_WRITE_SECONDS', 0.05)
    db = connect(tmp_path / 'db.sqlite3')
    with caplog.at_level(logging.WARNING, logger=write_fencing.__name__):
        db.execute('BEGIN')  # a consistent read: no write lock under WAL
        db.execute('SELECT * FROM t').fetchall()
        time.sleep(0.08)
        db.rollback()
    assert not caplog.records


@pytest.mark.parametrize('kind,expected', [('page_summary', 'done'), ('brief', 'failed')])
def test_a_page_summary_with_an_isolated_unknown_request_finishes_like_a_round(tmp_path, kind, expected):
    """Its result records failed pages and unread material; other job kinds are still held as failed."""
    from contextlib import closing

    from project_watch.studio.app import create_studio_app
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project('Synthetic Unknown Co').id
    p.store.upsert_project(pid, {'legal_name': 'Synthetic Unknown Co'}, consent=True)
    job = p.store.enqueue(pid, kind, input_version='manual:unknown')
    with closing(p.store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at,usage_version) "
                   "VALUES (?,?,?,?,'unknown',?,1)", (job['id'], 'synthetic-cut', 'model', 'p', '2026-11-18T00:00:00+00:00'))
    p.store.finish(job['id'])
    with closing(p.store._connect()) as db:
        assert db.execute('SELECT status FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()[0] == expected


@pytest.mark.parametrize('kind,isolated,expected', [('details', True, 'done'), ('business', True, 'done'),
                                                     ('details', False, 'failed')])
def test_an_extraction_job_whose_unclear_batch_was_isolated_finishes_partial(tmp_path, kind, isolated, expected):
    """Its other batches stay saved and nothing is applied; enqueue() holds the same input either way."""
    from contextlib import closing

    from project_watch.studio.app import create_studio_app
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project('Synthetic Unknown Co').id
    p.store.upsert_project(pid, {'legal_name': 'Synthetic Unknown Co'}, consent=True)
    job = p.store.enqueue(pid, kind, input_version='synthetic-input')
    with closing(p.store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at,usage_version) "
                   "VALUES (?,?,?,?,'unknown',?,1)", (job['id'], 'synthetic-cut', 'model', 'p', '2026-11-18T00:00:00+00:00'))
        if isolated:
            db.execute("INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
                       (job['id'], 'independent-step-failures-v1', '{"detail_proposal_3": {"status": "blocked"}}'))
    p.store.finish(job['id'])
    with closing(p.store._connect()) as db:
        assert db.execute('SELECT status FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()[0] == expected
    if isolated:
        # No new paid job for the same input.
        assert p.store.enqueue(pid, kind, automatic=True, input_version='synthetic-input')['id'] == job['id']
