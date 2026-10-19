import pytest
from test_studio_event_state import add, setup


def test_cancelled_fact_has_neutral_cited_concern_and_reversal_resolves_it(tmp_path):
    from project_watch.studio.concerns import ConcernStore
    service, pid, subject = setup(tmp_path)
    fact = add(service, pid, subject, 'cancelled', '2026-08-01')
    concerns = ConcernStore(service)
    row = concerns.read(pid)['items'][0]
    assert row['fact_ids'] == [fact['id']] and row['status'] == 'open' and row['investment_judgment'] is False
    add(service, pid, subject, 'resumed', '2026-08-02')
    assert concerns.read(pid)['items'][0]['status'] == 'resolved_by_source_change'


def test_feedback_is_versioned_idempotent_and_survives_restart(tmp_path):
    from project_watch.studio.application import StudioApplication, VersionConflict
    from project_watch.studio.concerns import ConcernFeedback, ConcernStore
    service, pid, subject = setup(tmp_path)
    add(service, pid, subject, 'delayed', '2026-08-01')
    concerns = ConcernStore(service)
    row = concerns.read(pid)['items'][0]
    body = ConcernFeedback(concern_id=row['id'], status='resolved', reason='human review', expected_version=service.project(pid)['config_version'], idempotency_key='feedback')
    first = concerns.feedback(pid, body)
    assert first['job_status'] == 'not_requested' and concerns.feedback(pid, body) == first
    assert ConcernStore(StudioApplication(service.store)).read(pid)['items'][0]['status'] == 'resolved'
    with pytest.raises(VersionConflict):
        concerns.feedback(pid, body.model_copy(update={'idempotency_key': 'another'}))
    next_body = body.model_copy(update={'status': 'open', 'expected_version': first['version'], 'idempotency_key': 'reopen'})
    concerns.feedback(pid, next_body)
    assert len(concerns.read(pid)['items'][0]['feedback_history']) == 2


def test_withdrawal_and_forecast_never_become_business_alert(tmp_path):
    from project_watch.studio.concerns import ConcernStore
    service, pid, subject = setup(tmp_path)
    fact = add(service, pid, subject, 'cancelled', '2026-08-01', basis='forecast')
    concerns = ConcernStore(service)
    assert concerns.read(pid)['items'][0]['status'] == 'observation_insufficient'
    service.evidence.dependencies.withdraw(pid, fact['refs'][0]['evidence_version_id'], 'source corrected', actor='fixture')
    assert concerns.read(pid)['items'][0]['status'] == 'observation_insufficient'


def test_owner_agent_parity_privacy_and_feedback_permissions(tmp_path):
    from fastapi.testclient import TestClient
    from test_studio_application_parity import headers

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.application import StudioApplication
    from project_watch.studio.monitoring_contracts import ObjectInput
    app = create_studio_app(tmp_path, start_worker=False)
    service = StudioApplication(app.state.studio_store)
    project = app.state.intake_worker.repository.create_project('Alpha')
    pid = project.id
    service.store.upsert_project(pid, {}, consent=False)
    subject = service.objects.save(pid, ObjectInput(kind='company', name='Alpha', visibility='public'), actor='user')
    add(service, pid, subject, 'delayed', '2026-08-01')
    web = TestClient(app)
    reader = web.post(f'/api/studio/projects/{pid}/agent-grants', json={'permission': 'read', 'label': 'reader'}).json()
    writer = web.post(f'/api/studio/projects/{pid}/agent-grants', json={'permission': 'write', 'label': 'writer'}).json()
    for part in ('milestones', 'concerns'):
        route = f'/api/studio/agent/projects/{pid}/{part}'
        response = web.get(route, headers=headers(reader))
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert response.json()['items'] == web.get(f'/api/studio/projects/{pid}/{part}').json()['items']
        assert web.get(route).status_code == 401
        assert web.get(route.replace(pid, 'outside'), headers=headers(reader)).status_code == 403
    row = web.get(f'/api/studio/projects/{pid}/concerns').json()['items'][0]
    ref = row['refs'][0]
    excerpt_route = f"/api/studio/projects/{pid}/evidence/{ref['evidence_version_id']}/parts/{ref['part_index']}"
    excerpt = web.get(excerpt_route, params={'start': ref['start'], 'end': ref['end']})
    assert excerpt.status_code == 200 and excerpt.headers['cache-control'] == 'no-store'
    assert 'Alpha Series A delayed' in excerpt.json()['quote']
    assert web.get(excerpt_route, params={'end': 2001}).status_code == 422
    assert web.get(excerpt_route.replace(pid, 'outside'), params={'end': ref['end']}).status_code == 404
    assert web.get(excerpt_route, headers=headers(reader), params={'end': ref['end']}).status_code == 403
    version = web.get(f'/api/studio/agent/projects/{pid}', headers=headers(writer)).json()['config_version']
    body = {'concern_id': row['id'], 'status': 'resolved', 'reason': 'Private human memo',
            'expected_version': version, 'idempotency_key': 'synthetic-feedback'}
    route = f'/api/studio/agent/projects/{pid}/concern-feedback'
    assert web.post(route, headers=headers(reader), json=body).status_code == 403
    response = web.post(route, headers=headers(writer), json=body)
    assert response.status_code == 200
    assert web.post(route, headers=headers(writer), json=body).json() == response.json()
    public = web.get(f'/api/studio/agent/projects/{pid}/concerns', headers=headers(reader)).json()
    assert public['items'][0]['status'] == 'resolved' and 'Private human memo' not in str(public)
    assert 'Private human memo' in str(web.get(f'/api/studio/projects/{pid}/concerns').json())
    assert web.post(route, headers=headers(writer), json={**body, 'idempotency_key': 'conflict'}).status_code == 409
    assert not service.store.jobs(pid)
