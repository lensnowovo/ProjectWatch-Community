"""Default bootstrap collection roles: synthetic providers and isolated SQLite."""
import json
import os
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import test_collection_host as fixtures
from test_collection_pages import study
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import bootstrap_collection, dsh_runtime
from project_watch.studio.collection_view import discovered_sources
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import SearchConfig

BODY = 'Synthetic Host develops SYN-ALPHA. Sponsor: Synthetic Hospital.'
LATE = 'Supported detail from the later article section.'
URL = 'https://public.example.invalid/overview'


def install(tmp_path, monkeypatch, *, mode='success', native=False, source_count=1):
    if native and not os.environ.get('PW_DSH_TEST_RUNTIME'):
        pytest.skip('requires explicitly verified native DSH runtime')
    pipeline, execution = fixtures.setup.__wrapped__(tmp_path)
    settings = pipeline.settings_store.load()
    settings.search = SearchConfig('tavily', 'synthetic')
    pipeline.settings_store.save(settings)
    seen = {'pages': [], 'models': [], 'clinical': [], 'materials': [], 'visible': []}
    active = {}
    original = dsh_runtime.run_dsh

    def send(_method, url, _headers, raw, _timeout):
        host = urlsplit(url).hostname
        if host == 'api.tavily.com':
            data = {'results': [{'title': 'Synthetic public source', 'url': URL + (str(i) if i else ''),
                                 'content': 'Saved short public summary'} for i in range(source_count)]}
        elif host == 'api.openalex.org':
            data = {'results': [], 'meta': {'count': 0}}
        elif host == 'www.cninfo.com.cn':
            data = {'announcements': [], 'totalAnnouncement': 0}
        elif host == 'clinicaltrials.gov':
            seen['clinical'].append(parse_qs(urlsplit(url).query))
            if mode == 'query_failed':
                return Response(503, {}, b'{}')
            data = {'studies': [] if mode == 'no_match' else [study()]}
        else:
            assert host == 'model.example.invalid'
            body = json.loads(raw)
            seen['models'].append(body)
            if mode == 'model_unknown':
                raise HttpError('connection_failed')
            if 'tools' not in body:
                review = json.loads(body['messages'][-1]['content'])
                message = {'role': 'assistant', 'content': json.dumps(synthetic_review(review))}
                finish = 'stop'
            else:
                tools = {tool['function']['name'] for tool in body['tools']}
                if 'pw_collect' in tools:
                    assert tools == set(bootstrap_collection.TOOLS)
                    assert not pipeline.store.list_events(project_id=execution.job['project_id'])
                    assert pipeline.monitoring_plan.items(execution.job['project_id']) == []
                    prior = [m for m in body['messages'] if m['role'] == 'tool']
                    if mode == 'turn_limit':
                        name, args = 'pw_status', {}
                    elif mode == 'late_scope' and not prior:
                        page = next(s for s in json.loads(active['task'])['sources'] if s['url'] == URL)
                        name, args = 'pw_read', dict(source_id=page['id'], view='page', offset=20500)
                    elif mode == 'late_scope' and len(prior) == 1:
                        page = json.loads(prior[0]['content'])
                        assert BODY in page['excerpt'] and page['window_offset'] == 20500
                        name, args = 'pw_collect', dict(adapter='clinical_term', query='SYN-ALPHA',
                            source_id=page['id'], quote=BODY, reason='The later body names the intervention')
                    elif not prior and mode not in ('scope_missing', 'read_failed', 'page_budget'):
                        page = next(s for s in json.loads(active['task'])['sources'] if s['url'] == URL)
                        name, args = 'pw_collect', dict(adapter='clinical_term', query='SYN-ALPHA',
                            source_id=page['id'], quote=BODY, reason='The public source names the intervention')
                    else:
                        name, args = 'pw_finish', {'gaps': {'company': 'Bounded synthetic collection'}}
                else:
                    assert tools == {'pw_submit_records'}
                    source = json.loads(active['task'])
                    seen['materials'].append(source)
                    pair = next(((ref, part, LATE) for ref, part in source['refs'].items() if LATE in part['quote']), None)
                    if pair is None:
                        pair = next(((ref, part, 'Synthetic Hospital') for ref, part in source['refs'].items()
                                     if 'Synthetic Hospital' in part['quote']), None)
                    if pair:
                        ref, part, text = pair
                        start = part['quote'].index(text)
                        observations = [dict(kind='industry', subject='Synthetic Host', label='Public source finding',
                            text=text, refs=[ref], start=start, end=start + len(text))]
                    else:
                        observations = []
                    name, args = 'pw_submit_records', {'observations': observations}
                message = {'role': 'assistant', 'tool_calls': [{'id': str(len(seen['models'])), 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}]}
                finish = 'tool_calls'
            data = {'choices': [{'message': message, 'finish_reason': finish}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}
        return Response(200, {}, json.dumps(data).encode())

    def read(url):
        seen['pages'].append(url)
        visible = discovered_sources(pipeline.store, execution.job['project_id'])
        seen['visible'].append(visible)
        assert visible['total'] >= 1 and not seen['materials']
        if mode == 'page_unknown':
            raise HttpError('connection_failed')
        if mode == 'read_failed' or not url.startswith(URL):
            return {'status': 'unavailable', 'text': '', 'error_code': 'http_failure'}
        text = ('a' * 20500 + BODY + '\n' + LATE) if mode == 'late_scope' else BODY + '\n' + 'a' * 20500 + '\n' + LATE
        return {'status': 'article_body', 'text': text}

    def turn(research, gateway, runtime, directory, **kwargs):
        active['task'] = research.task
        if native:
            return original(research, gateway, runtime, directory, **kwargs)
        messages = [{'role': 'user', 'content': research.task}]
        for _ in range(8):
            reply = gateway.model_call({'model': gateway.model.model, 'messages': messages,
                'tools': [{'function': {'name': name}} for name in research.tools]})
            message = reply['choices'][0]['message']
            messages.append(message)
            for call in message.get('tool_calls') or []:
                function = call['function']
                result = research.tool(function['name'], json.loads(function['arguments']))
                messages.append({'role': 'tool', 'name': function['name'], 'tool_call_id': call['id'],
                                 'content': json.dumps(result, ensure_ascii=False)})
            if research.state['finished']:
                return research.state['finished']
        pytest.fail('bounded role did not stop')

    pipeline.http_factory = lambda _: Http(send)
    monkeypatch.setattr(pipeline, 'reader', lambda: SimpleNamespace(read=read))
    monkeypatch.setattr(dsh_runtime, 'run_dsh', turn)
    monkeypatch.setenv('PROJECT_WATCH_DSH_MONITOR', '0')
    monkeypatch.setenv('PROJECT_WATCH_DSH_RUNTIME', os.environ.get('PW_DSH_TEST_RUNTIME', 'synthetic-runtime'))
    return pipeline, execution, seen


def run(pipeline, execution):
    pipeline.run_external(execution.job)
    return execution.read_checkpoint('dsh_state')['finished']


def test_default_reads_visible_summary_then_extracts_only_selected_body_and_collects_scope(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    result = run(pipeline, execution)
    assert result['records'] == 2 and len(seen['materials']) == 2
    assert seen['visible'][0]['items'][0]['source_type'] == 'search_summary'
    assert seen['visible'][0]['items'][0]['body_evidence_version_id'] is None
    assert len(seen['clinical']) == 1 and seen['clinical'][0]['query.term'] == ['SYN-ALPHA']
    assert 'query.spons' not in seen['clinical'][0]
    body = next(s for s in seen['materials'] if s['source_type'] == 'public_page')
    assert body['read_scope'] == 'partial' and any(LATE in p['quote'] for p in body['refs'].values())
    visible = discovered_sources(pipeline.store, execution.job['project_id'])
    article = next(i for i in visible['items'] if i['url'] == URL)
    assert article['body_evidence_version_id'] == body['evidence_version_id']
    with closing(pipeline.store._connect()) as db:
        tasks = [dict(t) for t in db.execute('SELECT * FROM studio_evidence_tasks ORDER BY rowid')]
    superseded = [t for t in tasks if json.loads(t['result']).get('disposition') == 'superseded_before_extraction']
    assert len(superseded) == 1 and not execution.read_checkpoint('extract_attempt:' + superseded[0]['id'])
    assert pipeline.store.list_items(project_id=execution.job['project_id'], category='trial')
    before = {k: len(v) for k, v in seen.items()}
    assert run(pipeline, execution) == result and before == {k: len(v) for k, v in seen.items()}


@pytest.mark.parametrize('mode,outcome,count', [('scope_missing', 'needs_scope', None),
                                               ('no_match', 'success', 0), ('query_failed', 'failed', None)])
def test_scope_missing_real_zero_and_failure_remain_distinct(tmp_path, monkeypatch, mode, outcome, count):
    pipeline, execution, _ = install(tmp_path, monkeypatch, mode=mode)
    result = run(pipeline, execution)
    rows = [r for r in result['collection']['rows'] if r['source'] ==
            ('clinical_sponsor' if mode == 'scope_missing' else 'clinical_term')]
    assert any(r['outcome'] == outcome and r['result_count'] == count for r in rows)
    assert result['status'] == 'partial'


@pytest.mark.parametrize('mode', ['read_failed', 'page_budget'])
def test_known_read_failure_or_preflight_budget_keeps_summary_without_page_replay(tmp_path, monkeypatch, mode):
    pipeline, execution, seen = install(tmp_path, monkeypatch, mode=mode)
    if mode == 'page_budget':
        execution.budgets['page'] = 0
    result = run(pipeline, execution)
    assert result['status'] == 'partial'
    assert seen['materials'][0]['source_type'] == 'search_summary'
    assert len(seen['pages']) == (0 if mode == 'page_budget' else 1)
    state = execution.read_checkpoint(bootstrap_collection.STAGE)
    if mode == 'page_budget':
        assert any(p['reason'] == 'page_budget_reserved' for p in state['pending'])
    assert not execution.read_checkpoint('dsh_hold')
    before = len(seen['models'])
    run(pipeline, execution)
    assert len(seen['models']) == before


@pytest.mark.parametrize('mode', ['page_unknown', 'model_unknown'])
def test_collection_unknown_stops_without_retry_or_extraction(tmp_path, monkeypatch, mode):
    pipeline, execution, seen = install(tmp_path, monkeypatch, mode=mode)
    for _ in range(2):
        with pytest.raises(HttpError, match=r'request_outcome_unknown|connection_failed'):
            run(pipeline, execution)
    assert not seen['materials']
    assert len(seen['pages']) == 1
    assert len(seen['models']) == (1 if mode == 'model_unknown' else 0)


def test_four_turn_collection_bound_leaves_material_publication_budget(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch, mode='turn_limit')
    execution.budgets['model'] = 7
    result = run(pipeline, execution)
    assert result['records'] == 1 and execution.budget_used('model') == 6
    collection = execution.read_checkpoint(bootstrap_collection.STAGE)
    assert collection['finished']['reason'] == 'bounded_stop'
    assert len(execution.read_checkpoint('bootstrap_collection_attempts_v1')) == 4
    assert len(seen['materials']) == 1


@pytest.mark.parametrize('boundary', ['page_selection', 'scope_retained'])
def test_collection_storage_crash_reuses_page_and_model_receipts(tmp_path, monkeypatch, boundary):
    from project_watch.studio import collection_dsh, evidence_tasks

    pipeline, execution, seen = install(tmp_path, monkeypatch)
    if boundary == 'page_selection':
        original = evidence_tasks.admit
        def fail(db, current, evidence_id):
            evidence = pipeline.evidence.get(current.job['project_id'], evidence_id, db=db)
            if evidence['source_type'] == 'public_page':
                raise RuntimeError('selection checkpoint crash')
            return original(db, current, evidence_id)
        monkeypatch.setattr(evidence_tasks, 'admit', fail)
    else:
        original = collection_dsh.search
        def fail(*args, **kwargs):
            if kwargs.get('adapter') == 'clinical_term':
                raise RuntimeError('selection checkpoint crash')
            return original(*args, **kwargs)
        monkeypatch.setattr(collection_dsh, 'search', fail)
    with pytest.raises(RuntimeError, match='selection checkpoint crash'):
        run(pipeline, execution)
    assert not seen['materials'] and seen['pages'] == [URL]
    if boundary == 'page_selection':
        monkeypatch.setattr(evidence_tasks, 'admit', original)
    else:
        monkeypatch.setattr(collection_dsh, 'search', original)
    assert run(pipeline, execution)['records'] == 2
    assert seen['pages'].count(URL) == 1 and len(seen['clinical']) == 1
    assert len(seen['models']) == 5  # two collection, two extraction, one semantic admission


@pytest.mark.skipif(not os.environ.get('PW_DSH_TEST_RUNTIME'), reason='requires pinned official DSH')
def test_native_collection_role_reads_collects_and_finishes_without_record_tools(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch, native=True)
    assert run(pipeline, execution)['records'] == 2
    assert len(seen['clinical']) == 1 and len(seen['materials']) == 2
    runtime = execution.read_checkpoint('bootstrap_collection_runtime_v1')
    assert set(runtime['advertised_tools']) == set(bootstrap_collection.TOOLS)


def test_collection_role_can_read_and_cite_later_body_window(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch, mode='late_scope')
    assert run(pipeline, execution)['records'] == 2
    with closing(pipeline.store._connect()) as db:
        target = json.loads(db.execute('SELECT record FROM studio_collection_targets').fetchone()[0])
    body = next(s for s in seen['materials'] if s['source_type'] == 'public_page')
    assert target['ref']['part_index'] == 1 and target['ref']['start'] == 500
    assert target['ref']['evidence_version_id'] == body['evidence_version_id']
    assert seen['pages'].count(URL) == 1


def test_fourth_page_selection_crash_resumes_existing_action_at_page_limit(tmp_path, monkeypatch):
    from project_watch.studio import evidence_tasks

    pipeline, execution, seen = install(tmp_path, monkeypatch, mode='scope_missing', source_count=4)
    original = evidence_tasks.admit
    def fail(db, current, evidence_id):
        evidence = pipeline.evidence.get(current.job['project_id'], evidence_id, db=db)
        if evidence['source_type'] == 'public_page' and len(seen['pages']) == 4:
            raise RuntimeError('fourth page selection crash')
        return original(db, current, evidence_id)
    monkeypatch.setattr(evidence_tasks, 'admit', fail)
    with pytest.raises(RuntimeError, match='fourth page selection crash'):
        run(pipeline, execution)
    assert len(seen['pages']) == 4 and not seen['materials']
    monkeypatch.setattr(evidence_tasks, 'admit', original)
    assert run(pipeline, execution)['records'] == 4
    assert len(seen['pages']) == 4 and len(seen['materials']) == 4
    actions = execution.read_checkpoint(bootstrap_collection.STAGE)['actions']
    assert len(actions) == 4 and all(a['status'] == 'complete' for a in actions.values())


def test_deferred_body_material_transfers_exact_source_without_old_round_or_recollection(tmp_path, monkeypatch):
    from project_watch.studio import bootstrap_continuation
    from project_watch.studio.execution import JobExecution

    pipeline, execution, seen = install(tmp_path, monkeypatch, mode='scope_missing', source_count=2)
    execution.budgets['model'] = 3
    result = run(pipeline, execution)
    [task_id] = result['material_work']['pending_task_ids']
    source = execution.read_checkpoint(bootstrap_collection.SOURCES + ':' + task_id)
    assert source and result['records'] == 1
    assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    pipeline.store.enqueue(execution.job['project_id'], 'external')
    current = JobExecution(pipeline.store, pipeline.store.claim(), budgets={'model': 5})
    pipeline._execution_state.current = current
    bootstrap_continuation.admit(current, execution.job['id'])
    before = len(seen['pages']), len(seen['clinical'])
    published = run(pipeline, current)
    assert published['records'] == 1
    assert current.read_checkpoint(bootstrap_collection.SOURCES + ':' + task_id) == source
    assert (len(seen['pages']), len(seen['clinical'])) == before
    assert current.read_checkpoint('collection_round_v1')['plan']['units'] == []
    [record] = current.read_checkpoint('dsh_state')['records']
    assert record['refs'][0]['evidence_version_id'] == source['material_body_evidence_id']
    assert len(seen['materials']) == 2


@pytest.mark.parametrize('mode,source_count', [('page_budget', 1), ('scope_missing', 5)])
def test_explicit_successor_worker_consumes_unissued_pages_without_repeating_collection(tmp_path, monkeypatch, mode, source_count):
    from project_watch.studio import bootstrap_continuation
    from project_watch.studio.execution import JobExecution

    pipeline, execution, seen = install(tmp_path, monkeypatch, mode=mode, source_count=source_count)
    if mode == 'page_budget':
        execution.budgets['page'] = 0
    run(pipeline, execution)
    pending = execution.read_checkpoint(bootstrap_collection.STAGE)['pending']
    assert len(pending) == 1 and pending[0]['tool'] == 'pw_read'
    before_pages, before_models = len(seen['pages']), len(seen['models'])
    seen['materials'].clear()
    assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    pipeline.store.enqueue(execution.job['project_id'], 'external_followup', input_version=execution.job['id'])
    current = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline._execution_state.current = current
    receipt = bootstrap_continuation.admit(current, execution.job['id'])
    assert bootstrap_continuation.admit(current, execution.job['id']) == receipt
    inherited = current.budget_used('model')
    assert inherited == execution.budget_used('model')
    pipeline.run_external_followup(current.job)
    result = current.read_checkpoint('dsh_state')['finished']
    assert result['records'] == 1
    assert len(seen['pages']) == before_pages + 1 and len(seen['models']) == before_models + 2
    assert current.budget_used('model') == inherited + 2
    assert not any('pw_collect' in {t['function']['name'] for t in b.get('tools', [])}
                   for b in seen['models'][before_models:])
    before = {k: len(v) for k, v in seen.items()}
    pipeline.run_external_followup(current.job)
    assert before == {k: len(v) for k, v in seen.items()}


@pytest.mark.parametrize('boundary', ['before_input', 'frozen_input'])
def test_withdrawn_body_never_enters_new_or_replayed_collection_model_input(tmp_path, monkeypatch, boundary):
    pipeline, execution, seen = install(tmp_path, monkeypatch, mode='scope_missing')
    withdrawn = []
    def withdraw(role):
        if not withdrawn:
            with closing(pipeline.store._connect()) as db:
                version = db.execute('SELECT evidence_version_id FROM studio_item_evidence').fetchone()[0]
            pipeline.evidence.dependencies.withdraw(execution.job['project_id'], version, 'synthetic withdrawal', actor='test')
            withdrawn.append(version)
    if boundary == 'before_input':
        original = bootstrap_collection.Collection.read_pages
        def read(role, **kwargs):
            result = original(role, **kwargs)
            withdraw(role)
            return result
        monkeypatch.setattr(bootstrap_collection.Collection, 'read_pages', read)
        assert run(pipeline, execution)['records'] == 0
        assert json.loads(execution.read_checkpoint('bootstrap_collection_input_v1'))['sources'] == []
        assert len(seen['models']) == 1 and not seen['materials']
    else:
        original = bootstrap_collection.Collection.guard_input
        def guard(role):
            withdraw(role)
            return original(role)
        monkeypatch.setattr(bootstrap_collection.Collection, 'guard_input', guard)
        for _ in range(2):
            with pytest.raises(HttpError, match='source_withdrawn'):
                run(pipeline, execution)
        assert len(seen['models']) == 0 and not seen['materials']
        assert seen['pages'] == [URL]


@pytest.mark.parametrize('guard', ['unknown', 'hold'])
def test_explicit_continuation_admission_cannot_bypass_project_protections(tmp_path, monkeypatch, guard):
    from project_watch.studio import bootstrap_continuation
    from project_watch.studio.execution import JobExecution

    pipeline, execution, seen = install(tmp_path, monkeypatch, mode='page_budget')
    execution.budgets['page'] = 0
    run(pipeline, execution)
    assert pipeline.store.finish(execution.job['id'], lease_token=execution.job['lease_token'])
    pipeline.store.enqueue(execution.job['project_id'], 'external_followup', input_version=execution.job['id'])
    current = JobExecution(pipeline.store, pipeline.store.claim())
    if guard == 'hold':
        current.checkpoint('dsh_hold', {'code': 'request_budget'})
    else:
        def uncertain():
            raise HttpError('connection_failed')
        with pytest.raises(HttpError):
            current.call('page', 'synthetic-uncertain', uncertain, provider='synthetic')
    before = {k: len(v) for k, v in seen.items()}
    with pytest.raises(HttpError, match=r'request_outcome_unknown|request_budget'):
        bootstrap_continuation.admit(current, execution.job['id'])
    assert current.read_checkpoint(bootstrap_continuation.STAGE) is None
    assert before == {k: len(v) for k, v in seen.items()}


def test_new_tool_evidence_withdrawal_blocks_next_model_request(tmp_path, monkeypatch):
    pipeline, execution, seen = install(tmp_path, monkeypatch)
    original = bootstrap_collection.Collection.expose
    withdrawn = []
    def expose(role, result):
        result = original(role, result)
        if result.get('query_scope') and not withdrawn:
            [source] = result['sources']
            version = source['evidence_version_id']
            assert version not in {s['evidence_version_id'] for s in json.loads(role.task)['sources']}
            pipeline.evidence.dependencies.withdraw(execution.job['project_id'], version,
                                                   'synthetic late withdrawal', actor='test')
            withdrawn.append(version)
        return result
    monkeypatch.setattr(bootstrap_collection.Collection, 'expose', expose)
    with pytest.raises(HttpError, match='source_withdrawn'):
        run(pipeline, execution)
    assert withdrawn and len(seen['models']) == 1 and len(seen['clinical']) == 1
    assert not seen['materials']
    assert any(s['evidence_version_id'] == withdrawn[0] for s in execution.read_checkpoint('collection_exposed_v1'))
