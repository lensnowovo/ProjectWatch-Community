"""ProjectWatch starts the local Codex for an authorized Agent project, on demand and on schedule."""
import json
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_watch.studio import agent_runs
from project_watch.studio.app import create_studio_app

FAKE = str(Path(__file__).with_name('fake_codex.py'))


@pytest.fixture
def codex(monkeypatch, tmp_path):
    record = tmp_path / 'codex-call.json'
    monkeypatch.setenv('PROJECT_WATCH_CODEX_COMMAND', json.dumps([sys.executable, '-I', FAKE]))
    monkeypatch.setenv('FAKE_CODEX_RECORD', str(record))
    monkeypatch.setenv('FAKE_CODEX_MODE', 'ok')
    agent_runs._STATUS_CACHE.clear()
    return record


def setup(tmp_path, *, authorize=True):
    home = tmp_path / 'home'
    app = create_studio_app(home, start_worker=False)
    web = TestClient(app, base_url='http://127.0.0.1:19431')
    connection = web.post('/api/studio/agent-connection', json={'client': 'codex'}).json()['connection_id']
    project = web.post('/api/studio/projects', json={'entity': {'legal_name': 'Synthetic Cells'}, 'consent': True,
                                                     'execution_mode': 'agent'}).json()['id']
    if authorize:
        assert web.post(f'/api/studio/projects/{project}/agent-connection',
                        json={'client': 'codex', 'connection_id': connection}).status_code == 200
    return app, web, connection, project, app.state.agent_runner


def enable(web, project, connection, **extra):
    return web.put(f'/api/studio/projects/{project}/agent-auto',
                   json={'enabled': True, 'connection_id': connection, **extra})


def test_enabling_starts_one_run_that_loads_only_this_projectwatch(tmp_path, codex):
    _app, web, connection, project, runner = setup(tmp_path)
    view = enable(web, project, connection, run_now=True).json()
    assert view['enabled'] and view['active']['status'] == 'queued'
    assert web.post(f'/api/studio/projects/{project}/agent-runs').status_code == 409  # one run at a time
    runner.step()
    last = web.get(f'/api/studio/projects/{project}/agent-auto').json()['last']
    assert last['status'] == 'done' and last['summary'] == '写入 2 条新闻。'
    assert (last['input_tokens'], last['output_tokens']) == (1200, 34)
    argv = json.loads(codex.read_text(encoding='utf-8'))['argv']
    assert '--ignore-user-config' in argv and argv[argv.index('--sandbox') + 1] == 'read-only'
    name = 'projectwatch_' + connection[:12]
    assert f'mcp_servers.{name}.default_tools_approval_mode="approve"' in argv
    assert f'{connection}.json' in next(a for a in argv if a.startswith(f'mcp_servers.{name}.env='))
    assert f'project_id={project}' in argv[-1]


def test_auto_research_needs_an_agent_project_authorized_to_codex(tmp_path, codex):
    _app, web, connection, project, _runner = setup(tmp_path, authorize=False)
    assert enable(web, project, connection).status_code == 409
    api = web.post('/api/studio/projects', json={'entity': {'legal_name': 'API Co'}, 'consent': True}).json()['id']
    assert enable(web, api, connection).status_code == 409


def test_a_failed_run_is_recorded_and_not_resent(tmp_path, codex, monkeypatch):
    _app, web, connection, project, runner = setup(tmp_path)
    monkeypatch.setenv('FAKE_CODEX_MODE', 'fail')
    enable(web, project, connection, run_now=True)
    runner.step()
    view = web.get(f'/api/studio/projects/{project}/agent-auto').json()
    assert view['last']['status'] == 'failed' and 'not logged in' in view['last']['error']
    assert runner.step() is None  # the next round waits for the schedule
    assert view['next_at'] > view['last']['created_at']


def test_a_run_past_the_time_limit_is_stopped_and_unknown(tmp_path, codex, monkeypatch):
    _app, web, connection, project, runner = setup(tmp_path)
    monkeypatch.setenv('FAKE_CODEX_MODE', 'hang')
    monkeypatch.setattr(agent_runs, 'TIMEOUT_SECONDS', 2)
    enable(web, project, connection, run_now=True)
    started = time.monotonic()
    runner.step()
    assert time.monotonic() - started < 30
    last = web.get(f'/api/studio/projects/{project}/agent-auto').json()['last']
    assert last['status'] == 'unknown' and '已停止' in last['error']


def test_a_revoked_grant_stops_auto_research(tmp_path, codex):
    _app, web, connection, project, runner = setup(tmp_path)
    enable(web, project, connection, run_now=True)
    grant = web.get(f'/api/studio/projects/{project}/agent-grants').json()[0]['id']
    assert web.delete(f'/api/studio/projects/{project}/agent-grants/{grant}').status_code in (200, 204)
    runner.step()
    view = web.get(f'/api/studio/projects/{project}/agent-auto').json()
    assert view['last']['status'] == 'failed' and not view['enabled']
    assert not codex.exists()


def test_a_grant_close_to_expiry_is_renewed_while_auto_research_is_on(tmp_path, codex):
    app, web, connection, project, runner = setup(tmp_path)
    path = tmp_path / 'home' / 'agent-connections' / f'{connection}.json'
    old = json.loads(path.read_text(encoding='utf-8'))['projects'][project]['grant_id']
    with closing(app.state.studio_store._connect()) as db, db:
        db.execute('UPDATE studio_agent_grants SET expires_at=? WHERE id=?', (int(time.time()) + 3600, old))
    enable(web, project, connection, run_now=True)
    runner.step()
    new = json.loads(path.read_text(encoding='utf-8'))['projects'][project]['grant_id']
    grants = {g['id']: g for g in web.get(f'/api/studio/projects/{project}/agent-grants').json()}
    assert new != old and grants[old]['revoked_at'] and not grants[new]['revoked_at']
    assert web.get(f'/api/studio/projects/{project}/agent-auto').json()['last']['status'] == 'done'


