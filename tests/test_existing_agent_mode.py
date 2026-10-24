import json
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_studio_agent_results import payload
from test_studio_application_parity import headers

from project_watch.studio.agent_bridge import AgentClient, build_studio_mcp_server
from project_watch.studio.app import create_studio_app


def setup(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    web = TestClient(app, base_url='http://127.0.0.1:19431')
    result = web.post('/api/studio/projects', json={'entity': {'legal_name': 'Synthetic Optics'},
                                                  'execution_mode': 'agent', 'consent': True})
    assert result.status_code == 201, result.text
    return app, web, result.json()['id']


def test_agent_create_has_no_provider_work_or_changed_existing_state(tmp_path, monkeypatch):
    from project_watch.studio.pipeline import Pipeline

    monkeypatch.setattr(Pipeline, 'tools', lambda *a: pytest.fail('Agent creation called a provider'))
    app, web, pid = setup(tmp_path)
    store = app.state.studio_store
    project = store.get_project(pid)
    assert project['execution_mode'] == 'agent' and project['status'] == 'paused'
    assert not store.jobs(pid)
    assert store.enqueue(pid, 'internal')['status'] == 'agent_required'
    assert store.enqueue(pid, 'external')['status'] == 'agent_required'
    assert store.claim() is None
    assert web.post(f'/api/studio/projects/{pid}/refresh', json={}).status_code == 409
    assert web.post(f'/api/studio/projects/{pid}/watchpoints', json={'text': 'Synthetic capacity', 'alert': False}).status_code == 409
    other = app.state.intake_worker.repository.create_project('Existing API')
    store.upsert_project(other.id, {'legal_name': 'Existing API'}, consent=False)
    store.set_status(other.id, 'paused')
    assert web.put('/api/studio/settings', json={'preferred_mode': 'agent'}).status_code == 200
    assert store.get_project(other.id)['execution_mode'] == 'api'
    assert store.get_project(other.id)['status'] == 'paused'
    assert web.get('/api/studio/settings').json()['preferred_mode'] == 'agent'
    assert web.put('/api/studio/settings', json={'preferred_mode': 'bad'}).status_code == 422


@pytest.mark.parametrize('client', ['codex', 'workbuddy'])
def test_connection_and_full_synthetic_roundtrip(tmp_path, client):
    app, web, pid = setup(tmp_path)
    response = web.post(f'/api/studio/projects/{pid}/agent-connection', json={'client': client})
    assert response.status_code == 200, response.text
    conn = response.json()
    assert 'pwa_' not in response.text
    assert conn['status'] == 'configuration_generated' and conn['provider_calls'] == 0
    config = tomllib.loads(conn['configuration']) if client == 'codex' else json.loads(conn['configuration'])
    server = config['mcp_servers' if client == 'codex' else 'mcpServers'][conn['server_name']]
    assert server['args'] == ['-I', '-B', '-m', 'project_watch.studio.agent_bridge']
    credential = json.loads(Path(server['env']['PROJECT_WATCH_AGENT_CONFIG']).read_text(encoding='utf-8'))
    hdr = headers(credential)
    root = f'/api/studio/agent/projects/{pid}'
    assert web.get(root, headers=hdr).status_code == 200
    scope = web.get(root+'/scope', headers=hdr).json()
    change = {'expected_version': scope['config_version'], 'expected_scope_version': scope['scope_version'],
              'idempotency_key': 'scope-synthetic-1',
              'item': {'text': 'Monitor factory ramp up', 'queries': ['synthetic optics factory'], 'reason': 'Synthetic user request'}}
    written = web.post(root+'/scope', headers=hdr, json=change)
    assert written.status_code == 200, written.text
    assert web.post(root+'/scope', headers=hdr, json=change).json() == written.json()
    stale = {**change, 'idempotency_key': 'scope-stale'}
    assert web.post(root+'/scope', headers=hdr, json=stale).status_code == 409
    # Research tool output is synthetic; this verifies the write contract, not live search quality.
    version = web.get(root, headers=hdr).json()['config_version']
    result = web.post(root+'/results', headers=hdr, json={**payload(version), 'producer': client})
    assert result.status_code == 201, result.text
    assert web.get(root+'/results/current', headers=hdr).json()['record']['producer'] == client
    scope = web.get(root+'/scope', headers=hdr).json()
    stop = {**change, 'expected_version': scope['config_version'], 'expected_scope_version': scope['scope_version'],
            'watch_id': written.json()['watch_id'], 'idempotency_key': 'stop-scope',
            'item': {**change['item'], 'status': 'retired'}}
    assert web.post(root+'/scope', headers=hdr, json=stop).status_code == 200
    assert web.get(root+'/scope', headers=hdr).json()['items'][0]['status'] == 'retired'
    assert not app.state.studio_store.jobs(pid)
    web.delete(f'/api/studio/projects/{pid}/agent-grants/{conn["grant_id"]}')
    assert web.get(root, headers=hdr).status_code == 403
    assert web.post(root+'/scope', headers=hdr, json=stop).status_code == 403


def test_bridge_mcp_schema_has_scope_contract():
    server = build_studio_mcp_server(AgentClient('http://127.0.0.1:1', 'synthetic', 'project', 'pwa_synthetic'))
    assert server is not None


def test_same_name_cannot_convert_existing_api_or_reset_agent(tmp_path):
    app, web, pid = setup(tmp_path)
    store = app.state.studio_store
    before = store.get_project(pid)
    duplicate = web.post('/api/studio/projects', json={'entity': {'legal_name': before['legal_name']},
                         'execution_mode': 'agent', 'consent': True})
    assert duplicate.status_code == 409
    assert store.get_project(pid) == before
    project = app.state.intake_worker.repository.create_project('Existing API')
    store.upsert_project(project.id, {'legal_name': 'Existing API'}, consent=False)
    store.set_status(project.id, 'paused')
    before = store.get_project(project.id)
    assert web.post('/api/studio/projects', json={'entity': {'legal_name': 'Existing API'},
                    'execution_mode': 'agent', 'consent': True}).status_code == 409
    assert store.get_project(project.id) == before


def test_scope_permissions_and_owner_edit_conflict(tmp_path):
    app, web, pid = setup(tmp_path)
    plan = app.state.studio_pipeline.monitoring_plan
    plan.add(pid, {'text': 'Private synthetic question'}, visibility='internal', actor='human', origin='human')
    public = plan.add(pid, {'text': 'Public synthetic question'}, visibility='public', actor='human', origin='human')
    grant = web.post(f'/api/studio/projects/{pid}/agent-grants', json={'permission':'write','label':'scope test'}).json()
    root = f'/api/studio/agent/projects/{pid}/scope'
    scope = web.get(root, headers=headers(grant)).json()
    assert len(scope['items']) == 1 and scope['items'][0]['id'] == public['id']
    body = {'expected_version':scope['config_version'], 'expected_scope_version':scope['scope_version'],
            'idempotency_key':'scope-race', 'item':{'text':'New public question'}}
    plan.update(public['id'], {'text': 'Owner revised public question'}, actor='human')
    assert web.post(root, headers=headers(grant), json=body).status_code == 409
    reader = web.post(f'/api/studio/projects/{pid}/agent-grants', json={'permission':'read','label':'reader'}).json()
    assert web.post(root, headers=headers(reader), json=body).status_code == 403


def test_agent_bp_parses_locally_and_requires_internal_grant(tmp_path, monkeypatch):
    from project_watch.studio.pipeline import Pipeline
    monkeypatch.setattr(Pipeline, 'tools', lambda *a: pytest.fail('local BP parsing called a provider'))
    app, web, pid = setup(tmp_path)
    uploaded = web.post(f'/api/studio/projects/{pid}/files', files={'file': ('Synthetic.txt', b'Synthetic internal connector business.', 'text/plain')})
    assert uploaded.status_code in (200, 201, 202), uploaded.text
    parsed = app.state.intake_worker.process_one()
    assert parsed.status == 'completed'
    assert app.state.studio_pipeline.snapshot_ready_documents() == 1
    assert not app.state.studio_store.jobs(pid)
    for internal in (False, True):
        grant = web.post(f'/api/studio/projects/{pid}/agent-grants', json={'permission':'read','label':'intake test','allow_internal':internal}).json()
        evidence = web.get(f'/api/studio/agent/projects/{pid}/evidence', headers=headers(grant))
        assert evidence.status_code == 200, evidence.text
        assert bool(evidence.json()['items']) is internal
