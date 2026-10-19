import copy

import pytest

from project_watch.monitor_activation import MonitorActivationStore
from project_watch.monitor_strategy import validate_strategy
from project_watch.repository import ProjectRepository


def setup(tmp_path):
    repo = ProjectRepository(tmp_path / 'watch_test.sqlite3')
    project = repo.create_project('Synthetic Project')
    source = repo.add_source(project.id, kind='document', reference='BP.pptx', content_sha256='a'*64)
    from project_watch.domain import FragmentInput
    job = repo.enqueue_intake(project.id, source.id)
    repo.claim_next_intake()
    repo.complete_intake(job.id, (FragmentInput(1, 'slide 1', 'Synthetic public company\nIndustrial vision systems'),), needs_ocr=False)
    return repo, project, source


def fixture(tmp_path):
    repo, project, source = setup(tmp_path)
    fragments = repo.list_fragments(source.id)
    packet = dict(project_id=project.id, source_id=source.id, source_sha256=source.content_sha256,
                  reference=source.reference,
                  excerpts=[dict(id='bp1', locator=fragments[0].locator, text=fragments[0].text)])
    output = dict(understanding=dict(text='Synthetic company makes vision systems.', evidence=['bp1']),
                  scopes=[dict(category='company', anchor='Synthetic public company',
                               question='What has changed in this company?', rationale='Company identified in BP.', evidence=['bp1']),
                          dict(category='sector', anchor='Industrial vision systems',
                               question='Which products are changing?', rationale='BP product direction.', evidence=['bp1'])], unknowns=[])
    strategy = validate_strategy(packet, output)
    return repo, packet, strategy


def activate(store, packet, strategy):
    return store.activate(packet, strategy, approved_terms=[s['anchor'] for s in strategy['scopes']],
                          provider='gdelt', actor='local-operator', consent=True)


def test_atomic_activation_and_restart_do_not_repeat_or_resume(tmp_path):
    repo, packet, strategy = fixture(tmp_path)
    store = MonitorActivationStore(repo)
    first = activate(store, packet, strategy)
    assert len(repo.list_monitoring_jobs(packet['project_id'])) == 2
    assert all(p.enabled for p in repo.list_monitoring_plans(packet['project_id']))
    plan = repo.list_monitoring_plans(packet['project_id'])[0]
    repo.set_monitoring_plan_enabled(plan.id, False)
    reopened = MonitorActivationStore(ProjectRepository(repo.db_path))
    assert activate(reopened, packet, strategy)['id'] == first['id']
    assert not repo.get_monitoring_plan(plan.id).enabled
    assert len(repo.list_monitoring_jobs(packet['project_id'])) == 2


