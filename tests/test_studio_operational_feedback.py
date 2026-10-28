"""Synthetic read-only status contracts; no network/model requests."""
from contextlib import closing

import pytest
from test_studio_agent_instance import make_service

from project_watch.studio.runtime_health import project_jobs, stage_phase


@pytest.mark.parametrize(('text', 'phase'), [
    ('检索公司新闻', 'collect'), ('阅读 4 篇报道正文', 'read'),
    ('生成当前判断', 'judge'), ('更新跟踪问题', 'judge'),
    ('修订监控方案', 'revise'), ('生成首版监控方案', 'revise'),
    ('等待确认请求结果', None), ('未知阶段', None), ('', None),
])
def test_stable_phase_vocabulary(text, phase):
    assert stage_phase(text) == phase


def test_project_status_retains_failure_past_recent_successes_without_writes(tmp_path):
    service, pid = make_service(tmp_path / 'synthetic.sqlite3')
    store = service.store
    failed = store.enqueue(pid, 'external_followup')
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='failed',finished_at='2026-10-18',error='synthetic' WHERE id=?", (failed['id'],))
    for n in range(12):
        done = store.enqueue(pid, 'brief')
        with closing(store._connect()) as db, db:
            db.execute("UPDATE studio_jobs SET status='done',finished_at=? WHERE id=?", (f'2026-10-19T00:{n:02d}:00', done['id']))
    active = store.enqueue(pid, 'insight')
    with closing(store._connect()) as db:
        before = [tuple(r) for r in db.execute('SELECT * FROM studio_jobs ORDER BY id')]
    result = project_jobs(store, pid, worker='stopped')
    assert failed['id'] in {j['id'] for j in result}
    assert active['id'] in {j['id'] for j in result}
    assert len(result) == 3
    assert all('lease_token' not in j and 'lease_until' not in j for j in result)
    assert all(j['runtime']['state'] != 'running' for j in result)
    with closing(store._connect()) as db:
        assert before == [tuple(r) for r in db.execute('SELECT * FROM studio_jobs ORDER BY id')]


def test_project_and_taskboard_share_runtime_and_phase(tmp_path):
    from project_watch.studio.taskboard import board
    service, pid = make_service(tmp_path / 'synthetic.sqlite3')
    store = service.store
    store.enqueue(pid, 'external')
    job = store.claim()
    store.set_stage(job['id'], '修订监控方案')
    projected = project_jobs(store, pid)[0]
    task = board(store, worker_state='running', model_ready=False, project_id=pid)['jobs'][0]
    assert projected['runtime']['phase'] == 'revise'
    assert projected['runtime']['state'] == task['runtime']['state'] == 'running'
    assert projected['runtime']['phase'] == task['runtime']['phase']


def test_active_list_projection_is_not_hidden_by_newer_finished_work(tmp_path):
    from project_watch.studio.runtime_health import active_projections
    service, pid = make_service(tmp_path / 'synthetic.sqlite3')
    store = service.store
    store.enqueue(pid, 'external')
    running = store.claim()
    store.set_stage(running['id'], '更新跟踪问题')
    done = store.enqueue(pid, 'brief')
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE id=?", (done['id'],))
    store.enqueue(pid, 'insight')
    projected = active_projections(store)[pid]
    assert projected['status'] == 'running'
    assert projected['runtime']['phase'] == 'judge'
    assert active_projections(store, worker='stopped')[pid]['runtime']['state'] == 'recovering'


def test_document_absorption_does_not_animate_a_recovering_job():
    from project_watch.studio.taskboard import _document_steps
    doc = {'intake_status': 'completed', 'fragments': 2, 'intake_updated_at': '2026-10-19', 'permission': None}
    job = {'status': 'running', 'stage': '整理公司档案', 'runtime': {'state': 'recovering', 'label': '等待恢复'}}
    steps = _document_steps(doc, job, True, '')
    assert steps[2]['state'] == 'blocked'
    assert steps[2]['note'] == '等待恢复'


def test_http_project_and_task_views_agree_without_dispatch(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.repository import ProjectRepository
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    store = app.state.studio_store
    repo = ProjectRepository(store.db_path)
    pid = repo.create_project('Synthetic Status').id
    store.upsert_project(pid, {'legal_name': 'Synthetic Status'}, consent=False)
    store.enqueue(pid, 'external')
    before = store.jobs(pid)
    with TestClient(app) as client:
        detail = client.get(f'/api/studio/projects/{pid}').json()
        task = client.get(f'/api/studio/tasks?project_id={pid}').json()
        projects = client.get('/api/studio/projects').json()
        assert detail['jobs'][0]['runtime']['state'] == task['jobs'][0]['runtime']['state'] == 'blocked'
        assert projects[0]['job_runtime']['state'] == 'blocked'
        assert 'lease_token' not in detail['jobs'][0]
    assert store.jobs(pid) == before
