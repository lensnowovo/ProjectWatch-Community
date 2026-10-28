"""Cross-round structured pages use saved positions, never repeat unknown calls."""
import json
from contextlib import closing
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
import test_collection_host as host_fixtures
import test_studio_dsh_monitor as fixtures
from test_collection_pages import announcement, study, work
from test_collection_plan import candidate, plan

from project_watch.studio import collection_continuation as continuation
from project_watch.studio import collection_dsh, collection_host
from project_watch.studio import collection_history as history
from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.collection_runner import run_collection
from project_watch.studio.collection_sources import CollectionSources
from project_watch.studio.dsh_bootstrap import PublicBootstrap
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import SearchConfig
from project_watch.studio.sources import Collectors


def manifest(execution, source='openalex_topic', *, at='2026-10-26T12:00:00+08:00', binding='config-v1', **changes):
    raw = plan([candidate() | dict(source=source, window_end=at, **changes)], at=at,
               round_id=execution.job['id'], project_id=execution.job['project_id'])
    return history.schedule(raw, binding, history.read(execution.store, raw['project_id']),
                            continuations=continuation.read(execution.store, raw['project_id']))


def next_job(execution):
    store, job = execution.store, execution.job
    assert store.finish(job['id'], lease_token=job['lease_token'])
    queued = store.enqueue(job['project_id'], 'external')
    claimed = store.claim()
    assert queued['id'] == claimed['id']
    return JobExecution(store, claimed)


def run(execution, p, send, *, max_pages=1):
    ledger = CollectionLedger(execution, p)
    sources = CollectionSources(execution, Collectors(execution.http(
        Http(send), lambda _: ('open_data', 'synthetic-source')), SearchConfig()), max_pages=max_pages)
    result = run_collection(ledger, collect=sources.collect, ingest=CollectionIngest(execution), guard=lambda: None)
    return ledger, result


