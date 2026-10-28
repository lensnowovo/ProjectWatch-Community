"""DSH supplements share collection receipts, with synthetic providers only."""
import json
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy

import pytest
import test_collection_host as host_fixtures
from test_collection_plan import candidate, plan

from project_watch.studio import collection_dsh, collection_host
from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.collection_ledger import STAGE, SUPPLEMENTS, CollectionLedger
from project_watch.studio.collection_runner import run_collection
from project_watch.studio.collection_scope import watch_direction
from project_watch.studio.dsh_bootstrap import PublicBootstrap
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import SearchConfig


@pytest.fixture
def research(tmp_path):
    pipeline, execution = host_fixtures.setup.__wrapped__(tmp_path)
    settings = pipeline.settings_store.load()
    settings.search = SearchConfig(provider='tavily', api_key='synthetic')
    pipeline.settings_store.save(settings)
    calls = []

    def send(_method, _url, _headers, body, _timeout):
        query = json.loads(body)['query']
        calls.append(query)
        if query == 'malformed':
            return Response(200, {}, b'{}')
        if query == 'unknown':
            raise HttpError('connection_failed')
        return Response(200, {}, json.dumps({'results': [dict(
            title='Synthetic ' + query, url='https://example.org/' + str(len(calls)), content='Public source excerpt')]}).encode())

    pipeline.http_factory = lambda _: Http(send)
    _, _, _, collectors = pipeline.tools()
    result = PublicBootstrap(pipeline, execution, collectors, None)
    from test_studio_dsh_monitor import synthetic_review
    result.record_reviewer = synthetic_review
    p = plan([candidate() | dict(source='search', category='company', query='base',
                                target_id=result.project_id)], round_id=execution.job['id'], project_id=result.project_id)
    ledger = CollectionLedger(execution, p)
    sources = collection_host.sources_for(result)
    run_collection(ledger, collect=sources.collect, ingest=CollectionIngest(execution), guard=result.guard)
    return result, calls


def test_supplement_keeps_base_frozen_and_enters_evidence_catalog(research):
    loop, calls = research
    base = deepcopy(loop.execution.read_checkpoint(STAGE))
    result = loop.tool('pw_search', {'query': 'extra', 'category': 'competitor'})
    assert result['collection']['outcome'] == 'success' and calls == ['base', 'extra']
    assert loop.execution.read_checkpoint(STAGE) == base
    assert len(loop.execution.read_checkpoint(SUPPLEMENTS)) == 1
    assert collection_dsh.coverage(loop)['status'] == 'complete'
    assert collection_dsh.catalog(loop)['total'] == 2
    [entry] = result['sources']
    assert entry['collection_matches'][0]['direction'] == 'competitor'
    read = loop.tool('pw_read', {'source_id': entry['id']})
    assert read['read_scope'] == 'summary' and 'Public source excerpt' in read['excerpt']
    assert calls == ['base', 'extra']


def test_exact_base_and_supplement_queries_are_not_sent_again(research):
    loop, calls = research
    for query in ('base', 'base', 'extra', 'extra'):
        result = loop.tool('pw_search', {'query': query, 'category': 'company'})
        assert result['collection']['outcome'] == 'success'
    assert calls == ['base', 'extra']
    assert len(loop.state['searches']) == 2
    assert loop.execution.usage()['search']['requests'] == 2


def test_same_query_for_another_direction_reuses_cache_and_retains_both_matches(research):
    loop, calls = research
    first = loop.tool('pw_search', {'query': 'base', 'category': 'company'})
    loop.tool('pw_read', {'source_id': first['sources'][0]['id']})
    result = collection_dsh.search(loop, 'base', 'chain', target_id='watch:synthetic')
    assert calls == ['base'] and result['collection']['outcome'] == 'success'
    matches = result['sources'][0]['collection_matches']
    assert {(m['direction'], m['target_id']) for m in matches} == {
        ('company', loop.project_id), ('chain', 'watch:synthetic')}