def test_strategy_created_jobs_are_readable_through_api(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.api import create_app
    repo, packet, strategy = fixture(tmp_path)
    activate(MonitorActivationStore(repo),packet,strategy)
    client=TestClient(create_app(repo.db_path),base_url='http://127.0.0.1')
    response=client.get(f"/api/projects/{packet['project_id']}/monitoring/jobs")
    assert response.status_code==200
    assert len(response.json())==2
    assert all(j['trigger']=='strategy' for j in response.json())


@pytest.mark.parametrize('problem', ['foreign', 'changed', 'fabricated', 'unapproved'])
def test_no_partial_activation_when_evidence_or_consent_invalid(tmp_path, problem):
    repo, packet, strategy = fixture(tmp_path)
    store = MonitorActivationStore(repo)
    if problem == 'foreign':
        packet['project_id'] = repo.create_project('Other').id
    elif problem == 'changed':
        packet['source_sha256'] = 'b'*64
    elif problem == 'fabricated':
        strategy['scopes'][1]['anchor'] = 'Unsupported term'
    else:
        strategy = copy.deepcopy(strategy)
    with pytest.raises(ValueError):
        if problem == 'unapproved':
            store.activate(packet, strategy, approved_terms=[], provider='gdelt', actor='test', consent=True)
        else:
            activate(store, packet, strategy)
    assert repo.list_monitoring_plans(packet['project_id']) == []


def test_partial_source_failure_is_not_claimed_as_success(tmp_path):
    repo, packet, strategy = fixture(tmp_path)
    store = MonitorActivationStore(repo)
    activate(store, packet, strategy)
    plans = repo.list_monitoring_plans(packet['project_id'])
    repo.record_source_run(plans[0].id, status='unreachable', error_code='connection_failed', idempotency_key='one')
    repo.record_source_run(plans[1].id, status='no_results', error_code=None, idempotency_key='two')
    status = store.read(packet['project_id'])
    assert status['coverage'] == 'partial'
    assert status['succeeded'] == 1 and status['failed'] == 1


def test_conflicting_last_scope_rolls_back_entire_batch(tmp_path):
    from project_watch.external_focus import ExternalFocusStore
    from project_watch.repository import ReviewConflictError
    repo, packet, strategy = fixture(tmp_path)
    prior = ExternalFocusStore(repo).create(packet['project_id'], category='industry',
                                          term='Industrial vision systems', provider='gdelt')
    store = MonitorActivationStore(repo)
    with pytest.raises(ReviewConflictError):
        activate(store, packet, strategy)
    assert [p.id for p in repo.list_monitoring_plans(packet['project_id'])] == [prior.id]
    assert repo.list_monitoring_jobs(packet['project_id']) == []
    assert store.read(packet['project_id']) is None


def test_new_strategy_preserves_existing_paused_scope_and_history(tmp_path):
    repo, packet, strategy = fixture(tmp_path)
    store = MonitorActivationStore(repo)
    original = activate(store, packet, strategy)
    plan = repo.list_monitoring_plans(packet['project_id'])[0]
    repo.set_monitoring_plan_enabled(plan.id, False)
    strategy['scopes'][0]['question'] = 'Has the product mix changed this year?'
    updated = activate(store, packet, strategy)
    assert updated['id'] != original['id']
    assert not repo.get_monitoring_plan(plan.id).enabled
    assert len(repo.list_monitoring_jobs(packet['project_id'])) == 2


def test_newer_bp_version_invalidates_old_activation(tmp_path):
    repo, packet, strategy = fixture(tmp_path)
    store = MonitorActivationStore(repo)
    repo.add_source(packet['project_id'], kind='document', reference=packet['reference'], content_sha256='b'*64)
    with pytest.raises(ValueError, match='newer source version'):
        activate(store, packet, strategy)
    assert repo.list_monitoring_jobs(packet['project_id']) == []


@pytest.mark.parametrize('enabled', [False, True])
def test_reuses_same_authorized_scope_across_providers_without_resuming(tmp_path, enabled):
    from project_watch.external_focus import ExternalFocusStore
    repo, packet, strategy = fixture(tmp_path)
    prior = ExternalFocusStore(repo).create(packet['project_id'], category='company',
                                          term='Synthetic public company', provider='bing')
    repo.set_monitoring_plan_enabled(prior.id, enabled)
    store = MonitorActivationStore(repo)
    activate(store, packet, strategy)
    plans = repo.list_monitoring_plans(packet['project_id'])
    assert len(plans) == 2
    assert repo.get_monitoring_plan(prior.id).enabled == enabled
    scope = next(s for s in store.read(packet['project_id'])['scopes'] if s['category'] == 'company')
    assert scope['plan_id'] == prior.id and not scope['owned']
    assert len(repo.list_monitoring_jobs(packet['project_id'])) == 1


def test_api_consent_binding_and_idempotent_start(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from project_watch.api import create_app
    repo, packet, strategy = fixture(tmp_path)
    monkeypatch.setenv('PROJECT_WATCH_SETTINGS_TOKEN', 'test-operator-token')
    client = TestClient(create_app(repo.db_path), base_url='http://127.0.0.1')
    url = f'/api/workspace/projects/{packet["project_id"]}/monitor-activation'
    body = dict(packet=packet, strategy=strategy, provider='gdelt')
    headers = {'Origin': 'http://127.0.0.1', 'X-Settings-Token': 'test-operator-token'}
    assert client.post(url+'/preview', json=body).status_code == 403
    assert client.post(url+'/preview', json=body, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    preview = client.post(url+'/preview', json=body, headers=headers)
    assert preview.status_code == 200, preview.text
    assert repo.list_monitoring_jobs(packet['project_id']) == []
    request = {**body, 'consent': True, 'approved_terms': preview.json()['terms'],
               'disclosure_digest': preview.json()['disclosure_digest']}
    assert client.post(url, json={**request, 'provider':'brave'}, headers=headers).status_code == 409
    assert client.post(url, json={**request, 'consent':False}, headers=headers).status_code == 409
    start = client.post(url, json=request, headers=headers)
    assert start.status_code == 202, start.text
    assert client.post(url, json=request, headers=headers).json()['id'] == start.json()['id']
    read = client.get(f'/api/projects/{packet["project_id"]}/monitoring/external').json()
    assert read['monitoring_strategy']['coverage'] == 'pending'
    assert len(repo.list_monitoring_jobs(packet['project_id'])) == 2
