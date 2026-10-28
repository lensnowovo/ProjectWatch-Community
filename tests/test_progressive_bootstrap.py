"""Real default entry, isolated stores and scripted provider; no paid calls."""
import json
import os
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import test_collection_host as fixtures
from test_collection_pages import work
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import dsh_runtime, progressive_bootstrap
from project_watch.studio.collection_view import discovered_sources
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response


@pytest.fixture
def setup(tmp_path):
    return fixtures.setup.__wrapped__(tmp_path)


def install_provider(setup, monkeypatch, *, native=False, failure=None):
    pipeline, execution = setup
    calls, roles = [], []
    active = {}
    original = dsh_runtime.run_dsh

    def send(_method, url, _headers, raw, _timeout):
        host = urlsplit(url).hostname
        if host == 'api.openalex.org':
            return Response(200, {}, json.dumps({'results': [work(i) for i in (1, 2, 3)], 'meta': {'count': 3}}).encode())
        if host == 'www.cninfo.com.cn':
            return Response(200, {}, b'{"announcements":[],"totalAnnouncement":0}')
        assert host == 'model.example.invalid'
        body = json.loads(raw)
        calls.append(body)
        assert execution.read_checkpoint(progressive_bootstrap.OWNER)['owner'] == 'materials'
        assert discovered_sources(pipeline.store, execution.job['project_id'])['total'] == 3
        if 'tools' in body:
            assert [t['function']['name'] for t in body['tools']] == ['pw_submit_records']
            if failure == 'unknown':
                raise HttpError('connection_failed')
            source = active['source']
            ref, bound = next(iter(source['refs'].items()))
            if 'Synthetic paper 1' in bound['quote']:
                message = {'role': 'assistant', 'content': 'No candidate.'}
                finish = 'stop'
            else:
                text = 'Synthetic abstract'
                start = bound['quote'].index(text)
                kind = 'industry' if 'Synthetic paper 2' in bound['quote'] else 'competitor'
                proposal = {'observations': [{'kind': kind, 'subject': 'Synthetic Host', 'label': 'Synthetic evidence',
                    'text': text, 'refs': [ref], 'start': start, 'end': start + len(text)}]}
                if failure == 'oversize' and kind == 'industry':
                    proposal['observations'][0]['text'] = 'x' * 601
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'candidate', 'type': 'function',
                    'function': {'name': 'pw_submit_records', 'arguments': json.dumps(proposal)}}]}
                finish = 'tool_calls'
        else:
            review = json.loads(body['messages'][-1]['content'])
            assert review['attribution_context']['confirmed_company'] == 'Synthetic Host'
            assert all('"subject": "Synthetic Host"' in v for v in review['fields'].values())
            expected = {'competitor'} if failure == 'oversize' else {'industry', 'competitor'}
            assert expected == {json.loads(v)['kind'] for v in review['fields'].values()}
            checked = synthetic_review(review)
            if failure == 'semantic':
                checked['checks'][0].update(status='insufficient', reason='Synthetic unsupported attribution')
            message, finish = {'role': 'assistant', 'content': json.dumps(checked)}, 'stop'
        return Response(200, {}, json.dumps({'choices': [{'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 10}}).encode())

    pipeline.http_factory = lambda _: Http(send)

    def turn(research, gateway, runtime, directory, **kwargs):
        assert research.tools == ('pw_submit_records',)  # no legacy extraction turn
        active['source'] = json.loads(research.task)
        roles.append(research.session_id)
        if native:
            return original(research, gateway, runtime, directory, **kwargs)
        reply = gateway.model_call({'model': gateway.model.model, 'messages': [{'role': 'user', 'content': research.task}],
            'tools': [{'function': {'name': 'pw_submit_records'}}]})
        message = reply['choices'][0]['message']
        if message.get('tool_calls'):
            call = message['tool_calls'][0]['function']
            research.tool(call['name'], json.loads(call['arguments']))
            return research.state['finished']
        research.execution.checkpoint(research.runtime_receipt_stage, {'initialized': True, 'prompted': True,
            'idle': True, 'drained': True, 'error': 'dsh_no_valid_submission'})
        raise HttpError('dsh_no_valid_submission')

    monkeypatch.setattr(dsh_runtime, 'run_dsh', turn)
    return calls, roles


def run(setup):
    return dsh_runtime.run_bootstrap(*setup, Path(os.environ.get('PW_DSH_TEST_RUNTIME', 'synthetic-runtime')))


def test_default_materials_publish_despite_one_invalid_and_replay_without_extraction(setup, monkeypatch):
    calls, roles = install_provider(setup, monkeypatch)
    result = run(setup)
    pipeline, execution = setup
    assert result['records'] == 2 and result['status'] == 'partial'
    assert len(roles) == 3 and len(calls) == 4  # three extraction, one existing semantic admission
    events = pipeline.store.list_events(project_id=execution.job['project_id'])
    assert {e['category'] for e in events} == {'industry', 'competitor'}
    with closing(pipeline.store._connect()) as db:
        assert [r[0] for r in db.execute('SELECT status FROM studio_evidence_tasks ORDER BY rowid')] == ['rejected', 'complete', 'complete']
        for item in db.execute('SELECT extra FROM studio_items WHERE event_id IS NOT NULL'):
            [claim] = json.loads(item['extra'])['dsh_claims']
            assert claim['subject'] == 'Synthetic Host'
            [ref] = claim['refs']
            evidence = pipeline.evidence.get(execution.job['project_id'], ref['evidence_version_id'], db=db)
            assert ref['quote'] == evidence['parts'][ref['part_index']]['text'][ref['start']:ref['end']]
    restarted = JobExecution(pipeline.store, execution.job)
    pipeline._execution_state.current = restarted
    assert run((pipeline, restarted)) == result
    assert len(roles) == 3 and len(calls) == 4


def test_profile_failure_preserves_news_and_sources_and_retry_reuses_receipts(setup, monkeypatch):
    calls, roles = install_provider(setup, monkeypatch)
    pipeline, execution = setup
    saved = pipeline.store.put_compiled_profile
    monkeypatch.setattr(pipeline.store, 'put_compiled_profile', lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError('closeout failed')))
    with pytest.raises(RuntimeError, match='closeout failed'):
        run(setup)
    assert len(pipeline.store.list_events(project_id=execution.job['project_id'])) == 2
    assert discovered_sources(pipeline.store, execution.job['project_id'])['total'] == 3
    monkeypatch.setattr(pipeline.store, 'put_compiled_profile', saved)
    assert run(setup)['records'] == 2
    assert len(calls) == 4 and len(roles) == 3


def test_semantic_rejection_is_not_published(setup, monkeypatch):
    install_provider(setup, monkeypatch, failure='semantic')
    result = run(setup)
    assert result['records'] == 1 and len(result['record_admission']['rejected']) == 1
    assert len(setup[0].store.list_events(project_id=setup[1].job['project_id'])) == 1


def test_oversize_candidate_is_reported_not_silently_published_with_empty_summary(setup, monkeypatch):
    install_provider(setup, monkeypatch, failure='oversize')
    result = run(setup)
    assert result['records'] == 1 and result['status'] == 'partial'
    pipeline, execution = setup
    assert len(execution.read_checkpoint('material_mapping_v2')['issues']) == 2
    [event] = pipeline.store.list_events(project_id=execution.job['project_id'])
    assert event['category'] == 'competitor' and event['summary'] == 'Synthetic abstract'


@pytest.mark.parametrize('failure', ['unknown', 'budget', 'lease'])
def test_default_dispatch_protections(setup, monkeypatch, failure):
    calls, _ = install_provider(setup, monkeypatch, failure=failure)
    pipeline, execution = setup
    if failure == 'budget':
        execution.budgets['model'] = 0
    if failure == 'lease':
        assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    expected = 'request_outcome_unknown' if failure == 'unknown' else 'request_budget' if failure == 'budget' else 'lease expired'
    for _ in range(2):
        with pytest.raises((RuntimeError, HttpError), match=expected):
            run(setup)
    assert len(calls) == (1 if failure == 'unknown' else 0)


@pytest.mark.parametrize('stage', ['dsh_binding', 'dsh_state', 'dsh_runtime', 'collection_admission_v1', 'recovery_origin'])
def test_started_and_recovery_jobs_freeze_legacy_before_research_construction(setup, stage):
    _, execution = setup
    execution.checkpoint(stage, {})
    owner = progressive_bootstrap.freeze(execution, 'bootstrap')
    assert owner['owner'] == 'legacy'
    assert progressive_bootstrap.freeze(execution, 'bootstrap') == owner


def test_monitor_stays_legacy_and_cannot_change_bootstrap_owner(setup, monkeypatch):
    pipeline, execution = setup
    pipeline.http_factory = lambda _: Http(lambda _m, url, *_a: fixtures.response(url))
    seen = []
    monkeypatch.setattr(dsh_runtime, 'run_dsh', lambda research, *_: seen.append(research.tools))
    dsh_runtime.run_monitor(pipeline, execution, None)
    assert 'pw_record' in seen[0]
    assert execution.read_checkpoint(progressive_bootstrap.OWNER)['owner'] == 'legacy'
    with pytest.raises(ValueError, match='pipeline mismatch'):
        run(setup)


def test_new_material_owner_cannot_collect_around_another_jobs_unknown(setup):
    pipeline, previous = setup
    def lost():
        raise HttpError('connection_failed')
    with pytest.raises(HttpError, match='connection_failed'):
        previous.call('model', 'old-unresolved', lost, provider='synthetic')
    assert pipeline.store.finish(previous.job['id'], lease_token=previous.job['lease_token'])
    pipeline.store.enqueue(previous.job['project_id'], 'external')
    current = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline._execution_state.current = current
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('no collection or model around old unknown'))
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        run((pipeline, current))
    assert previous.usage()['model']['unknown'] == 1 and not current.usage()


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_native_default_bootstrap_runs_only_material_roles(setup, monkeypatch):
    calls, roles = install_provider(setup, monkeypatch, native=True)
    assert run(setup)['records'] == 2
    assert len(roles) == 3 and len(calls) == 4
