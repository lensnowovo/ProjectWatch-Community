import copy
import json

import pytest
from test_studio_application_parity import headers, setup_app


def payload(version=1):
    claim = {'text': 'Synthetic maker supplies connectors.', 'refs': ['N1']}
    return {'expected_version': version, 'idempotency_key': 'batch-1', 'producer': 'Codex',
            'sources': [{'id': 'N1', 'kind': 'public', 'title': 'New factory',
                         'url': 'https://company.example/news/1', 'text': 'Factory opened in July.',
                         'read_scope': 'summary', 'published_at': '2026-07-08'}],
            'news': [{'key': 'factory-open', 'title': 'Factory opened', 'summary': 'A new factory opened.',
                      'impact': 'Agent interpretation: monitor ramp-up.', 'category': 'company',
                      'event_at': '2026-07-03', 'refs': ['N1']}],
            'company': {'one_liner': claim, 'summary': claim, 'sections': [{'title': '业务与产品', 'points': [claim]}]},
            'brief': {'headline': 'Monitor factory ramp-up', 'summary': claim},
            'deep': {'headline': 'Monitor execution, not just capacity plans',
                     'position': {'supporting': [claim]}, 'industry': {'drivers': [claim]}}}


def route(pid):
    return f'/api/studio/agent/projects/{pid}/results'


