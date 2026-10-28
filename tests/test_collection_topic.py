"""Selected-topic admission, collection and reporting use isolated synthetic data."""
import json
import os
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import test_collection_host as fixtures
from fastapi.testclient import TestClient

from project_watch.studio import collection_host, dsh_runtime
from project_watch.studio.collection_ledger import STAGE
from project_watch.studio.dsh_topic import enqueue_topic, latest_topic
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.settings import SearchConfig


@pytest.fixture
def setup(tmp_path):
    pipeline, old = fixtures.setup.__wrapped__(tmp_path)
    assert pipeline.store.finish(old.job['id'], lease_token=old.job['lease_token'])
    settings = pipeline.settings_store.load()
    settings.search = SearchConfig(providers=['tavily'], keys={'tavily': 'synthetic'}, google_news=False)
    pipeline.settings_store.save(settings)
    pid = old.job['project_id']
    selected = pipeline.monitoring_plan.add(pid, dict(text='激光模组是否取得许可', objects=['激光模组'],
        axis='asset', queries=['激光模组 许可'], tier='topic', exit_rule='公开原文确认许可', cadence='medium'),
        actor='system', origin='system', visibility='public')
    other = pipeline.monitoring_plan.add(pid, dict(text='其他产品是否投产', queries=['其他产品 投产'],
        tier='topic', exit_rule='公开原文确认投产'), actor='system', origin='system', visibility='public')
    relation = pipeline.monitoring_plan.add(pid, dict(text='Synthetic supplier', queries=['Synthetic supplier'],
        reason='Public synthetic relation', sources=['https://example.org/supplier'],
        scope_kind='relation', relation_type='supplier', relation_target='Synthetic supplier', axis='partners'),
        actor='human', origin='human', visibility='public')
    return pipeline, pid, selected, other, relation


def start(setup):
    pipeline, pid, watch, _, _ = setup
    job = enqueue_topic(pipeline, pid, watch['id'], watch['lifecycle_version'])
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    assert execution.job['id'] == job['id']
    # These tests preserve the previously-started topic protocol and its wire.
    # Fresh Pipeline.run material/assessment coverage lives in test_question_assessment.
    from project_watch.studio.progressive_bootstrap import freeze
    freeze(execution, 'monitor', materials=False)
    pipeline._execution_state.current = execution
    return execution


def test_admission_is_atomic_idempotent_and_does_not_expand_active_job(setup):
    pipeline, pid, selected, other, _ = setup
    first = enqueue_topic(pipeline, pid, selected['id'], selected['lifecycle_version'])
    assert enqueue_topic(pipeline, pid, selected['id'], selected['lifecycle_version'])['id'] == first['id']
    with pytest.raises(ValueError, match='已有公开研究任务'):
        enqueue_topic(pipeline, pid, other['id'], other['lifecycle_version'])
    assert pipeline.store.enqueue(pid, 'external')['id'] == first['id']
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    request = execution.read_checkpoint('collection_request_v1')
    assert request['mode'] == 'topic' and request['question']['id'] == selected['id']
    assert other['id'] not in pipeline.monitoring_plan.explorations.view(pid)


@pytest.mark.parametrize('change', ['paused', 'agent', 'version', 'retired', 'internal', 'relation', 'no_exit', 'no_queries'])
def test_invalid_scope_never_enqueues_or_wakes(setup, change):
    pipeline, pid, watch, _, relation = setup
    with closing(pipeline.store._connect()) as db, db:
        if change in ('paused', 'agent'):
            column = 'status' if change == 'paused' else 'execution_mode'
            db.execute(f'UPDATE studio_projects SET {column}=? WHERE project_id=?', (change, pid))
        elif change in ('retired', 'internal', 'no_exit', 'no_queries'):
            field, value = {'retired': ('status', 'retired'), 'internal': ('visibility', 'internal'),
                            'no_exit': ('exit_rule', ''), 'no_queries': ('queries', '[]')}[change]
            db.execute(f'UPDATE studio_watchpoints SET {field}=? WHERE id=?', (value, watch['id']))
    if change == 'relation':
        watch = relation
    with pytest.raises(ValueError):
        enqueue_topic(pipeline, pid, watch['id'], 'wrong' if change == 'version' else watch['lifecycle_version'])
    assert not pipeline.monitoring_plan.explorations.view(pid)
    assert len(pipeline.store.jobs(pid)) == 1  # completed fixture job only


def test_project_mismatch_and_existing_full_round_cannot_be_rebound(setup):
    pipeline, pid, watch, _, _ = setup
    with pytest.raises(ValueError):
        enqueue_topic(pipeline, 'other-project', watch['id'], watch['lifecycle_version'])
    job = pipeline.store.enqueue(pid, 'external', automatic=True)
    with pytest.raises(ValueError, match='已有公开研究任务'):
        enqueue_topic(pipeline, pid, watch['id'], watch['lifecycle_version'])
    assert JobExecution(pipeline.store, pipeline.store.claim()).read_checkpoint('collection_request_v1') == dict(
        schema_version=1, mode='incremental', origin='scheduler')
    assert not pipeline.monitoring_plan.explorations.view(pid)
    assert job['id']