def test_failed_supplement_cannot_disappear_from_finish_coverage_or_retry(research):
    loop, calls = research
    for _ in range(2):
        result = loop.tool('pw_search', {'query': 'malformed', 'category': 'company'})
        assert result['collection']['outcome'] == 'failed' and result['sources'] == []
    assert calls == ['base', 'malformed']
    assert collection_dsh.coverage(loop)['status'] == 'partial'
    assert not loop.state['searches']


def test_budget_deferred_supplement_has_no_request_and_no_success_claim(research):
    loop, calls = research
    loop.execution.budgets['search'] = 1
    result = loop.tool('pw_search', {'query': 'extra', 'category': 'company'})
    assert result['collection']['outcome'] == 'budget_deferred'
    assert calls == ['base'] and collection_dsh.coverage(loop)['status'] == 'partial'


def test_unknown_supplement_blocks_all_further_dispatch(research):
    loop, calls = research
    for query in ('unknown', 'another'):
        with pytest.raises(HttpError):
            loop.tool('pw_search', {'query': query, 'category': 'company'})
    assert calls == ['base', 'unknown']
    assert loop.execution.usage()['search']['unknown'] == 1


def test_indexed_intent_remains_pending_after_pre_dispatch_interruption(research, monkeypatch):
    loop, calls = research
    original = CollectionLedger.__init__
    interrupted = []

    def interrupted_init(self, *args, **kwargs):
        if kwargs.get('stage', STAGE) != STAGE and not interrupted:
            interrupted.append(1)
            raise RuntimeError('synthetic interruption before ledger')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CollectionLedger, '__init__', interrupted_init)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        loop.tool('pw_search', {'query': 'extra', 'category': 'company'})
    assert calls == ['base'] and collection_dsh.coverage(loop)['status'] == 'partial'
    result = loop.tool('pw_search', {'query': 'extra', 'category': 'company'})
    assert result['collection']['outcome'] == 'success' and calls == ['base', 'extra']
    assert len(loop.execution.read_checkpoint(SUPPLEMENTS)) == 1


@pytest.mark.parametrize('query', ['relation', 'malformed'])
def test_explore_retains_relation_direction_and_only_marks_successful_query(research, query):
    from project_watch.studio.dsh_monitor import PublicMonitor

    loop, calls = research
    watch = loop.pipeline.monitoring_plan.add(loop.project_id, {
        'text': 'Synthetic supplier', 'queries': [query], 'scope_kind': 'relation',
        'reason': 'Synthetic public relation', 'sources': ['https://example.org/relation'],
        'relation_type': 'supplier', 'relation_target': 'Synthetic supplier', 'axis': 'partners'},
        actor='human', origin='human', visibility='public')
    monitoring = PublicMonitor(loop.pipeline, loop.execution, loop.collectors, None)
    result = monitoring.tool('pw_explore', {'watch_id': watch['id'], 'query': query})
    assert result['collection']['target_id'] == 'watch:' + watch['id']
    assert calls == ['base', query]
    current = next(w for w in monitoring.plan.items(loop.project_id) if w['id'] == watch['id'])
    if query == 'relation':
        assert current['last_run_at'] and result['collection']['outcome'] == 'success'
        assert result['sources'][0]['collection_matches'][0]['direction'] == 'chain'
    else:
        assert not current['last_run_at'] and result['collection']['outcome'] == 'failed'


def test_gateway_binding_cannot_be_inherited_by_replacement_collectors(research):
    loop, calls = research
    loop.collectors = copy(loop.collectors)
    with ThreadPoolExecutor(max_workers=1) as executor, pytest.raises(ValueError, match='current job execution'):
        executor.submit(collection_host.sources_for, loop).result()
    assert calls == ['base']


@pytest.mark.parametrize('axis,direction', [('science', 'technology'), ('rules', 'policy'),
                                          ('market', 'industry'), ('partners', 'chain'), ('peers', 'competitor')])
def test_saved_axis_maps_to_direction_without_industry_keywords(axis, direction):
    assert watch_direction({'axis': axis}) == direction
