"""Local disclosure -> explicit consent -> persisted reading, with no real egress."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_external_analysis_jobs import sample

from project_watch.api import create_app

HEADERS = {'Origin': 'http://127.0.0.1', 'X-Settings-Token': 'synthetic-operator'}


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('PROJECT_WATCH_SETTINGS_TOKEN', 'synthetic-operator')
    s = sample(tmp_path)
    repo, project, _, _, model, _, _, _, plans, selections = s
    app = create_app(repo.db_path, proposal_model=model)
    client = TestClient(app, base_url='http://127.0.0.1')
    route = f'/api/workspace/projects/{project.id}/external-analysis'
    draft = {'plan_ids': plans, 'selections': selections, 'include_future_news': True, 'max_requests': 2}
    return s, app, client, route, draft


def preview(client, route, draft):
    response = client.post(route + '/preview', json=draft, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def consent(draft, disclosure):
    return {**draft, 'consent': True, 'request_key': 'synthetic-external-api',
            'disclosure_digest': disclosure['disclosure_digest']}


def test_preview_consent_background_result_and_restart(tmp_path, monkeypatch):
    s, app, client, route, draft = setup(tmp_path, monkeypatch)
    repo, project, source, _, model, *_ = s
    before = client.get(route).json()
    assert before['authorizations'] == [] and before['items'] == []
    assert before['automatic_analysis_available'] is False
    disclosure = preview(client, route, draft)
    assert disclosure['scope']['material_refs'][0]['source_id'] == source.id
    assert disclosure['recipient']['model'] == 'fake'
    assert disclosure['limits']['max_output_tokens'] == 3072
    assert model.calls == 0  # Disclosure is local, not a preview model run.
    body = consent(draft, disclosure)
    saved = client.post(route + '/authorization', json=body, headers=HEADERS)
    assert saved.status_code == 200, saved.text
    grant_id = saved.json()['id']
    assert client.post(route + '/authorization', json=body, headers=HEADERS).json()['id'] == grant_id
    assert app.state.external_analysis_worker.process_one()['state'] == 'succeeded'
    read = client.get(route).json()
    assert len(read['items']) == 1 and read['items'][0]['analysis']['review_status'] == 'unreviewed'
    reboot = TestClient(create_app(repo.db_path, proposal_model=model), base_url='http://127.0.0.1')
    assert reboot.get(route).json()['items'] == read['items']
    assert reboot.app.state.external_analysis_worker.process_one() is None and model.calls == 1
    assert repo.list_observations(project.id) == []


@pytest.mark.parametrize('path', ['/preview', '/authorization'])
def test_writes_require_local_origin_and_operator_token(tmp_path, monkeypatch, path):
    s, _, client, route, draft = setup(tmp_path, monkeypatch)
    body = draft if path == '/preview' else consent(draft, preview(client, route, draft))
    assert client.post(route+path, json=body).status_code == 403
    assert client.post(route+path, json=body, headers={**HEADERS, 'Origin': 'https://evil.test'}).status_code == 403
    assert client.post(route+path, json=body, headers={**HEADERS, 'X-Settings-Token': 'wrong'}).status_code == 403
    assert s[4].calls == 0


@pytest.mark.parametrize('changed', ['scope', 'budget', 'source', 'receiver', 'consent'])
def test_changed_disclosure_cannot_authorize(tmp_path, monkeypatch, changed):
    s, app, client, route, draft = setup(tmp_path, monkeypatch)
    disclosure = preview(client, route, draft)
    body = consent(draft, disclosure)
    if changed == 'scope':
        body['selections'] = []
    elif changed == 'budget':
        body['max_requests'] = 3
    elif changed == 'source':
        # API owns a repository instance; mutate its source view only for this test.
        app.state.external_analysis_store.repo.get_source = lambda _: replace(s[2], content_sha256='e'*64)
    elif changed == 'receiver':
        s[4].authorization_id = lambda: 'c'*64
    else:
        body['consent'] = False
    response = client.post(route+'/authorization', json=body, headers=HEADERS)
    assert response.status_code == (422 if changed == 'consent' else 409), response.text
    assert client.get(route).json()['authorizations'] == [] and s[4].calls == 0


def test_pause_revoke_scope_owner_and_missing_model(tmp_path, monkeypatch):
    s, app, client, route, draft = setup(tmp_path, monkeypatch)
    body = consent(draft, preview(client, route, draft))
    grant = client.post(route+'/authorization', json=body, headers=HEADERS).json()
    url = route+'/authorization/'+grant['id']
    assert client.patch(url, json={'status': 'paused'}).status_code == 403
    assert client.patch(url, json={'status': 'paused'}, headers=HEADERS).status_code == 200
    assert client.get(route).json()['processing']['reason'] == 'paused'
    assert app.state.external_analysis_worker.process_one() is None
    other = s[0].create_project('Other synthetic')
    assert client.patch(url.replace(s[1].id, other.id), json={'status': 'active'}, headers=HEADERS).status_code == 404
    assert client.patch(url, json={'status': 'revoked'}, headers=HEADERS).status_code == 200
    assert client.patch(url, json={'status': 'active'}, headers=HEADERS).status_code == 409
    app.state.external_analysis_worker.settings.model = None
    assert client.get(route).json()['processing']['reason'] == 'model_missing'
    assert client.post(route+'/preview', json=draft, headers=HEADERS).status_code == 503


def test_team_and_nonlocal_access_disabled(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from project_watch.external_analysis_api import attach_external_analysis

    s, _, client, route, draft = setup(tmp_path, monkeypatch)
    # The application host middleware rejects this before route execution.
    assert client.get(route, headers={'Host': 'outside.test'}).status_code == 400
    app = FastAPI()
    attach_external_analysis(app, s[0], s[5], store=None, worker=None, team_mode=True)
    team = TestClient(app, base_url='http://127.0.0.1')
    assert team.get(route).status_code == 404
    assert team.post(route+'/preview', json=draft, headers=HEADERS).status_code == 404


def test_intelligence_read_survives_missing_model_without_dispatch_or_writes(tmp_path, monkeypatch):
    from contextlib import closing

    s, app, client, route, draft = setup(tmp_path, monkeypatch)
    repo, project, _, _, model, *_ = s
    body = consent(draft, preview(client, route, draft))
    assert client.post(route+'/authorization', json=body, headers=HEADERS).status_code == 200
    assert app.state.external_analysis_worker.process_one()['state'] == 'succeeded'
    url = route.replace('external-analysis', 'external-intelligence')
    with closing(repo._connect()) as db:
        before = list(db.iterdump())
    app.state.external_analysis_worker.settings.model = None
    response = client.get(url)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['version'] == 'external-intelligence-v1'
    assert sum(data['counts'].values()) == len(data['items']) == 1
    assert data['items'][0]['analysis_state'] == 'available'
    assert data['items'][0]['evidence']
    assert data['processing']['reason'] == 'model_missing'
    assert model.calls == 1
    with closing(repo._connect()) as db:
        assert list(db.iterdump()) == before
    other = repo.create_project('Other synthetic')
    assert client.get(url.replace(project.id, other.id)).json()['items'] == []
    assert client.get(url.replace(project.id, 'missing')).status_code == 404
    assert client.get(url, headers={'Host': 'outside.test'}).status_code == 400
    assert repo.list_observations(project.id) == []


def test_intelligence_stale_analysis_is_not_reused(tmp_path, monkeypatch):
    s, app, client, route, draft = setup(tmp_path, monkeypatch)
    body = consent(draft, preview(client, route, draft))
    client.post(route+'/authorization', json=body, headers=HEADERS)
    app.state.external_analysis_worker.process_one()
    source = s[2]
    app.state.external_analysis_store.repo.get_source = lambda _: replace(source, content_sha256='e'*64)
    response = client.get(route.replace('external-analysis', 'external-intelligence')).json()
    assert response['stale_analysis_count'] == 1
    assert response['items'][0]['analysis'] is None
    assert response['items'][0]['analysis_state'] == 'not_available'
    assert s[4].calls == 1


def test_intelligence_team_and_direct_nonlocal_denied(tmp_path):
    from fastapi import FastAPI

    from project_watch.external_analysis_api import attach_external_analysis

    for team, host, expected in [(True, '127.0.0.1', 404), (False, 'outside.test', 403)]:
        app = FastAPI()
        attach_external_analysis(app, None, None, store=None, worker=None, team_mode=team)
        client = TestClient(app, base_url='http://'+host)
        assert client.get('/api/workspace/projects/any/external-intelligence').status_code == expected