def test_unknown_request_cannot_be_bypassed_by_topic_admission(setup):
    pipeline, pid, watch, _, _ = setup
    pipeline.store.enqueue(pid, 'external')
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    def lost():
        raise TimeoutError('synthetic unknown')
    with pytest.raises(TimeoutError):
        execution.call('search', 'synthetic', lost)
    with pytest.raises(ValueError, match='原请求'):
        enqueue_topic(pipeline, pid, watch['id'], watch['lifecycle_version'])
    assert execution.usage()['search']['unknown'] == 1
    assert not pipeline.monitoring_plan.explorations.view(pid)


def test_owner_wakes_only_selected_question_and_duplicate_does_not_reset_generation(setup):
    pipeline, pid, watch, other, _ = setup
    engine = pipeline.monitoring_plan.explorations
    engine.refresh(pid)
    for question in (watch, other):
        engine.stop(question, 'no_gain')
    before = engine.view(pid)
    enqueue_topic(pipeline, pid, watch['id'], watch['lifecycle_version'])
    after = engine.view(pid)
    assert after[watch['id']]['generation'] == before[watch['id']]['generation'] + 1
    assert after[watch['id']]['state'] == 'active' and after[other['id']] == before[other['id']]
    enqueue_topic(pipeline, pid, watch['id'], watch['lifecycle_version'])
    assert engine.view(pid) == after


@pytest.mark.parametrize('with_evidence', [False, True])
def test_topic_runtime_collects_only_selected_question_and_preserves_project_reports(setup, monkeypatch, with_evidence):
    pipeline, pid, watch, other, relation = setup
    execution = start(setup)
    before_project = pipeline.store.get_project(pid)
    before_watches = {w['id']: w for w in pipeline.monitoring_plan.items(pid)}
    pipeline.store.set_state('dsh-monitor:' + pid, 'synthetic previous global result')
    pipeline.store.put_plan(pid, {'last_report': {'company': {'checked': 7}}, 'collection_phase': 'complete'})
    before_plan = pipeline.store.get_plan(pid)
    calls = []
    quote = '合成公司激光模组取得许可，量产仍在计划中。'
    def send(method, url, headers, body, timeout):
        calls.append(json.loads(body)['query'])
        assert calls[-1] == watch['queries'][0]
        rows = [dict(title='激光模组取得许可', content=quote, url='https://example.org/laser', score=1)] if with_evidence else []
        return Response(200, {}, json.dumps(dict(results=rows)).encode())
    pipeline.http_factory = lambda _: Http(send)

    def model(research, *_):
        assert research.topic_projection
        from test_studio_dsh_monitor import synthetic_review
        research.judgment_reviewer = synthetic_review
        research.record_reviewer = synthetic_review
        ctx = research.tool('pw_context', {})
        assert [w['id'] for w in ctx['watches']] == [watch['id']]
        assert ctx['due_ids'] == [watch['id']]
        units = execution.read_checkpoint(STAGE)['plan']['units']
        assert len(units) == 1 and units[0]['target_id'] == 'watch:' + watch['id']
        assert execution.read_checkpoint(collection_host.SCOPE)['source_queries'] == []
        with pytest.raises(ValueError, match='outside selected'):
            research.tool('pw_explore', dict(watch_id=other['id'], query=other['queries'][0]))
        with pytest.raises(ValueError, match='company scope'):
            research.tool('pw_record', dict(kind='business'))
        refs = []
        if with_evidence:
            sid = ctx['collected_sources']['sources'][0]['id']
            research.tool('pw_read', dict(source_id=sid))
            research.tool('pw_record', dict(kind='company', label='激光模组许可', source_id=sid, quote=quote, text=quote))
            ctx = research.tool('pw_context', {})
            refs = ctx['watch_evidence'][watch['id']]
            assert refs
            for pending in ctx['required_assessments']:
                research.tool('pw_assess', dict(watch_id=watch['id'], refs=pending['refs'], status='unchanged',
                    change_basis='evidence_only', note=quote))
        result = research.tool('pw_judge', dict(analysis_json=json.dumps(dict(watch_id=watch['id'],
            status='answered' if with_evidence else 'insufficient', text=quote if with_evidence else '',
            refs=refs, gaps=[] if with_evidence else ['许可状态仍待原文确认']))))
        assert result['saved']
        with pytest.raises(ValueError, match='cannot expand'):
            research.tool('pw_revise', dict(changes_json=json.dumps([dict(watch_id=watch['id'], action='explore')]), reason='test'))
        research.tool('pw_revise', dict(changes_json='[]', reason='保留长期监控范围'))
        return research.tool('pw_finish', dict(reason='所选问题本轮结束'))

    monkeypatch.setattr(dsh_runtime, 'run_dsh', model)
    result = dsh_runtime.run_monitor(pipeline, execution, None)
    assert result['mode'] == 'topic' and result['collection']['status'] == 'complete'
    assert result['status'] == ('complete' if with_evidence else 'partial')
    assert calls == watch['queries']
    view = latest_topic(pipeline.store, pid, watch['id'])
    assert view['result']['answer_status'] == ('answered' if with_evidence else 'insufficient')
    assert view['result']['collection_status'] == 'complete'
    assert view['scope_changed'] is False
    assert len(view['result']['sources']) == int(with_evidence)
    if with_evidence:
        assert view['result']['sources'][0]['url'] == 'https://example.org/laser'
        assert view['result']['sources'][0]['context']
    assert latest_topic(pipeline.store, pid, other['id'])['job'] is None
    assert pipeline.store.get_project(pid)['last_external_at'] == before_project['last_external_at']
    assert pipeline.store.get_state('dsh-monitor:' + pid) == 'synthetic previous global result'
    assert pipeline.store.get_plan(pid) == before_plan
    assert pipeline.store.get_public_profile(pid) is None
    after = {w['id']: w for w in pipeline.monitoring_plan.items(pid)}
    assert after[other['id']] == before_watches[other['id']] and after[relation['id']] == before_watches[relation['id']]
    assert other['id'] not in pipeline.monitoring_plan.explorations.view(pid)
    # Durable completion can be read after a resumed invocation, with no calls.
    assert dsh_runtime.run_monitor(pipeline, execution, None) == result
    assert calls == watch['queries']


