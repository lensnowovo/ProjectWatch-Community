"""Specialist queries originate in public evidence and use real host receipts."""
import json
from contextlib import closing
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
import test_collection_supplements as fixtures
from test_collection_pages import study, work

from project_watch.studio import collection_dsh, collection_host, collection_targets
from project_watch.studio.collection_ledger import STAGE
from project_watch.studio.dsh_bootstrap import PublicBootstrap
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart


@pytest.fixture
def research(tmp_path):
    loop, searches = fixtures.research.__wrapped__(tmp_path)
    calls = []

    def send(_method, url, _headers, body, _timeout):
        calls.append(url)
        host = urlsplit(url).hostname
        if host == 'clinicaltrials.gov':
            payload = {'studies': [study()]}
        elif host == 'api.openalex.org':
            payload = {'results': [work()], 'meta': {'count': 1}}
        elif host == 'www.cninfo.com.cn':
            payload = {'announcements': [], 'totalAnnouncement': 0}
        elif host == 'api.tavily.com':
            assert json.loads(body)['query']
            payload = {'results': []}
        else:
            pytest.fail('unexpected synthetic host: ' + host)
        return Response(200, {}, json.dumps(payload).encode())

    loop.pipeline.http_factory = lambda _: Http(send)
    loop.collectors = loop.pipeline.tools()[3]
    collection_host.sources_for(loop)  # Production prepare binds before the SDK gateway thread.
    [entry] = collection_dsh.catalog(loop)['sources']
    args = dict(adapter='clinical_term', query='Public source', source_id=entry['id'],
                quote='Public source excerpt', reason='Synthetic evidence supplies a query candidate')
    return loop, args, calls, searches


def test_specialist_tool_retains_query_and_imports_typed_record_without_repeating(research):
    loop, args, calls, searches = research
    original = deepcopy(loop.execution.read_checkpoint(STAGE))
    first = loop.tool('pw_collect', args)
    second = loop.tool('pw_collect', args)
    assert first == second and first['collection']['outcome'] == 'success'
    assert len(calls) == 1 and searches == ['base']
    assert parse_qs(urlsplit(calls[0]).query)['query.term'] == ['Public source']
    assert 'query.spons' not in calls[0]
    assert loop.execution.usage()['open_data']['requests'] == 1
    [target] = collection_targets.targets(loop)
    assert target['basis'] == 'public_query_candidate' and target['adapter'] == 'clinical_term'
    assert first['sources'][0]['source_type'] == 'structured_record'
    assert first['sources'][0]['collection_matches'][0]['direction'] == 'trial'
    assert 'Synthetic Hospital' in first['sources'][0]['excerpt']
    assert loop.execution.read_checkpoint(STAGE) == original
    assert loop.state['searches'] == []  # professional queries are not fabricated web-search calls


@pytest.mark.parametrize('adapter', list(collection_targets.ADAPTERS))
def test_each_specialist_adapter_is_routed_through_host(research, adapter):
    loop, args, calls, _ = research
    result = loop.tool('pw_collect', args | {'adapter': adapter})
    assert result['collection']['source'] == adapter
    assert result['collection']['outcome'] == 'success'
    assert calls
    assert result['query_scope']['category'] == collection_targets.ADAPTERS[adapter][0]


@pytest.mark.parametrize('change', [dict(query='unsupported term'), dict(quote='not in source'),
                                  dict(adapter='not_applicable'), dict(source_id='Cmissing')])
def test_invalid_or_unsupported_scope_never_dispatches(research, change):
    loop, args, calls, _ = research
    with pytest.raises(ValueError):
        loop.tool('pw_collect', args | change)
    assert not calls and collection_targets.targets(loop) == []


def test_internal_and_cross_project_evidence_cannot_supply_scope(research):
    loop, _, calls, _ = research
    other = loop.pipeline.repository.create_project('Other synthetic company')
    for project, visibility in [(loop.project_id, 'internal'), (other.id, 'public')]:
        record = loop.pipeline.evidence.save(EvidenceInput(project_id=project, source_key='synthetic',
            extraction_version='v1', visibility=visibility, read_scope='partial',
            parts=[EvidencePart(locator_kind='stored_text', locator='0', text='Synthetic private scope')]))
        with pytest.raises(KeyError):
            collection_targets.retain(loop, adapter='clinical_term', query='Synthetic', reason='Synthetic reason',
                ref=dict(evidence_version_id=record['id'], part_index=0, start=0, end=23, quote='Synthetic private scope'))
    assert not calls and collection_targets.targets(loop) == []


