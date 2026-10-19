"""Runtime truth and explicitly quoted abbreviations; entirely synthetic/offline."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.app import create_studio_app
from project_watch.studio.competition_research import discover_candidates


def test_worker_runtime_state_is_read_only(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Runtime').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.store.enqueue(pid, 'external')
    before = pipeline.store.jobs(pid)
    monkeypatch.setattr(pipeline, 'tools', lambda: pytest.fail('read must not call providers'))
    monkeypatch.setattr(pipeline.store, 'enqueue', lambda *_a, **_k: pytest.fail('read must not schedule'))
    client = TestClient(app)
    for _ in range(2):
        response = client.get(f'/api/studio/projects/{pid}').json()
        assert response['execution'] == {'analysis': 'stopped', 'parsing': 'stopped',
            'health': {'state': 'stopped', 'label': '后台未运行', 'restart_required': False}}
        assert response['project']['worker_state'] == 'stopped'
        assert response['monitoring_progress']['state'] == 'queued'
        assert client.get('/api/studio/projects').json()[0]['worker_state'] == 'stopped'
    assert pipeline.store.jobs(pid) == before
    for worker in (app.state.studio_worker, app.state.intake_worker):
        worker._thread = SimpleNamespace(is_alive=lambda: True)
        assert worker.runtime_state() == 'running'
        worker._stop.set()
        assert worker.runtime_state() == 'stopping'
        worker._thread = SimpleNamespace(is_alive=lambda: False)
        assert worker.runtime_state() == 'stopped'


def discovery(tmp_path, quote, name='合成光学有限公司'):
    service, pid = make_service(tmp_path / 'aliases.sqlite3')
    target = service.objects.ensure_subject(pid)
    evidence = service.evidence.save(sample(pid, quote).model_copy(update={'visibility': 'public'}))
    data = {'candidates': [{'name': name, 'source': 'D1', 'quote': quote}]}
    return service, pid, target, evidence, data


def test_discovery_retains_explicit_alias_and_replays_without_versions(tmp_path):
    quote = '合成光学有限公司（简称：合成光学）提供车载连接器。'
    service, pid, target, evidence, data = discovery(tmp_path, quote)
    obj = discover_candidates(service, pid, target['id'], [evidence], data)[0]
    assert [a['name'] for a in obj['aliases']] == ['合成光学']
    assert obj['aliases'][0]['visibility'] == 'public'
    assert obj['refs'][0]['quote'] == quote
    again = discover_candidates(service, pid, target['id'], [evidence], data)[0]
    assert again['id'] == obj['id'] and again['version'] == obj['version']
    from project_watch.studio.race import peers_for
    peers = peers_for({'name': 'Synthetic Optics'}, {'competitors': [{'name': obj['name']}]}, {},
                      public_objects=service.objects.page(pid, include_internal=False))
    assert peers[0]['aliases'] == ['合成光学']


@pytest.mark.parametrize('quote', [
    '合成光学有限公司与合成光学交流合作。',
    '合成光学有限公司（曾简称：合成光学）提供产品。',
    '合成光学有限公司（产品名：合成光学）提供产品。',
    '合成光学有限公司与甲方（简称：合成光学）合作。',
])
def test_co_mentions_and_historical_product_labels_are_not_aliases(tmp_path, quote):
    service, pid, target, evidence, data = discovery(tmp_path, quote)
    assert discover_candidates(service, pid, target['id'], [evidence], data)[0]['aliases'] == []


def test_explicit_alias_preserves_user_override_and_lost_guard(tmp_path):
    quote = '合成光学有限公司（以下简称“合成光学”）研发连接器。'
    service, pid, target, evidence, data = discovery(tmp_path, quote)
    obj = discover_candidates(service, pid, target['id'], [evidence], data)[0]
    from project_watch.studio.monitoring_contracts import Alias, ObjectInput
    item = ObjectInput.model_validate({k: obj[k] for k in ObjectInput.model_fields})
    human = service.objects.save(pid, item.model_copy(update={'aliases': (Alias(name='人工名称', visibility='public'),)}),
                                 actor='user', object_id=obj['id'], expected_version=obj['version'])
    assert discover_candidates(service, pid, target['id'], [evidence], data)[0] == human
    def lost(_db):
        raise ValueError('synthetic lost lease')
    with pytest.raises(ValueError, match='lost lease'):
        discover_candidates(service, pid, target['id'], [evidence], data, guard=lost)
    assert service.objects.get(pid, obj['id']) == human


@pytest.mark.parametrize('change', ['superseded', 'withdrawn'])
def test_stale_source_cannot_reintroduce_aliases_and_history_is_preserved(tmp_path, change):
    quote = '合成光学有限公司（简称：合成光学）提供车载连接器。'
    service, pid, target, evidence, data = discovery(tmp_path, quote)
    obj = discover_candidates(service, pid, target['id'], [evidence], data)[0]
    if change == 'withdrawn':
        service.evidence.dependencies.withdraw(pid, evidence['id'], 'synthetic withdrawal', actor='user')
    else:
        service.evidence.save(sample(pid, '合成光学有限公司的新正文，不包含旧简称。').model_copy(update={'visibility': 'public'}))
    assert discover_candidates(service, pid, target['id'], [evidence], data) == []
    view = service.objects.page(pid, include_internal=False, current_aliases_only=True)
    assert next(r for r in view if r['id'] == obj['id'])['aliases'] == []
    assert '合成光学' not in service.objects.public_names(pid)
    assert service.objects.get(pid, obj['id'])['aliases'] == obj['aliases']


def test_excluded_abbreviation_cannot_resurrect_candidate(tmp_path, monkeypatch):
    quote = '合成光学有限公司（简称：合成光学）提供车载连接器。'
    service, pid, target, evidence, data = discovery(tmp_path, quote)
    monkeypatch.setattr(service.relationships, 'view', lambda *_a, **_k:
                        {'exclusions': [{'company_wide': True, 'peer_names': ['合成光学']}], 'items': []})
    assert discover_candidates(service, pid, target['id'], [evidence], data) == []
    assert len(service.objects.page(pid, include_internal=False)) == 1


def test_discovered_alias_reaches_race_api_and_disappears_after_withdrawal(tmp_path, monkeypatch):
    def deny(*_a, **_k):
        pytest.fail('read projection cannot dispatch providers')
    monkeypatch.setattr('project_watch.studio.http.Http.get', deny)
    monkeypatch.setattr('project_watch.studio.http.Http.post_json', deny)
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    service = app.state.studio_application
    pid = pipeline.repository.create_project('Synthetic Connector').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    target = service.objects.ensure_subject(pid)
    quote = '合成光学有限公司（简称：星光品牌）提供车载连接器。'
    evidence = service.evidence.save(sample(pid, quote).model_copy(update={'visibility': 'public'}))
    discover_candidates(service, pid, target['id'], [evidence],
                        {'candidates': [{'name': '合成光学有限公司', 'source': 'D1', 'quote': quote}]})
    pipeline.store.put_plan(pid, {'competitors': [{'name': '合成光学有限公司'}]})
    pipeline.store.create_event(pid, {'category': 'competitor', 'title': '星光品牌获车企定点',
                                     'importance': 'notable', 'event_at': '2026-01-01'}, [])
    before = pipeline.store.jobs(pid)
    client = TestClient(app)
    route = f'/api/studio/projects/{pid}/race?template=hardware'
    assert client.get(route).json()['rows'][1]['stage']['label'] == '定点'
    service.evidence.dependencies.withdraw(pid, evidence['id'], 'synthetic withdrawal', actor='user')
    assert client.get(route).json()['rows'][1]['stage'] is None
    assert pipeline.store.jobs(pid) == before