def test_scope_edit_stops_before_any_provider_and_topic_never_falls_back(setup, monkeypatch):
    pipeline, _pid, watch, _, _ = setup
    execution = start(setup)
    pipeline.monitoring_plan.update(watch['id'], {'text': '用户改了问题'}, actor='human')
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('changed scope must not dispatch'))
    with pytest.raises(ValueError, match='question changed'):
        dsh_runtime.run_monitor(pipeline, execution, None)
    monkeypatch.delenv('PROJECT_WATCH_DSH_RUNTIME', raising=False)
    with pytest.raises(ValueError, match='requires DSH'):
        pipeline.run_external(execution.job)
    assert not execution.usage()


def test_topic_api_owner_only_and_dsh_gate(setup, monkeypatch):
    pipeline, pid, watch, _, _ = setup
    # create_studio_app stores its application on the fixture pipeline indirectly;
    # use a separate synthetic API fixture below rather than opening any service.
    from project_watch.studio.app import create_studio_app
    app = create_studio_app(pipeline.settings_store.path.parent, start_worker=False)
    client = TestClient(app)
    url = f'/api/studio/projects/{pid}/research-topic'
    data = dict(watch_id=watch['id'], expected_version=watch['lifecycle_version'])
    monkeypatch.delenv('PROJECT_WATCH_DSH_RUNTIME', raising=False)
    assert client.post(url, json=data).status_code == 409
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', 'synthetic-runtime')
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '1')
    monkeypatch.setattr(dsh_runtime, 'runtime_entry', lambda _: Path(__file__))
    assert client.post(url, json=data, headers={'Authorization': 'Bearer synthetic-agent'}).status_code == 403
    result = client.post(url, json=data)
    assert result.status_code == 202, result.text
    assert result.json()['kind'] == 'external'
    before = pipeline.store.jobs(pid)
    read_url = url + '/' + watch['id']
    view = client.get(read_url)
    assert view.status_code == 200 and view.json()['job']['id'] == result.json()['id']
    assert view.json()['result'] is None
    assert client.get(read_url, headers={'Authorization': 'Bearer synthetic-agent'}).status_code == 403
    assert client.get(url + '/missing-question').status_code == 404
    assert pipeline.store.jobs(pid) == before


def test_read_result_is_project_bound_and_marks_changed_scope_without_waking(setup):
    pipeline, pid, watch, _, relation = setup
    watch = pipeline.monitoring_plan.update(watch['id'], {'parent_id': relation['id']}, actor='human')
    job = enqueue_topic(pipeline, pid, watch['id'], watch['lifecycle_version'])
    with pytest.raises(LookupError, match='不存在'):
        latest_topic(pipeline.store, 'another-project', watch['id'])
    pipeline.monitoring_plan.update(relation['id'], {'text': 'Changed parent'}, actor='human')
    before = pipeline.monitoring_plan.explorations.view(pid)
    for _ in range(2):
        view = latest_topic(pipeline.store, pid, watch['id'])
        assert view['scope_changed'] and view['result'] is None
        assert view['question']['text'] == watch['text']
        assert view['job']['id'] == job['id']
    assert pipeline.monitoring_plan.explorations.view(pid) == before
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_watchpoints SET visibility='internal' WHERE id=?", (watch['id'],))
    with pytest.raises(LookupError, match='不存在'):
        latest_topic(pipeline.store, pid, watch['id'])


