"""Synthetic status presentation only; no paid requests."""
import json
from contextlib import closing

from test_studio_agent_instance import make_service

from project_watch.studio.taskboard import board


def test_partial_task_has_reason_and_no_generic_paid_retry(tmp_path):
    service, pid = make_service(tmp_path / 'synthetic.sqlite3')
    store = service.store
    job = store.enqueue(pid, 'commercial')
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done',error='',finished_at=created_at WHERE id=?", (job['id'],))
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                   (job['id'], 'commercial_coverage', json.dumps({'partial': True})))
        db.execute('INSERT OR REPLACE INTO studio_state VALUES (?,?)', ('task_dismissed:' + job['id'], 'yes'))
    result = board(store, worker_state='running', model_ready=True)
    row = result['jobs'][0]
    assert row['runtime']['state'] == 'partial'
    assert row['advice']['why'].strip()
    assert row['advice']['todo'].strip()
    assert row['advice']['action'] == 'none'
    assert row['dismissed'] is True
    assert result['summary']['partial_24h'] == 1
    assert result['summary']['failed_24h'] == result['summary']['done_24h'] == 0
    with closing(store._connect()) as db:
        assert db.execute('SELECT status,error FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()[:] == ('done', '')


def test_unknown_partial_has_cost_warning_not_automatic_retry():
    from project_watch.studio.taskboard import advice
    value = advice({'kind': 'commercial', 'status': 'done', 'unknown': 1,
                    'runtime': {'state': 'partial', 'label': '已有结果保留，部分请求结果不明'}})
    assert '结果不明' in value['why']
    assert value['action'] == 'none'
    assert '不会自动重发' in value['todo']


def test_partial_dismiss_only_saves_reminder_not_outcome_or_queue(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    store = app.state.studio_store
    project = client.post('/api/studio/projects', json={
        'entity': {'legal_name': '合成状态验收'}, 'consent': True}).json()
    with closing(store._connect()) as db, db:
        db.execute('DELETE FROM studio_jobs')
    job = store.enqueue(project['id'], 'commercial')
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done',finished_at=created_at WHERE id=?", (job['id'],))
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                   (job['id'], 'commercial_coverage', json.dumps({'partial': True})))
        before = [tuple(r) for r in db.execute('SELECT * FROM studio_jobs ORDER BY id')]
    assert client.post(f"/api/studio/tasks/{job['id']}/dismiss").status_code == 204
    [row] = client.get('/api/studio/tasks').json()['jobs']
    assert row['dismissed'] and row['runtime']['state'] == 'partial'
    with closing(store._connect()) as db:
        assert before == [tuple(r) for r in db.execute('SELECT * FROM studio_jobs ORDER BY id')]
    # Active and genuinely successful tasks cannot be dismissed as incomplete.
    active = store.enqueue(project['id'], 'brief')
    assert client.post(f"/api/studio/tasks/{active['id']}/dismiss").status_code == 404
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done',finished_at=created_at WHERE id=?", (active['id'],))
    assert client.post(f"/api/studio/tasks/{active['id']}/dismiss").status_code == 404
