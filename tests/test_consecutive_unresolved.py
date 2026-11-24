"""A provider that stops answering is told apart from an isolated network drop."""
from contextlib import closing

import pytest
from test_continuation_upgrade import setup

from project_watch.studio.execution import JobExecution


@pytest.mark.parametrize('statuses,expected', [
    ([], 0),
    (['completed', 'unknown'], 1),
    (['unknown', 'completed', 'unknown'], 1),
    (['completed', 'unknown', 'not_sent', 'unknown'], 2),
    (['completed', 'unknown', 'resent', 'started'], 1),
])
def test_only_the_latest_unanswered_run_counts(tmp_path, statuses, expected):
    store, _pid, job = setup(tmp_path)
    with closing(store._connect()) as db, db:
        for n, status in enumerate(statuses):
            db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                       "VALUES (?,?,'model','p',?,?)", (job['id'], f'k{n}', status, f'2026-01-01T00:00:{n:02d}+00:00'))
        db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                   "VALUES (?,'search','search','s','unknown','2026-01-01T00:01:00+00:00')", (job['id'],))
    assert JobExecution(store, job).consecutive_unresolved() == expected