def test_budget_deferred_scope_is_recorded_but_not_reported_as_success(research):
    loop, args, calls, _ = research
    loop.execution.budgets['open_data'] = 0
    result = loop.tool('pw_collect', args)
    assert result['collection']['outcome'] == 'budget_deferred'
    assert collection_dsh.coverage(loop)['status'] == 'partial'
    assert not calls and len(collection_targets.targets(loop)) == 1
    loop.tool('pw_collect', args)
    assert not calls


def test_next_round_dispatches_retained_scope_without_reading_raw_plan(research, monkeypatch):
    loop, args, calls, _ = research
    result = loop.tool('pw_collect', args)
    old_execution = loop.execution
    assert loop.store.finish(old_execution.job['id'], lease_token=old_execution.job['lease_token'])
    loop.store.enqueue(loop.project_id, 'external')
    execution = JobExecution(loop.store, loop.store.claim())
    loop.pipeline._execution_state.current = execution
    collection_host.admit(execution, mode='incremental')
    collectors = loop.pipeline.tools()[3]
    next_loop = PublicBootstrap(loop.pipeline, execution, collectors, None)
    monkeypatch.setattr(loop.store, 'get_plan', lambda *_: pytest.fail('raw internal plan must not be read'))
    collection_host.prepare(next_loop)
    plan = execution.read_checkpoint(STAGE)['plan']
    unit = next(u for u in plan['units'] if u['source'] == 'clinical_term')
    assert unit['query'] == args['query'] and unit['target_id'] == 'scope:' + result['query_scope']['id']
    assert unit['applicability_evidence_ids'] == [result['query_scope']['ref']['evidence_version_id']]
    assert not any(u['source'] == 'clinical_sponsor' for u in plan['units'])
    assert any(r['source'] == 'clinical_term' and r['outcome'] == 'success'
               for r in collection_dsh.coverage(next_loop)['rows'])
    # A cached API response may be reused; the new round must still have an actual receipt.
    assert any('clinicaltrials.gov' in c for c in calls)


def test_disabled_scope_cannot_be_revived_by_model(research):
    loop, args, calls, _ = research
    result = loop.tool('pw_collect', args)
    target = result['query_scope'] | {'status': 'disabled'}
    with closing(loop.store._connect()) as db, db:
        db.execute('UPDATE studio_collection_targets SET record=? WHERE project_id=? AND id=?',
                   (json.dumps(target), loop.project_id, target['id']))
    with pytest.raises(RuntimeError, match='scope changed'):
        loop.tool('pw_collect', args)
    assert len(calls) == 1
    assert collection_targets.targets(loop)[0]['capability'] == 'disabled'


def test_scope_withdrawal_between_professional_pages_stops_dispatch(research):
    loop, args, calls, _ = research

    def withdraw(*_args):
        calls.append('first page')
        [target] = collection_targets.targets(loop)
        target['status'] = 'disabled'
        target.pop('capability')
        with closing(loop.store._connect()) as db, db:
            db.execute('UPDATE studio_collection_targets SET record=? WHERE project_id=? AND id=?',
                       (json.dumps(target), loop.project_id, target['id']))
        return Response(200, {}, json.dumps({'studies': [study()], 'nextPageToken': 'page-two'}).encode())

    loop.pipeline.http_factory = lambda _: Http(withdraw)
    loop.collectors = loop.pipeline.tools()[3]
    with pytest.raises(RuntimeError, match='scope changed'):
        loop.tool('pw_collect', args)
    assert calls == ['first page']


def test_specialist_unknown_stops_other_queries(research):
    loop, args, calls, _ = research

    def unknown(*_args):
        calls.append('synthetic unknown')
        raise HttpError('connection_failed')

    loop.pipeline.http_factory = lambda _: Http(unknown)
    loop.collectors = loop.pipeline.tools()[3]
    with pytest.raises(HttpError):
        loop.tool('pw_collect', args)
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        loop.tool('pw_collect', args | {'adapter': 'openalex_topic'})
    assert calls == ['synthetic unknown']
