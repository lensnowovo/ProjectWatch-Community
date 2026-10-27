"""Normal runtime entry dispatches host work before DSH, using isolated stores."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_collection_pages import work

from project_watch.studio import collection_host, dsh_runtime
from project_watch.studio.app import create_studio_app
from project_watch.studio.collection_ledger import STAGE
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import ModelConfig


@pytest.fixture
def setup(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    settings = pipeline.settings_store.load()
    settings.model = ModelConfig(base_url='https://model.example.invalid/v1', model='synthetic', api_key='synthetic')
    pipeline.settings_store.save(settings)
    project = pipeline.repository.create_project('Synthetic Host')
    pipeline.store.upsert_project(project.id, {'legal_name': 'Synthetic Host'}, consent=True)
    pipeline.store.enqueue(project.id, 'external')
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline._execution_state.current = execution
    return pipeline, execution


def response(url):
    host = urlsplit(url).hostname
    if host == 'api.openalex.org':
        return Response(200, {}, json.dumps({'results': [work()], 'meta': {'count': 1}}).encode())
    if host == 'www.cninfo.com.cn':
        return Response(200, {}, b'{"announcements":[],"totalAnnouncement":0}')
    pytest.fail('unexpected provider dispatch: ' + host)


def test_runtime_collects_and_ingests_before_dsh_and_replay_adds_no_requests(setup, monkeypatch):
    pipeline, execution = setup
    calls, statuses = [], []

    def send(method, url, *_args):
        calls.append(urlsplit(url).hostname)
        assert execution.read_checkpoint('dsh_runtime_phase')['phase'] == 'collect'
        return response(url)

    pipeline.http_factory = lambda _: Http(send)

    def model_turn(research, *_args):
        status = research.tool('pw_status', {})
        assert len(calls) == 3
        assert len(status['collected_sources']['sources']) == 1
        assert status['collection']['status'] == 'partial'  # unconfigured search and missing scopes stay visible
        assert execution.read_checkpoint('dsh_runtime_phase')['phase'] == 'read'
        source = research.tool('pw_read', {'source_id': status['collected_sources']['sources'][0]['id']})
        assert source['source_type'] == 'structured_record'
        statuses.append(status)
        return status

    monkeypatch.setattr(dsh_runtime, 'run_dsh', model_turn)
    for _ in range(2):
        dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert len(calls) == 3 and len(statuses) == 2
    assert execution.usage()['open_data']['requests'] == 3
    assert execution.read_checkpoint(STAGE)['plan']['mode'] == 'first'


def test_runtime_does_not_raise_explicit_zero_budget(setup, monkeypatch):
    pipeline, execution = setup
    execution.budgets.update(search=0, model=0, page=0, open_data=0)
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('budget must prevent dispatch'))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda research, *_: research.tool('pw_status', {}))
    result = dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert all(execution.budgets[k] == 0 for k in ('search', 'model', 'page', 'open_data'))
    assert not execution.usage()
    assert {r['outcome'] for r in result['collection']['rows']} >= {'budget_deferred', 'not_configured'}


def test_missing_model_configuration_stops_before_any_collection(setup, monkeypatch):
    pipeline, execution = setup
    settings = pipeline.settings_store.load()
    settings.model = ModelConfig()
    pipeline.settings_store.save(settings)
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('missing model must prevent collection'))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: pytest.fail('missing model'))
    with pytest.raises(ValueError, match='configured OpenAI-compatible model'):
        dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert not execution.usage()
    assert execution.read_checkpoint(STAGE) is None


def test_host_unknown_stops_before_model_and_is_not_resent(setup, monkeypatch):
    pipeline, execution = setup
    calls = []

    def lost(*_args):
        calls.append(1)
        raise HttpError('connection_failed')

    pipeline.http_factory = lambda _: Http(lost)
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: pytest.fail('unknown must block DSH'))
    for _ in range(2):
        with pytest.raises(HttpError, match='request_outcome_unknown'):
            dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert calls == [1]
    assert execution.usage()['open_data']['unknown'] == 1


def test_legacy_started_round_does_not_acquire_new_host_work(setup, monkeypatch):
    pipeline, execution = setup
    original_budgets = dict(execution.budgets)
    execution.checkpoint('dsh_state', {'sources': [], 'records': [], 'searches': [],
                                     'errors': [], 'events': [], 'finished': None})
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('legacy round must not expand'))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda research, *_: research.tool('pw_status', {}))
    result = dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert result['collection']['reason'] == 'legacy_round_without_collection_manifest'
    assert result['collection']['status'] == 'partial'
    assert execution.read_checkpoint(STAGE) is None
    assert execution.budgets == original_budgets


def test_runtime_scope_never_reads_internal_plan(setup, monkeypatch):
    pipeline, execution = setup
    pipeline.monitoring_plan.add(execution.job['project_id'], {'text': 'PRIVATE sentinel',
        'queries': ['PRIVATE sentinel']}, actor='human', origin='human', visibility='internal')
    calls = []

    def send(_method, url, *_args):
        calls.append(url)
        return response(url)

    pipeline.http_factory = lambda _: Http(send)
    monkeypatch.setattr(pipeline.store, 'get_plan', lambda *_: pytest.fail('raw plan is not a public projection'))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: None)
    dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert 'PRIVATE' not in json.dumps(execution.read_checkpoint(STAGE))
    assert 'PRIVATE' not in str(calls)


def test_scope_withdrawal_mid_page_stops_next_request(setup, monkeypatch):
    pipeline, execution = setup
    watch = pipeline.monitoring_plan.add(execution.job['project_id'], {'text': 'Synthetic watch',
        'queries': ['Synthetic watch']}, actor='human', origin='human', visibility='public')
    calls = []

    def send(_method, url, *_args):
        calls.append(url)
        pipeline.monitoring_plan.update(watch['id'], {'status': 'retired'}, actor='human')
        return response(url)

    pipeline.http_factory = lambda _: Http(send)
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda *_: pytest.fail('scope changed'))
    with pytest.raises(RuntimeError, match='scope changed'):
        dsh_runtime.run_bootstrap(pipeline, execution, None)
    assert len(calls) == 1


def test_analysis_only_keeps_existing_evidence_without_collection_admission(setup, monkeypatch):
    pipeline, execution = setup
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('analysis-only cannot collect'))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda research, *_: research.tool('pw_status', {}))
    result = dsh_runtime.run_monitor(pipeline, execution, None, analysis_only=True)
    assert result['collection'] is None
    assert execution.read_checkpoint(collection_host.ADMISSION) is None


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_official_dsh_full_entry_uses_automatically_collected_evidence(setup):
    pipeline, execution = setup
    source_calls, model_calls, selected = [], [], {}

    def send(_method, url, _headers, raw, _timeout):
        if urlsplit(url).hostname != 'model.example.invalid':
            source_calls.append(url)
            return response(url)
        assert len(source_calls) == 3
        payload = json.loads(raw)
        if 'tools' not in payload:
            from test_studio_dsh_monitor import synthetic_review
            review = json.loads(payload['messages'][-1]['content'])
            assert review['purpose'] == 'public_record_admission'
            return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(synthetic_review(review))},
                'finish_reason': 'stop'}]}).encode())
        index = len(model_calls)
        if index == 1:
            directory = json.loads(payload['messages'][-1]['content'])['collected_sources']
            selected.update(directory['sources'][0])
        if index == 2:
            evidence = json.loads(payload['messages'][-1]['content'])
            assert evidence['source_type'] == 'structured_record'
            assert evidence['evidence_version_id'] == selected['evidence_version_id']
        operations = [('pw_status', {}), ('pw_read', {'source_id': selected.get('id', '')}),
                      ('pw_record', {'kind': 'business', 'source_id': selected.get('id', ''),
                                     'quote': 'Synthetic abstract', 'text': 'Synthetic abstract',
                                     'label': 'Synthetic description'}), ('pw_finish', {'gaps': {}})]
        assert index < len(operations)
        name, args = operations[index]
        model_calls.append(name)
        return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': f'host-{index}', 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args)}}]},
            'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())

    pipeline.http_factory = lambda _: Http(send)
    result = dsh_runtime.run_bootstrap(pipeline, execution, Path(os.environ['PW_DSH_TEST_RUNTIME']))
    assert len(source_calls) == 3 and len(model_calls) == 4
    assert result['status'] == result['collection']['status'] == 'partial'
    [fact] = pipeline.store.get_public_profile(execution.job['project_id'])['facts']
    assert fact['refs'][0]['evidence_version_id'] == selected['evidence_version_id']
    assert execution.usage()['model']['completed'] == 5
    assert execution.usage()['open_data']['completed'] == 3