@pytest.mark.parametrize('source', ['openalex_topic', 'cninfo', 'clinical_term'])
def test_three_rounds_continue_without_repeating_saved_pages_or_advancing_early(tmp_path, source):
    execution = fixtures.loop.__wrapped__(tmp_path).execution
    calls = []
    def send(_method, url, *_):
        args = parse_qs(urlsplit(url).query)
        page = int(args.get('page', args.get('pageNum', args.get('pageToken', ['1'])))[0])
        calls.append(page)
        indices = range((page - 1) * 20 + 1, min(page * 20, 45) + 1)
        if source == 'openalex_topic':
            data = dict(results=[work(n) for n in indices], meta={'count': 45})
            assert 'to_publication_date:2026-10-26' in args['filter'][0]
        elif source == 'cninfo':
            data = dict(announcements=[announcement(n) for n in indices], totalAnnouncement=45)
            assert args['edate'] == ['2026-10-26']
        else:
            rows = []
            for n in indices:
                row = deepcopy(study())
                row['protocolSection']['identificationModule']['nctId'] = f'NCT{n:08d}'
                rows.append(row)
            data = dict(studies=rows, **({'nextPageToken': str(page + 1)} if page < 3 else {}))
        return Response(200, {}, json.dumps(data).encode())
    origin = '2026-10-26T12:00:00+08:00'
    for index in range(3):
        p = manifest(execution, source, at=f'2026-10-{26 + index}T12:00:00+08:00')
        unit = p['units'][0]
        assert unit['window_end'] == origin
        if index:
            assert unit['continuation']['pages_completed'] == index
            assert unit['continuation']['previous_job_id'] != execution.job['id']
        ledger, result = run(execution, p, send)
        assert result['status'] == ('complete' if index == 2 else 'partial')
        assert ledger.receipts[0]['pages_completed'] == 1
        assert ledger.receipts[0]['result_count'] == (5 if index == 2 else 20)
        assert len(ledger.receipts[0]['request_ids']) == 1
        assert ledger.receipts[0]['request_ids'][0].startswith('call:' + execution.job['id'] + ':')
        # Same-job replay neither imports prior pages nor requests this page again.
        run(execution, p, lambda *_: pytest.fail('replay must not dispatch'))
        saved = history.read(execution.store, execution.job['project_id'])
        if index < 2:
            assert not saved
            execution = next_job(execution)
    assert calls == [1, 2, 3]
    assert next(iter(saved.values()))['last_success_at'] == '2026-10-26T04:00:00+00:00'
    assert next(iter(continuation.read(execution.store, p['project_id']).values()))['continuation'] is None
    with closing(execution.store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM studio_collection_sources').fetchone()[0] == 45


def partial(execution):
    p = manifest(execution)
    def send(*_):
        return Response(200, {}, json.dumps(dict(results=[work(n) for n in range(20)], meta={'count': 40})).encode())
    ledger, _ = run(execution, p, send)
    return p, ledger


def test_zero_remaining_budget_keeps_position_and_does_not_certify_progress(tmp_path):
    execution = fixtures.loop.__wrapped__(tmp_path).execution
    p, _ = partial(execution)
    before = continuation.read(execution.store, p['project_id'])
    execution = next_job(execution)
    execution.budgets['open_data'] = 0
    p = manifest(execution, at='2026-10-27T12:00:00+08:00')
    ledger, coverage = run(execution, p, lambda *_: pytest.fail('zero budget must not dispatch'))
    assert coverage['status'] == 'partial' and ledger.receipts[0]['outcome'] == 'budget_deferred'
    assert continuation.read(execution.store, p['project_id']) == before
    assert not history.read(execution.store, p['project_id'])


@pytest.mark.parametrize('change', ['config', 'version', 'query', 'evidence', 'disabled', 'not_due'])
def test_changed_scope_or_excluded_unit_cannot_inherit_cursor(tmp_path, change):
    execution = fixtures.loop.__wrapped__(tmp_path).execution
    partial(execution)
    execution = next_job(execution)
    values = dict(config={'binding': 'config-v2'}, version={'query_version': 'changed'}, query={'query': 'Changed'},
        evidence={'applicability_evidence_ids': ['new-proof']}, disabled={'capability': 'disabled', 'reason': 'Owner disabled'},
        not_due={'schedule_due': False})[change]
    p = manifest(execution, at='2026-10-27T12:00:00+08:00', **values)
    assert 'continuation' not in p['units'][0]


@pytest.mark.parametrize('failure', ['malformed', 'unknown'])
def test_failure_invalidates_cursor_and_old_ledger_cannot_resurrect_it(tmp_path, failure):
    execution = fixtures.loop.__wrapped__(tmp_path).execution
    old_plan, _ = partial(execution)
    old_execution = execution
    execution = next_job(execution)
    p = manifest(execution, at='2026-10-27T12:00:00+08:00')
    calls = []
    def send(_method, url, *_):
        calls.append(url)
        if failure == 'unknown':
            raise HttpError('connection_failed')
        return Response(200, {}, b'{}')
    if failure == 'unknown':
        with pytest.raises(HttpError):
            run(execution, p, send)
        with pytest.raises(HttpError):
            run(execution, p, send)
        assert execution.usage()['open_data']['unknown'] == 1
    else:
        _, coverage = run(execution, p, send)
        assert coverage['rows'][0]['outcome'] == 'failed'
    assert len(calls) == 1
    assert next(iter(continuation.read(execution.store, p['project_id']).values()))['continuation'] is None
    with pytest.raises(RuntimeError, match='lease'):
        CollectionLedger(old_execution, old_plan)
    assert not history.read(execution.store, p['project_id'])


def test_derived_position_rebuilt_after_index_write_interruption(tmp_path, monkeypatch):
    execution = fixtures.loop.__wrapped__(tmp_path).execution
    original = continuation.record
    def crash(execution, plan, receipts):
        if any(r['outcome'] != 'running' for r in receipts):
            raise RuntimeError('synthetic index interruption')
        return original(execution, plan, receipts)
    monkeypatch.setattr(continuation, 'record', crash)
    with pytest.raises(RuntimeError, match='synthetic index'):
        partial(execution)
    monkeypatch.setattr(continuation, 'record', original)
    p = manifest(execution)
    run(execution, p, lambda *_: pytest.fail('saved page must not be requested again'))
    value = next(iter(continuation.read(execution.store, p['project_id']).values()))
    assert value['continuation']['cursor'] == 2
    assert execution.usage()['open_data']['requests'] == 1


def test_clinical_cursor_cycle_across_jobs_stops_before_repeating_request(tmp_path):
    execution = fixtures.loop.__wrapped__(tmp_path).execution
    calls = []
    def send(_method, url, *_):
        token = parse_qs(urlsplit(url).query).get('pageToken', [None])[0]
        calls.append(token)
        return Response(200, {}, json.dumps(dict(studies=[study()],
            nextPageToken={None: 'a', 'a': 'b', 'b': 'a'}[token])).encode())
    for index in range(4):
        p = manifest(execution, 'clinical_term', at=f'2026-10-{26 + index}T12:00:00+08:00')
        ledger, coverage = run(execution, p, send)
        assert coverage['status'] == 'partial'
        if index < 3:
            execution = next_job(execution)
    assert calls == [None, 'a', 'b']
    assert ledger.receipts[0]['reason'] == 'collection_cursor_cycle'
    assert not history.read(execution.store, p['project_id'])
    assert next(iter(continuation.read(execution.store, p['project_id']).values()))['continuation'] is None


def test_dsh_discovered_scope_continues_in_next_host_round(tmp_path):
    pipeline, execution = host_fixtures.setup.__wrapped__(tmp_path)
    settings = pipeline.settings_store.load()
    settings.search = SearchConfig(providers=['tavily'], keys={'tavily': 'synthetic'}, google_news=False)
    pipeline.settings_store.save(settings)
    clinical_pages = []
    def send(_method, url, _headers, _body, _timeout):
        host = urlsplit(url).hostname
        if host == 'clinicaltrials.gov':
            page = int(parse_qs(urlsplit(url).query).get('pageToken', ['1'])[0])
            clinical_pages.append(page)
            rows = []
            for n in range((page - 1) * 20 + 1, min(page * 20, 65) + 1):
                row = deepcopy(study())
                row['protocolSection']['identificationModule']['nctId'] = f'NCT{n:08d}'
                rows.append(row)
            data = dict(studies=rows, **({'nextPageToken': str(page + 1)} if page < 4 else {}))
        elif host == 'api.tavily.com':
            data = dict(results=[dict(title='Synthetic public source', content='Public source excerpt',
                                      url='https://example.org/public-source')])
        elif host == 'api.openalex.org':
            data = dict(results=[], meta={'count': 0})
        elif host == 'www.cninfo.com.cn':
            data = dict(announcements=[], totalAnnouncement=0)
        else:
            pytest.fail('unexpected provider')
        return Response(200, {}, json.dumps(data).encode())
    pipeline.http_factory = lambda _: Http(send)
    def host_round(execution, mode):
        pipeline._execution_state.current = execution
        collection_host.admit(execution, mode=mode)
        loop = PublicBootstrap(pipeline, execution, pipeline.tools()[3], None)
        collection_host.prepare(loop)
        return loop
    loop = host_round(execution, 'first')
    source = next(s for s in collection_dsh.catalog(loop)['sources'] if s['title'] == 'Synthetic public source')
    result = loop.tool('pw_collect', dict(adapter='clinical_term', query='Public source', source_id=source['id'],
                      quote='Public source excerpt', reason='Synthetic evidenced query'))
    assert result['collection']['outcome'] == 'partial'
    assert clinical_pages == [1, 2, 3]
    execution = next_job(execution)
    second = host_round(execution, 'incremental')
    assert clinical_pages == [1, 2, 3, 4]
    from project_watch.studio.collection_ledger import STAGE
    saved = execution.read_checkpoint(STAGE)
    [unit] = [u for u in saved['plan']['units'] if u['source'] == 'clinical_term']
    assert unit['continuation']['pages_completed'] == 3
    [row] = [r for r in saved['receipts'] if r['unit_id'] == unit['id']]
    assert row['result_count'] == 5 and not row['truncated']
    assert any(s['source_type'] == 'structured_record' for s in collection_dsh.catalog(second)['sources'])
