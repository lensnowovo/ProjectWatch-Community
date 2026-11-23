"""A project's other jobs are not starved by public rounds that run back to back."""
from contextlib import closing
from datetime import UTC, datetime, timedelta

from project_watch.studio.app import create_studio_app


def ago(hours):
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def project(tmp_path):
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project('Synthetic Busy Co').id
    p.store.upsert_project(pid, {'legal_name': 'Synthetic Busy Co'}, consent=True)
    return p.store, pid


def set_job(store, job, **values):
    with closing(store._connect()) as db, db:
        for key, value in values.items():
            db.execute(f'UPDATE studio_jobs SET {key}=? WHERE id=?', (value, job['id']))


def side_job(store, pid, created, **values):
    job = store.enqueue(pid, 'details', input_version='synthetic-details')
    set_job(store, job, created_at=created, **values)
    return job


def round_ran(store, pid, collected, finished):
    """Collection stamps the project when it ends; the round ends later with its brief."""
    with closing(store._connect()) as db, db:
        db.execute('UPDATE studio_projects SET last_external_at=? WHERE project_id=?', (collected, pid))
    brief = store.enqueue(pid, 'brief', input_version='synthetic-brief')
    set_job(store, brief, status='done', started_at=collected, finished_at=finished)


def test_a_job_that_waited_through_a_round_starts_before_the_next_round(tmp_path):
    store, pid = project(tmp_path)
    details = side_job(store, pid, ago(9))
    round_ran(store, pid, collected=ago(6), finished=ago(1))
    assert pid not in store.due_external_projects(0)
    assert store.claim('side')['id'] == details['id']
    assert pid in store.due_external_projects(0)  # once it has started, the round is due again


def test_a_job_queued_while_the_round_was_still_analysing_also_goes_first(tmp_path):
    store, pid = project(tmp_path)
    side_job(store, pid, ago(4))  # after collection ended, before the brief
    round_ran(store, pid, collected=ago(6), finished=ago(1))
    assert pid not in store.due_external_projects(0)


def test_a_job_queued_after_the_round_finished_does_not_hold_the_next(tmp_path):
    store, pid = project(tmp_path)
    round_ran(store, pid, collected=ago(6), finished=ago(2))
    side_job(store, pid, ago(1))
    assert pid in store.due_external_projects(0)


def test_public_monitoring_is_held_at_most_one_side_turn(tmp_path):
    store, pid = project(tmp_path)
    side_job(store, pid, ago(20))
    round_ran(store, pid, collected=ago(18), finished=ago(store.SIDE_TURN.total_seconds() / 3600 + 1))
    assert pid in store.due_external_projects(0)


def test_a_job_waiting_out_a_provider_backoff_does_not_hold_the_round(tmp_path):
    store, pid = project(tmp_path)
    side_job(store, pid, ago(9), retry_after='2999-01-01T00:00:00+00:00')
    round_ran(store, pid, collected=ago(6), finished=ago(1))
    assert pid in store.due_external_projects(0)


def test_a_project_never_collected_is_due_regardless(tmp_path):
    store, pid = project(tmp_path)
    side_job(store, pid, ago(9))
    assert pid in store.due_external_projects(0)


def test_the_scheduler_holds_the_round_too(tmp_path):
    from project_watch.studio import signals
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = p.repository.create_project('Synthetic Busy Co').id
    p.store.upsert_project(pid, {'legal_name': 'Synthetic Busy Co'}, consent=True)
    side_job(p.store, pid, ago(30))
    round_ran(p.store, pid, collected=ago(28), finished=ago(1))
    assert pid not in signals.due_external(p.store, p.monitor, 6)