def test_the_next_round_is_scheduled_after_the_cadence(tmp_path, codex):
    app, web, connection, project, runner = setup(tmp_path)
    enable(web, project, connection, cadence_days=7, run_now=True)
    runner.step()
    assert runner.step() is None
    with closing(app.state.studio_store._connect()) as db, db:
        db.execute("UPDATE studio_agent_runs SET created_at='2020-01-01T00:00:00+00:00'")
    runner._save_settings(project, {**runner.settings(project), 'enabled_at': '2020-01-01T00:00:00+00:00'})
    run = runner.step()
    assert run and run['trigger'] == 'schedule'
    web.put(f'/api/studio/projects/{project}/agent-auto', json={'enabled': False})
    with closing(app.state.studio_store._connect()) as db, db:
        db.execute("UPDATE studio_agent_runs SET created_at='2020-01-01T00:00:00+00:00'")
    assert runner.step() is None  # switched off: nothing scheduled


def test_a_run_left_running_by_an_exit_is_recorded_as_unknown(tmp_path, codex):
    app, web, connection, project, runner = setup(tmp_path)
    enable(web, project, connection, run_now=True)
    with closing(app.state.studio_store._connect()) as db, db:
        db.execute("UPDATE studio_agent_runs SET status='running'")
    runner.recover()
    last = web.get(f'/api/studio/projects/{project}/agent-auto').json()['last']
    assert last['status'] == 'unknown'


def test_codex_status_reports_availability_and_login(tmp_path, codex, monkeypatch):
    _app, web, *_ = setup(tmp_path)
    status = web.get('/api/studio/agent/codex').json()
    assert status['available'] and status['logged_in'] and status['version'] == 'codex-cli 0.0.0-test'
    monkeypatch.setenv('FAKE_CODEX_LOGGED_IN', '0')
    assert not web.get('/api/studio/agent/codex?refresh=true').json()['logged_in']


def test_codex_gets_no_local_tools_and_no_projectwatch_environment(tmp_path, codex, monkeypatch):
    _app, web, connection, project, runner = setup(tmp_path)
    monkeypatch.setenv('PROJECT_WATCH_MODEL_API_KEY', 'sk-test-not-for-codex')
    enable(web, project, connection, run_now=True)
    runner.step()
    call = json.loads(codex.read_text(encoding='utf-8'))
    disabled = {call['argv'][i + 1] for i, a in enumerate(call['argv']) if a == '--disable'}
    assert set(agent_runs.LOCAL_TOOLS) <= disabled
    assert call['project_watch_env'] == []


def test_switching_on_schedules_from_now_instead_of_running_at_once(tmp_path, codex):
    _app, web, connection, project, runner = setup(tmp_path)
    view = enable(web, project, connection).json()
    assert view['active'] is None and view['next_at'] > view['enabled_at']
    assert runner.step() is None and not codex.exists()


def test_a_removed_project_gets_no_more_runs(tmp_path, codex):
    app, web, connection, project, runner = setup(tmp_path)
    enable(web, project, connection, run_now=True)
    runner.step()
    assert web.delete(f'/api/studio/projects/{project}').status_code in (200, 204)
    with closing(app.state.studio_store._connect()) as db, db:
        db.execute("UPDATE studio_agent_runs SET created_at='2020-01-01T00:00:00+00:00'")
    codex.unlink()
    assert runner.step() is None and not codex.exists()
    assert web.post(f'/api/studio/projects/{project}/agent-runs').status_code == 409


def test_a_stop_before_codex_starts_cancels_the_run(tmp_path, codex):
    _app, web, connection, project, runner = setup(tmp_path)
    run_id = enable(web, project, connection, run_now=True).json()['active']['id']
    claimed = runner._claim()
    web.post(f'/api/studio/projects/{project}/agent-runs/{run_id}/cancel')
    runner.execute(claimed)
    assert web.get(f'/api/studio/projects/{project}/agent-auto').json()['last']['status'] == 'cancelled'
    assert not codex.exists()


def test_a_manual_run_with_auto_research_off_does_not_renew_the_grant(tmp_path, codex):
    app, web, connection, project, runner = setup(tmp_path)
    path = tmp_path / 'home' / 'agent-connections' / f'{connection}.json'
    old = json.loads(path.read_text(encoding='utf-8'))['projects'][project]['grant_id']
    enable(web, project, connection)
    web.put(f'/api/studio/projects/{project}/agent-auto', json={'enabled': False})
    with closing(app.state.studio_store._connect()) as db, db:
        db.execute('UPDATE studio_agent_grants SET expires_at=? WHERE id=?', (int(time.time()) + 3600, old))
    web.post(f'/api/studio/projects/{project}/agent-runs')
    runner.step()
    assert json.loads(path.read_text(encoding='utf-8'))['projects'][project]['grant_id'] == old


def test_a_run_that_writes_nothing_back_is_not_reported_complete(tmp_path, codex, monkeypatch):
    _app, web, connection, project, runner = setup(tmp_path)
    monkeypatch.setenv('FAKE_CODEX_MODE', 'nowrite')
    enable(web, project, connection, run_now=True)
    runner.step()
    last = web.get(f'/api/studio/projects/{project}/agent-auto').json()['last']
    assert last['status'] == 'failed' and '没有写回' in last['error'] and last['summary']
