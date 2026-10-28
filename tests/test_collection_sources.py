"""Exercise real Collectors via a synthetic transport and isolated query cache."""
import json

import pytest
import test_studio_dsh_monitor as fixtures
from test_collection_plan import plan

from project_watch.studio.collection_sources import CollectionSources, _validate_search_response
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.query_ledger import ProjectQueryCache
from project_watch.studio.settings import SearchConfig
from project_watch.studio.sources import Collectors


@pytest.fixture
def adapter(tmp_path):
    research = fixtures.loop.__wrapped__(tmp_path)
    execution = research.execution
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append(json.loads(body)['query'])
        return Response(200, {}, json.dumps({'results': [dict(
            title='Synthetic research', url='https://example.org/story', content='Synthetic excerpt')]}).encode())

    http = execution.http(Http(transport), lambda _: ('search', 'synthetic-provider'))
    cache = ProjectQueryCache(execution.store, execution.job['project_id'], purpose='collection',
                              job_id=execution.job['id'], guard=execution._guard)
    collectors = Collectors(http, SearchConfig(provider='tavily', api_key='synthetic-key'), search_cache=cache)
    return CollectionSources(execution, collectors), calls


def unit(adapter, source='search'):
    return plan(round_id=adapter.execution.job['id'])['units'][0] | {'source': source}


def test_request_and_cache_use_are_distinct_and_retain_original_time(adapter):
    sources, calls = adapter
    first = sources.collect(unit(sources))
    second = sources.collect(unit(sources))
    assert len(calls) == 1
    assert first['items'] == second['items']
    assert first['request_ids'][0].startswith('call:')
    assert first['cache_provenance'] == []
    assert second['request_ids'][0].startswith('query-use:')
    assert second['cache_provenance'][0]['completed_at']
    assert second['coverage_kind'] == 'bounded_search'
    assert second['window_applied'] is False
    assert 'synthetic-key' not in json.dumps(first['request_provenance'])


def test_same_job_http_replay_is_provenance_even_without_query_cache(adapter):
    sources, calls = adapter
    sources.collectors.search_cache = None
    sources.collect(unit(sources))
    replay = sources.collect(unit(sources))
    assert len(calls) == 1
    assert replay['request_provenance']['requests'][0]['replay_hits'] == 1
    assert replay['request_ids'][0].startswith('call:')


def test_missing_key_never_becomes_empty_success(adapter):
    sources, calls = adapter
    sources.collectors.search_config = SearchConfig()
    assert sources.capabilities()['search'] == 'not_configured'
    with pytest.raises(HttpError, match='unavailable'):
        sources.collect(unit(sources))
    assert calls == []


def test_patent_filter_retains_query_and_does_not_assign_company_ownership(adapter):
    sources, calls = adapter
    result = sources.collect(unit(sources, 'patent_search'))
    assert result['items'] == []  # non-patent host is not promoted to patent evidence
    assert len(calls) == 1 and calls[0].startswith('site:patents.google.com ')
    assert result['request_ids']


def test_regulatory_adapter_records_all_physical_searches(adapter):
    sources, calls = adapter
    result = sources.collect(unit(sources, 'cn_regulatory'))
    assert len(calls) == 3
    assert len(result['request_provenance']['requests']) == 3
    assert result['items'] == []


def test_source_not_integrated_never_dispatches(adapter):
    sources, calls = adapter
    assert 'unregistered_source' not in sources.capabilities()
    with pytest.raises(HttpError, match='unavailable'):
        sources.collect(unit(sources, 'unregistered_source'))
    assert calls == []


@pytest.mark.parametrize('value', [{}, {'results': None}, {'results': {}}, {'results': [None]}])
def test_malformed_wire_response_is_not_empty_success(value):
    with pytest.raises(HttpError, match='invalid_search_response'):
        _validate_search_response('https://api.tavily.com/search', Response(200, {}, json.dumps(value).encode()))


@pytest.mark.parametrize('url,value', [
    ('https://api.tavily.com/search', {'results': []}),
    ('https://api.deepseek.com/v1/tools/search', {'results': []}),
    ('https://api.bochaai.com/v1/web-search', {'data': {'webPages': {'value': []}}}),
    ('https://api.search.brave.com/res/v1/web/search', {'web': {'results': []}}),
    ('https://api.search.brave.com/res/v1/news/search', {'results': []}),
])
def test_explicit_empty_wire_list_is_a_valid_result(url, value):
    _validate_search_response(url, Response(200, {}, json.dumps(value).encode()))


def test_malformed_response_records_known_request_without_caching_empty_success(adapter):
    sources, _calls = adapter
    execution = sources.execution
    http = execution.http(Http(lambda *_: Response(200, {}, b'{}')),
                          lambda _: ('search', 'synthetic-malformed'))
    collectors = Collectors(http, sources.collectors.search_config, search_cache=sources.collectors.search_cache)
    checked = CollectionSources(execution, collectors)
    scope = unit(checked)
    with pytest.raises(HttpError, match='invalid_search_response'):
        checked.collect(scope)
    trace = execution.read_checkpoint('collection_trace:' + scope['id'][:40])
    assert trace['requests'][0]['status'] == 'completed'  # HTTP received, parse failed
    assert trace['query_uses'][0]['outcome'] == 'failed'
    assert execution.usage()['search']['unknown'] == 0


def test_timeout_trace_is_retained_and_cache_does_not_retry(adapter):
    sources, _calls = adapter
    execution = sources.execution
    sent = []

    def timeout(*_args):
        sent.append(True)
        raise HttpError('connection_failed')

    collectors = Collectors(execution.http(Http(timeout), lambda _: ('search', 'synthetic-timeout')),
                            sources.collectors.search_config, search_cache=sources.collectors.search_cache)
    checked = CollectionSources(execution, collectors)
    scope = unit(checked)
    with pytest.raises(HttpError, match='connection_failed'):
        checked.collect(scope)
    trace = execution.read_checkpoint('collection_trace:' + scope['id'][:40])
    assert trace['requests'][0]['status'] == 'unknown'
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        checked.collect(scope)
    assert len(sent) == 1
    retained = execution.read_checkpoint('collection_trace:' + scope['id'][:40])
    assert retained['requests'][0]['status'] == 'unknown'
    # The cache transaction rolls back its attempted "held" insert when it
    # raises. Preserve the actual unknown use and record the admission error.
    assert {u['outcome'] for u in retained['query_uses']} == {'unknown'}
    assert retained['last_error'] == 'request_outcome_unknown'
