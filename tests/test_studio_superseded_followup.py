"""A follow-up superseded by new inputs is rerun as a fresh round; unknown outcomes stay held.

Reproduces the first-use path: create a project, upload a BP while the first
follow-up runs, then press "rerun" on the task board. Synthetic data only.
"""
from contextlib import closing

from fastapi.testclient import TestClient
from test_studio_external_round import first, followup, setup

from project_watch.studio import taskboard
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.store import now

SUPERSEDED = '监控依据已变化，请重新更新项目；已有结果保留。'


def _superseded(pipeline, pid):
    job = followup(pipeline)
    JobExecution(pipeline.store, job).checkpoint('synthetic_saved', {'read': True})
    pipeline.store.finish(job['id'], error=SUPERSEDED, lease_token=job['lease_token'])
    failed = next(j for j in pipeline.store.jobs(pid) if j['id'] == job['id'])
    assert failed['status'] == 'failed' and failed['retry_after'] == '9999-12-31'
    return job


def _rerun(tmp_path, pid):
    with TestClient(create_studio_app(tmp_path, start_worker=False)) as client:
        return client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'external_followup'})


def test_rerun_of_superseded_followup_starts_a_fresh_round(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    job = _superseded(pipeline, pid)

    response = _rerun(tmp_path, pid)

    assert response.status_code == 202, response.text
    assert response.json()['kind'] == 'external' and response.json()['status'] == 'queued'
    old = next(j for j in pipeline.store.jobs(pid) if j['id'] == job['id'])
    assert old['status'] == 'failed'  # The stale follow-up is not resumed or republished.
    assert JobExecution(pipeline.store, job).read_checkpoint('synthetic_saved') == {'read': True}
    # The fresh round runs to completion and clears the hold, as an explicit update would.
    first(pipeline)
    rest = followup(pipeline)
    pipeline.run(rest)
    pipeline.store.finish(rest['id'], lease_token=rest['lease_token'])
    assert pid in pipeline.store.due_external_projects(0)


def test_superseded_followup_with_an_unknown_request_stays_held(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    job = _superseded(pipeline, pid)
    with closing(pipeline.store._connect()) as db, db:
        db.execute("INSERT INTO studio_job_calls (job_id,call_key,kind,provider,status,created_at) "
                   "VALUES (?,?,?,?,?,?)", (job['id'], 'synthetic-call', 'search', 'synthetic', 'started', now()))

    response = _rerun(tmp_path, pid)

    assert response.status_code == 409 and '请求结果尚未确认' in response.json()['detail']
    assert not [j for j in pipeline.store.jobs(pid) if j['status'] == 'queued']


def test_held_followup_for_other_reasons_is_still_refused(tmp_path, monkeypatch):
    pipeline, pid, _ = setup(tmp_path, monkeypatch)
    first(pipeline)
    job = followup(pipeline)
    pipeline.store.finish(job['id'], error='request_outcome_unknown: synthetic', lease_token=job['lease_token'])

    response = _rerun(tmp_path, pid)

    assert response.status_code == 409
    assert not [j for j in pipeline.store.jobs(pid) if j['status'] == 'queued']


def test_task_board_names_new_material_as_a_possible_cause():
    advice = taskboard.advice({'kind': 'external_followup', 'error': SUPERSEDED})
    assert advice['action'] == 'rerun' and '新增了资料' in advice['why']