def test_result_reads_chunked_evidence_and_reports_corruption_without_dispatch(setup):
    pipeline, pid, watch, _, _ = setup
    execution = start(setup)
    execution.checkpoint('dsh_state', {'finished': {'mode': 'topic', 'status': 'complete',
        'collection': {'status': 'complete'}, 'topic_judgment': {'status': 'answered', 'text': 'Synthetic answer',
            'refs': ['N1'], 'gaps': []}}})
    execution.checkpoint_chunked('dsh_monitor_context', {'numbered': {'N1': {'title': 'Synthetic source',
        'url': 'https://example.org/original', 'context': 'public synthetic excerpt ' * 5000}}})
    before = pipeline.store.jobs(pid)
    assert len(latest_topic(pipeline.store, pid, watch['id'])['result']['sources'][0]['context']) > 100000
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage LIKE 'cpchunk:%'",
                   (json.dumps('changed synthetic chunk'), execution.job['id']))
    with pytest.raises(ValueError, match='checksum mismatch'):
        latest_topic(pipeline.store, pid, watch['id'])
    from project_watch.studio.app import create_studio_app
    client = TestClient(create_studio_app(pipeline.settings_store.path.parent, start_worker=False))
    response = client.get(f'/api/studio/projects/{pid}/research-topic/{watch["id"]}')
    assert response.status_code == 409
    assert '无法核验' in response.json()['detail']
    assert not execution.usage() and pipeline.store.jobs(pid) == before


def test_parent_scope_is_bound_but_does_not_become_another_collection_target(setup, monkeypatch):
    pipeline, pid, watch, _, relation = setup
    watch = pipeline.monitoring_plan.update(watch['id'], {'parent_id': relation['id']}, actor='human')
    execution = start((pipeline, pid, watch, None, relation))
    assert execution.read_checkpoint('collection_request_v1')['parents'][0]['id'] == relation['id']
    pipeline.monitoring_plan.update(relation['id'], {'text': 'Changed parent scope'}, actor='human')
    pipeline.http_factory = lambda _: Http(lambda *_: pytest.fail('parent changed; no dispatch'))
    with pytest.raises(ValueError, match='parent changed'):
        dsh_runtime.run_monitor(pipeline, execution, None)
    assert not execution.usage()


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_pinned_dsh_can_finish_a_bounded_empty_topic_without_global_judgment(setup):
    pipeline, pid, watch, other, _ = setup
    execution = start(setup)
    calls, queries = [], []
    def send(_method, url, _headers, body, _timeout):
        if urlsplit(url).hostname == 'api.tavily.com':
            queries.append(json.loads(body)['query'])
            return Response(200, {}, b'{"results":[]}')
        assert urlsplit(url).hostname == 'model.example.invalid'
        payload = json.loads(body)
        if 'tools' not in payload:
            from test_studio_dsh_monitor import synthetic_review
            report = synthetic_review(json.loads(payload['messages'][-1]['content']))
            return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant',
                'content': json.dumps(report)}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())
        index = len(calls)
        if index == 1:
            context = json.loads(json.loads(body)['messages'][-1]['content'])
            assert context['mode'] == 'topic'
            assert [w['id'] for w in context['watches']] == [watch['id']]
            assert other['id'] not in json.dumps(context)
        operations = [('pw_context', {}), ('pw_judge', dict(analysis_json=json.dumps(dict(
            watch_id=watch['id'], status='insufficient', text='', refs=[], gaps=['许可信息尚待确认'])))),
            ('pw_revise', dict(changes_json='[]', reason='未取得可引用的新资料')),
            ('pw_finish', dict(reason='保留问题，结束本轮'))]
        assert index < len(operations)
        name, args = operations[index]
        calls.append(name)
        return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': f'topic-{index}', 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args)}}]},
            'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())
    pipeline.http_factory = lambda _: Http(send)
    result = dsh_runtime.run_monitor(pipeline, execution, Path(os.environ['PW_DSH_TEST_RUNTIME']))
    assert result['status'] == 'partial' and result['collection']['status'] == 'complete'
    assert result['topic_judgment']['status'] == 'insufficient'
    assert calls == ['pw_context', 'pw_judge', 'pw_revise', 'pw_finish']
    assert queries == watch['queries']
    assert execution.usage()['model']['completed'] == 5
    assert not pipeline.store.get_state('dsh-monitor:' + pid)
    assert pipeline.store.get_project(pid)['last_external_at'] is None
