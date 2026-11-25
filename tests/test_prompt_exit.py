"""Exit hands a running job back before its next request instead of waiting for its last batch."""
from contextlib import closing

import pytest
from test_write_transaction_timing import _worker_with_job, status

from project_watch.studio.execution import JobExecution, JobYield
from project_watch.studio.http import Response


def test_no_request_is_sent_once_exit_is_requested(tmp_path):
    p, _worker, job = _worker_with_job(tmp_path)
    execution = JobExecution(p.store, p.store.claim(), stop_requested=lambda: True)
    with pytest.raises(JobYield):
        execution.call('model', 'next-batch', lambda: pytest.fail('sent after exit was requested'))
    with closing(p.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_job_calls WHERE job_id=?', (job['id'],)).fetchone()[0] == 0


def test_exit_mid_job_queues_it_and_resume_does_not_resend(tmp_path, monkeypatch):
    """Live M2: exit waited over half an hour for a 2.5 h extraction to reach its last batch."""
    p, worker, job = _worker_with_job(tmp_path)
    sent = []

    def batches(job):
        execution = p._execution_state.current
        for n in range(3):
            def send(n=n):
                sent.append(n)
                if n == 0:
                    worker._stop.set()  # the owner clicks exit while the first answer is arriving
                return Response(200, {}, b'{"ok": true}')
            execution.call('model', f'batch-{n}', send)

    original = p.run

    def run(job):
        from project_watch.studio.execution import JobExecution as Execution
        p._execution_state.current = Execution(p.store, job, stop_requested=p._execution_state.stop_requested)
        try:
            batches(job)
        finally:
            p._execution_state.current = None
    monkeypatch.setattr(p, 'run', run)
    assert worker.step()
    assert tuple(status(p, job['id'])) == ('queued', '')  # handed back, not failed
    assert sent == [0]
    worker._stop.clear()
    assert worker.step()
    assert sent == [0, 1, 2]  # the answered batch was replayed from the journal, not sent again
    assert status(p, job['id'])['status'] == 'done'
    monkeypatch.setattr(p, 'run', original)


def test_a_handback_passes_through_handlers_that_record_any_error_as_failure():
    assert issubclass(JobYield, BaseException) and not issubclass(JobYield, Exception)
    try:
        try:
            raise JobYield()
        except Exception:  # e.g. a step that records any error as its own failure
            pytest.fail('a hand-back was caught as an error')
    except JobYield:
        pass
