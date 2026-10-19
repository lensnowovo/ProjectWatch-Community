"""Real upload/parser/API wiring with an explicitly fake model, no network."""
import json

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.monitor_strategy import validate_strategy
from tests.test_reading_worker import FakeModel


def test_fresh_upload_requires_consent_then_builds_monitoring(tmp_path, monkeypatch):
    monkeypatch.setenv('PROJECT_WATCH_SETTINGS_TOKEN', 'synthetic-token')
    model = FakeModel()

    def plan(packet, *, before_send):
        before_send(json.dumps({'model': 'fake', 'stream': True, 'max_tokens': 8192, 'messages': []}).encode())
        model.calls += 1
        return validate_strategy(packet, {
            'understanding': {'text': '合成企业生产工业检测设备。', 'evidence': ['one']},
            'scopes': [{'category': 'company', 'anchor': '工业检测设备',
                        'question': '这一产品有哪些进展？', 'rationale': '所选片段描述的产品方向。', 'evidence': ['one']}],
            'unknowns': [],
        })

    model.plan_monitoring = plan
    app = create_app(tmp_path / 'handoff_test.sqlite3', proposal_model=model)
    client = TestClient(app, base_url='http://127.0.0.1')
    project = client.post('/api/projects', json={'name': 'Synthetic handoff'}).json()
    root = f'/api/projects/{project["id"]}'
    uploaded = client.post(root + '/files', files={'file': ('bp.txt', '公司生产工业检测设备。'.encode(), 'text/plain')})
    assert uploaded.status_code == 202
    source = uploaded.json()['source']
    assert app.state.intake_worker.process_one().status == 'completed'
    fragments = client.get(root + f'/sources/{source["id"]}/fragments').json()
    assert model.calls == 0
    packet = dict(project_id=project['id'], source_id=source['id'], source_sha256=source['content_sha256'],
                  reference=source['reference'], excerpts=[dict(id='one', locator=fragments[0]['locator'], text=fragments[0]['text'])])
    route = f'/api/workspace/projects/{project["id"]}/monitor-autopilot'
    headers = {'Origin': 'http://127.0.0.1', 'X-Settings-Token': 'synthetic-token'}
    preview = client.post(route + '/preview', json={'packet': packet}, headers=headers)
    assert preview.status_code == 200 and model.calls == 0
    body = dict(packet=packet, model_consent=True, public_consent=True, disclosure_digest=preview.json()['disclosure_digest'])
    started = client.post(route, json=body, headers=headers)
    assert started.status_code == 202
    app.state.monitor_autopilot.process_one()
    assert model.calls == 1
    scopes = client.get(root + '/monitoring/external').json()['monitoring_strategy']['scopes']
    assert len(scopes) == 1 and scopes[0]['category'] == 'sector'
    assert len(client.get(root + '/monitoring/jobs').json()) == 1
    # Identical uploads/requests never reset the charged request or repeat work.
    again = client.post(root + '/files', files={'file': ('bp.txt', '公司生产工业检测设备。'.encode(), 'text/plain')}).json()
    assert again['source']['id'] == source['id']
    assert client.post(route, json=body, headers=headers).json()['id'] == started.json()['id']
    app.state.monitor_autopilot.process_one()
    assert model.calls == 1
