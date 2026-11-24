"""Run independent steps of one job a few at a time, without abandoning a sent request.

Each step keeps its own request journal row, so an unknown outcome is still held and
never resent. Steps run with the job's execution and write fence, as on the job thread.
After the first failure no new step starts; the running ones finish (their answers are
journaled and checkpointed) and the first failure is raised. A width of 1, or a job
without a configured width, runs the steps in order on the calling thread.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext

MAX_WIDTH = 4
WAVE_DEFAULT = 3  # the request senders' width before it became a setting


def width(execution, default=1) -> int:
    return max(1, min(MAX_WIDTH, int(getattr(execution, 'parallel_width', default) or default)))


def waves(execution, pending):
    """Split unsaved requests into waves of the job's width.

    The first request goes alone only while this job has an unresolved request: on
    resume it may be that one, and nothing else is paid for if it cannot complete.
    Without an unresolved row no pending request can be an unknown one.
    """
    size = width(execution, WAVE_DEFAULT)
    first = 1 if unresolved(execution) else size
    return [pending[:first]] + [pending[i:i + size] for i in range(first, len(pending), size)]


def unresolved(execution) -> bool:
    """Whether a request of this job could be held when sent: the rows dispatch.check holds on."""
    from contextlib import closing

    from project_watch.studio.dispatch import RECEIVED_FAILURE, REDO_PREFIX, batch_of, explicit_redo
    from project_watch.studio.store import now
    store, job = getattr(execution, 'store', None), getattr(execution, 'job', None)
    if store is None or job is None:
        return True  # unknown journal: keep the cautious order
    batch = None if explicit_redo(job) else batch_of(job)
    with closing(store._connect()) as db:
        held = db.execute(
            "SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id WHERE j.project_id=? "
            "AND (j.id=? OR (? IS NOT NULL AND j.kind='analyze' AND j.input_version IN ('collect:'||?, ?||?))) "
            "AND (c.status='started' OR (c.status='unknown' AND COALESCE(c.retryable,0)<>2) "
            f"OR (c.kind='model' AND c.status='completed' AND c.response_status IN {RECEIVED_FAILURE} AND c.retryable=0)) LIMIT 1",
            (job['project_id'], job['id'], batch, batch, REDO_PREFIX, batch)).fetchone()
        flying = db.execute(
            "SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id WHERE j.project_id=? "
            "AND c.status='started' AND j.status='running' AND j.lease_until>? LIMIT 1",
            (job['project_id'], now())).fetchone()
    return bool(held or flying)


def run(execution, tasks, *, limit=None, cautious=True):
    """Results of ``tasks`` (callables) in order; at most ``limit`` (default: the job's width) at once."""
    tasks = list(tasks)
    size = width(execution) if limit is None else max(1, min(MAX_WIDTH, limit))
    if size == 1 or len(tasks) <= 1:
        return [task() for task in tasks]
    if cautious and unresolved(execution):
        # As in waves(): on resume the first may be the held one; nothing else is paid for if it stops.
        first = tasks[0]()
        return [first, *run(execution, tasks[1:], limit=size, cautious=False)]
    scope = getattr(execution, 'thread_scope', None) or nullcontext

    def in_job(task):
        with scope():
            return task()

    results, failure, queue = [None] * len(tasks), None, iter(enumerate(tasks))
    with ThreadPoolExecutor(max_workers=size, thread_name_prefix='studio-step') as pool:
        running = {}

        def fill():
            for index, task in queue:
                running[pool.submit(in_job, task)] = index
                if len(running) >= size:
                    return
        fill()
        while running:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                index = running.pop(future)
                try:
                    results[index] = future.result()
                except BaseException as exc:  # raised once the running siblings finish
                    failure = failure or exc
            if failure is None:
                fill()
    if failure is not None:
        raise failure
    return results
