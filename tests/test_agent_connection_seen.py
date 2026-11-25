"""The setup page can tell that a client really connected, not only that a configuration exists."""
import json

from fastapi.testclient import TestClient

from project_watch.studio.agent_bridge import InstanceAgentClient
from project_watch.studio.app import create_studio_app


def web(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    return app, TestClient(app, base_url='http://127.0.0.1:19431')


def connections(client):
    return client.get('/api/studio/agent-connections').json()


def test_a_connection_is_seen_only_once_its_bridge_reaches_projectwatch(tmp_path):
    _app, client = web(tmp_path)
    connection = client.post('/api/studio/agent-connection', json={'client': 'codex'}).json()
    assert connections(client)[0]['last_seen_at'] is None  # a generated configuration is not a connection
    assert client.get('/api/studio/agent/runtime', headers={'X-ProjectWatch-Connection': 'f' * 32}).status_code == 200
    assert connections(client)[0]['last_seen_at'] is None  # an unknown connection records nothing
    client.get('/api/studio/agent/runtime', headers={'X-ProjectWatch-Connection': connection['connection_id']})
    assert connections(client)[0]['last_seen_at']


def test_the_bridge_names_its_connection_when_it_checks_the_runtime(tmp_path):
    _app, client = web(tmp_path)
    connection = client.post('/api/studio/agent-connection', json={'client': 'workbuddy'}).json()
    path = tmp_path / 'agent-connections' / f"{connection['connection_id']}.json"
    sent = []

    def transport(method, url, headers, body):
        sent.append(headers)
        response = client.get('/api/studio/agent/runtime', headers=headers)
        return response.status_code, response.content
    config = json.loads(path.read_text(encoding='utf-8'))
    config.pop('desktop_marker', None)
    path.write_text(json.dumps(config), encoding='utf-8')
    InstanceAgentClient(path, transport=transport).runtime()
    assert sent[0]['X-ProjectWatch-Connection'] == connection['connection_id']
    assert connections(client)[0]['last_seen_at']