def test_round_trip_atomic_replay_and_existing_pages(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    app.state.studio_store.set_note(pid, 'Keep human note')
    app.state.studio_store.put_brief(pid, {'headline': 'System snapshot', 'updated_at': '2025-01-01'})
    version = web.get(f'/api/studio/projects/{pid}').json()['project']['config_version']
    body = payload(version)
    response = web.post(route(pid), headers=headers(grant), json=body)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['job_status'] == 'not_requested' and result['news_count'] == 1
    assert web.post(route(pid), headers=headers(grant), json=body).json() == result
    detail = web.get(f'/api/studio/projects/{pid}').json()
    assert detail['brief']['headline'] == body['brief']['headline']
    assert detail['profile']['origin']['producer'] == 'Codex'
    assert detail['profile']['sections'][0]['points'][0]['refs'][0]['url'].startswith('https:')
    assert detail['project']['note'] == 'Keep human note' and not detail['jobs']
    assert app.state.studio_store.get_brief(pid)['headline'] == 'System snapshot'
    feed = web.get(f'/api/studio/feed?project_id={pid}').json()
    assert len(feed) == 1 and feed[0]['extra']['agent_result']['producer'] == 'Codex'
    assert feed[0]['published_at'] == '2026-07-08'
    events = web.get(f'/api/studio/events?project_id={pid}').json()
    assert events[0]['event_at'] == '2026-07-03'
    assert events[0]['sources'][0]['extra']['source_published_at'] == '2026-07-08'
    deep = web.get(f'/api/studio/projects/{pid}/insight').json()['record']
    assert deep['analysis']['headline'] == body['deep']['headline']
    assert deep['origin']['producer'] == 'Codex' and deep['analysis']['position']['verdict'] == 'unclear'
    changed = copy.deepcopy(body)
    changed['brief']['headline'] = 'different'
    assert web.post(route(pid), headers=headers(grant), json=changed).status_code == 409
    history = web.get(route(pid), headers=headers(grant)).json()
    assert len(history['items']) == 1
    assert web.get(route(pid)+'/current',headers=headers(grant)).json()['record']['brief']['headline']==body['brief']['headline']


@pytest.mark.parametrize('case', ['reader', 'revoked', 'other_project', 'stale'])
def test_permissions_and_cas(tmp_path, case):
    app, web, pid, grant = setup_app(tmp_path)
    body = payload()
    if case == 'reader':
        grant = web.post(f'/api/studio/projects/{pid}/agent-grants', json={'permission':'read','label':'reader'}).json()
    elif case == 'revoked':
        web.delete(f'/api/studio/projects/{pid}/agent-grants/{grant["id"]}')
    elif case == 'other_project':
        other = app.state.intake_worker.repository.create_project('Other')
        app.state.studio_store.upsert_project(other.id, {'legal_name':'Other'}, consent=False)
        pid = other.id
    else:
        body['expected_version'] = 99
    response = web.post(route(pid), headers=headers(grant), json=body)
    assert response.status_code == (409 if case == 'stale' else 403)
    assert not app.state.studio_store.list_events(project_id=pid)


@pytest.mark.parametrize('change', ['bad_url','private_url','commercial','unknown_ref','internal_news','missing_internal'])
def test_reject_before_any_partial_write(tmp_path, change):
    app, web, pid, grant = setup_app(tmp_path)
    body = payload()
    if change == 'bad_url':
        body['sources'][0]['url'] = 'javascript:alert(1)'
    elif change == 'private_url':
        body['sources'][0]['url'] = 'http://127.0.0.1/private'
    elif change == 'commercial':
        body['sources'][0]['kind'] = 'commercial'
    elif change == 'unknown_ref':
        body['company']['summary']['refs'] = ['absent']
    else:
        body['sources'].append({'id':'D1','kind':'internal','title':'BP','source_id':'not-in-project','locator':'slide 1'})
        if change == 'internal_news':
            body['news'][0]['refs'] = ['D1']
        else:
            body['company']['summary']['refs'] = ['D1']
    assert web.post(route(pid), headers=headers(grant), json=body).status_code in (403,422)
    assert not app.state.studio_store.list_events(project_id=pid)
    assert app.state.studio_store.get_profile(pid) is None


def test_later_revision_keeps_read_state_and_history(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    body = payload()
    first = web.post(route(pid), headers=headers(grant), json=body).json()
    event = app.state.studio_store.list_events(project_id=pid)[0]
    app.state.studio_store.set_event_status(event['id'], 'read')
    body['expected_version'] = first['version']
    body['idempotency_key'] = 'batch-2'
    body['news'][0]['impact'] = 'Updated interpretation.'
    assert web.post(route(pid), headers=headers(grant), json=body).status_code == 201
    events = app.state.studio_store.list_events(project_id=pid)
    assert len(events) == 1 and events[0]['status'] == 'read'
    assert len(web.get(route(pid), headers=headers(grant)).json()['items']) == 2
    # Persistent reopen, without invoking providers.
    from project_watch.studio.store import StudioStore
    reopened = StudioStore(app.state.studio_store.db_path)
    assert json.loads(reopened.get_state(f'agent_results:{pid}'))['producer'] == 'Codex'


def test_internal_scope_exact_locator_and_no_public_export(tmp_path):
    from project_watch.domain import FragmentInput

    app, web, pid, public = setup_app(tmp_path)
    service = app.state.studio_application
    private = service.issue_grant(pid, 'write', 'private', allow_internal=True)
    repo = app.state.intake_worker.repository
    source = repo.add_source(pid, kind='document', reference='synthetic.txt', content_sha256='a'*64)
    repo.enqueue_intake(pid, source.id)
    job = repo.claim_next_intake()
    repo.complete_intake(job.id, [FragmentInput(ordinal=1,locator='line 1',text='Private synthetic business')])
    body = payload()
    body['sources'].append({'id':'D1','kind':'internal','title':'Private', 'source_id':source.id,'locator':'line 1'})
    body['company']['summary']['refs'] = ['D1']
    assert web.post(route(pid), headers=headers(public), json=body).status_code == 403
    response = web.post(route(pid), headers=headers(private), json=body)
    assert response.status_code == 201, response.text
    assert not web.get(route(pid),headers=headers(public)).json()['items']
    assert web.get(route(pid)+'/current',headers=headers(public)).json()['record'] is None
    assert web.get(route(pid)+'/current',headers=headers(private)).json()['record']['company']
    assert len(web.get(route(pid),headers=headers(private)).json()['items']) == 1
    package = service.material_package(pid, include_internal=False)
    assert 'Private synthetic business' not in json.dumps(package)
    profile = web.get(f'/api/studio/projects/{pid}').json()['profile']
    assert profile['origin']['producer']=='Codex'
    assert not app.state.studio_store.jobs(pid)


def test_removed_project_and_stale_revision_do_not_publish(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    app.state.studio_store.remove_project(pid)
    assert web.post(route(pid), headers=headers(grant), json=payload()).status_code == 404
    assert not app.state.studio_store.list_events(project_id=pid)


def test_unreferenced_high_confidence_is_not_published(tmp_path):
    _, web, pid, grant = setup_app(tmp_path)
    body = payload()
    body['deep']['technology']={'maturity':'frontier','consensus':'consensus','confidence':'high'}
    assert web.post(route(pid),headers=headers(grant),json=body).status_code==201
    technology=web.get(f'/api/studio/projects/{pid}/insight').json()['record']['analysis']['technology']
    assert technology['maturity']=='unclear' and technology['confidence']=='low'


def test_unknown_event_date_uses_old_publication_not_import_time_for_recency(tmp_path):
    app, web, pid, grant=setup_app(tmp_path)
    body=payload()
    body['news'][0]['event_at']=None
    body['sources'][0]['published_at']='2020-01-01'
    assert web.post(route(pid),headers=headers(grant),json=body).status_code==201
    assert not app.state.studio_store.list_events(project_id=pid,since_days=60)
    event=app.state.studio_store.list_events(project_id=pid)[0]
    assert event['event_at'] is None
    assert event['sources'][0]['published_at']=='2020-01-01'
