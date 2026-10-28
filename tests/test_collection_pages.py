"""Structured pagination and date semantics against synthetic wire responses."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import test_studio_dsh_monitor as fixtures
from test_collection_plan import candidate, plan

from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.collection_pages import clinical_page, cninfo_page, openalex_page
from project_watch.studio.collection_runner import run_collection
from project_watch.studio.collection_sources import CollectionSources
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import SearchConfig
from project_watch.studio.sources import Collectors


def work(n=1):
    return {'id': f'https://openalex.org/W{n}', 'doi': f'https://doi.org/10.0/{n}',
            'display_name': f'Synthetic paper {n}', 'publication_date': '2026-10-20',
            'authorships': [], 'abstract_inverted_index': {'Synthetic': [0], 'abstract': [1]}}


def study():
    return {'protocolSection': {'identificationModule': {'nctId': 'NCT00000001', 'briefTitle': 'Synthetic trial'},
                               'statusModule': {'overallStatus': 'RECRUITING',
                                                'studyFirstPostDateStruct': {'date': '2024-01-02'},
                                                'lastUpdatePostDateStruct': {'date': '2026-10-20'}},
                               'sponsorCollaboratorsModule': {'leadSponsor': {'name': 'Synthetic Hospital'}}}}


def wire(data, calls=None):
    def send(_method, url, *_args):
        if calls is not None:
            calls.append(parse_qs(urlsplit(url).query))
        return Response(200, {}, json.dumps(data).encode())
    return Http(send)


def announcement(n=1, *, attachment=True):
    return {'announcementId': str(n), 'announcementTitle': f'Synthetic filing {n}',
            'secName': 'Synthetic Issuer', 'announcementTime': 1792454400000,
            'adjunctUrl': f'finalpage/2026-10-20/{n}.PDF' if attachment else ''}


def test_announcement_metadata_is_retained_without_claiming_pdf_read():
    calls = []
    result = cninfo_page(wire({'announcements': [announcement(attachment=False)],
                               'totalAnnouncement': 2}, calls), 'Synthetic Issuer', per_page=1)
    item = result['items'][0]
    assert item['extra']['source_identity'] == 'cninfo:1'
    assert not item['extra']['attachment_available']
    assert item['extra']['source_record'] == announcement(attachment=False)
    assert item['url'].startswith('https://www.cninfo.com.cn/new/fulltextSearch/full?')
    assert result['next_cursor'] == 2 and result['truncated']
    assert calls[0]['pageNum'] == ['1']


def test_announcement_zero_and_unknown_total_are_distinct():
    empty = cninfo_page(wire({'announcements': [], 'totalAnnouncement': 0}), 'Synthetic')
    assert not empty['truncated'] and empty['items'] == []
    unknown = cninfo_page(wire({'announcements': [announcement()]}), 'Synthetic')
    assert unknown['truncated'] and unknown['next_cursor'] is None
    assert unknown['total_reported'] is None


def test_announcement_explicit_zero_metadata_can_explain_null_results():
    data = {'announcements': None, 'totalAnnouncement': 0, 'totalRecordNum': 0,
            'totalpages': 0, 'hasMore': False}
    result = cninfo_page(wire(data), 'Synthetic')
    assert result['items'] == []
    assert result['total_reported'] == 0
    assert result['truncated'] is False and result['next_cursor'] is None
    assert data['announcements'] is None


@pytest.mark.parametrize('change', [
    {'totalAnnouncement': 1}, {'totalAnnouncement': None}, {'totalAnnouncement': False},
    {'hasMore': True}, {'hasMore': None}, {'totalRecordNum': 1}, {'totalpages': 1},
])
def test_null_announcement_rows_without_consistent_zero_metadata_still_fail(change):
    data = {'announcements': None, 'totalAnnouncement': 0, 'totalRecordNum': 0,
            'totalpages': 0, 'hasMore': False} | change
    with pytest.raises(HttpError):
        cninfo_page(wire(data), 'Synthetic')


@pytest.mark.parametrize('data', [{}, {'announcements': [], 'totalAnnouncement': 1},
                                 {'announcements': [], 'totalAnnouncement': True},
                                 {'announcements': [announcement() | {'adjunctUrl': '../private'}]}])
def test_announcement_invalid_page_cannot_report_successful_zero(data):
    with pytest.raises(HttpError):
        cninfo_page(wire(data), 'Synthetic')


def test_announcement_pages_keep_request_provenance_across_resume(tmp_path):
    research = fixtures.loop.__wrapped__(tmp_path)
    execution, calls = research.execution, []

    def send(_method, url, *_args):
        page = int(parse_qs(urlsplit(url).query)['pageNum'][0])
        calls.append(page)
        rows = [announcement(n) for n in range(1, 21)] if page == 1 else [announcement(21, attachment=False)]
        return Response(200, {}, json.dumps({'announcements': rows, 'totalAnnouncement': 21}).encode())

    adapter = CollectionSources(execution, Collectors(execution.http(
        Http(send), lambda _: ('open_data', 'synthetic-cninfo')), SearchConfig()))
    unit = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])['units'][0] | {'source': 'cninfo'}
    first, second = adapter.collect(unit), adapter.collect(unit)
    assert calls == [1, 2]
    assert len(first['items']) == 21 and not first['truncated']
    assert first['items'] == second['items']
    assert len(first['request_ids']) == 2 and first['request_ids'] == second['request_ids']
    assert execution.usage()['open_data']['requests'] == 2


def test_paper_page_keeps_scope_and_next_page():
    calls = []
    result = openalex_page(wire({'results': [work()], 'meta': {'count': 2}}, calls),
                           'Synthetic Institute', mode='affiliation', per_page=1,
                           start='2026-01-01', end='2026-10-26')
    assert result['truncated'] and result['next_cursor'] == 2
    assert calls[0]['filter'] == ['raw_affiliation_strings.search:Synthetic Institute,from_publication_date:2026-01-01,to_publication_date:2026-10-26']
    assert result['items'][0]['extra']['source_record'] == work()
    assert result['items'][0]['extra']['paper_search']['mode'] == 'affiliation'


@pytest.mark.parametrize('data', [{}, {'results': [], 'meta': {'count': 1}},
                               {'results': [], 'meta': {'count': True}}, {'results': [None], 'meta': {'count': 1}}])
def test_paper_malformed_or_inconsistent_page_is_not_zero(data):
    with pytest.raises(HttpError):
        openalex_page(wire(data), 'Synthetic')


def test_paper_empty_complete_page_and_basic_pagination_limit():
    assert not openalex_page(wire({'results': [], 'meta': {'count': 0}}), 'Synthetic')['truncated']
    capped = openalex_page(wire({'results': [work()], 'meta': {'count': 10001}}),
                           'Synthetic', page=10000, per_page=1)
    assert capped['truncated'] and capped['next_cursor'] is None
    with pytest.raises(ValueError):
        openalex_page(wire({}), 'Synthetic', page=10001, per_page=1)


def test_clinical_intervention_route_and_distinct_dates():
    calls = []
    result = clinical_page(wire({'studies': [study()], 'nextPageToken': 'next-page'}, calls),
                           'Synthetic Intervention', mode='term')
    assert calls[0]['query.term'] == ['Synthetic Intervention']
    assert 'query.spons' not in calls[0]
    item = result['items'][0]
    assert item['published_at'].startswith('2024-01-02')
    assert item['extra']['source_updated_at'] == '2026-10-20'
    assert item['extra']['sponsor'] == 'Synthetic Hospital'
    assert result['truncated'] and result['next_cursor'] == 'next-page'


def test_clinical_token_loop_and_missing_studies_are_failures():
    for data in ({}, {'studies': [study()], 'nextPageToken': 'old-token'}):
        with pytest.raises(HttpError):
            clinical_page(wire(data), 'Synthetic', cursor='old-token')
    assert not clinical_page(wire({'studies': []}), 'Synthetic')['truncated']


def test_host_follows_pages_and_checkpoint_replay_dispatches_nothing(tmp_path):
    research = fixtures.loop.__wrapped__(tmp_path)
    execution = research.execution
    calls = []

    def send(_method, url, *_args):
        page = int(parse_qs(urlsplit(url).query)['page'][0])
        calls.append(page)
        rows = [work(n) for n in range(1, 21)] if page == 1 else [work(21)]
        return Response(200, {}, json.dumps({'results': rows, 'meta': {'count': 21}}).encode())

    collectors = Collectors(execution.http(Http(send), lambda _: ('open_data', 'synthetic-openalex')), SearchConfig())
    adapter = CollectionSources(execution, collectors)
    unit = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])['units'][0] | {'source': 'openalex_topic'}
    first = adapter.collect(unit)
    second = adapter.collect(unit)
    assert calls == [1, 2]
    assert len(first['items']) == 21
    assert first['items'] == second['items']
    assert not first['truncated'] and first['pages_completed'] == 2
    assert second['request_ids'] == first['request_ids']
    assert execution.usage()['open_data']['requests'] == 2


def test_page_budget_keeps_real_continuation(tmp_path):
    research = fixtures.loop.__wrapped__(tmp_path)
    execution = research.execution
    http = execution.http(wire({'results': [work(n) for n in range(20)], 'meta': {'count': 40}}),
                          lambda _: ('open_data', 'synthetic-openalex'))
    adapter = CollectionSources(execution, Collectors(http, SearchConfig()), max_pages=1)
    unit = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])['units'][0] | {'source': 'openalex_topic'}
    result = adapter.collect(unit)
    assert result['truncated'] and result['next_cursor'] == 2
    assert len(result['items']) == 20


def test_crash_between_page_and_trace_checkpoint_keeps_request_provenance(tmp_path, monkeypatch):
    research = fixtures.loop.__wrapped__(tmp_path)
    execution, calls = research.execution, []
    http = execution.http(wire({'results': [work()], 'meta': {'count': 1}}, calls),
                          lambda _: ('open_data', 'synthetic-openalex'))
    adapter = CollectionSources(execution, Collectors(http, SearchConfig()))
    unit = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])['units'][0] | {'source': 'openalex_topic'}
    original = execution.checkpoint_chunked

    def crash(stage, payload):
        if stage.startswith('collection_trace:'):
            raise RuntimeError('synthetic crash before trace')
        return original(stage, payload)

    monkeypatch.setattr(execution, 'checkpoint_chunked', crash)
    with pytest.raises(RuntimeError, match='synthetic crash'):
        adapter.collect(unit)
    restarted = JobExecution(execution.store, execution.job)
    recovered = CollectionSources(restarted, Collectors(
        restarted.http(wire({}, calls), lambda _: ('open_data', 'synthetic-openalex')), SearchConfig()))
    result = recovered.collect(unit)
    assert len(calls) == 1
    assert len(result['request_ids']) == len(result['items']) == 1
    assert not result['truncated']
    assert restarted.usage()['open_data']['requests'] == 1


def test_clinical_cycle_is_detected_before_repeated_page_dispatch(tmp_path):
    research = fixtures.loop.__wrapped__(tmp_path)
    execution, calls = research.execution, []

    def send(_method, url, *_args):
        token = parse_qs(urlsplit(url).query).get('pageToken', [None])[0]
        calls.append(token)
        next_token = {None: 'a', 'a': 'b', 'b': 'a'}[token]
        return Response(200, {}, json.dumps({'studies': [study()], 'nextPageToken': next_token}).encode())

    http = execution.http(Http(send), lambda _: ('open_data', 'synthetic-clinical'))
    adapter = CollectionSources(execution, Collectors(http, SearchConfig()), max_pages=5)
    unit = plan(round_id=execution.job['id'], project_id=execution.job['project_id'])['units'][0] | {'source': 'clinical_term'}
    result = adapter.collect(unit)
    assert result['outcome'] == 'failed' and result['reason'] == 'collection_cursor_cycle'
    assert result['truncated'] and len(result['items']) == 1
    assert calls == [None, 'a', 'b']
    state = execution.read_checkpoint('collection_pages:' + unit['id'][:40])
    assert state['cursor'] == 'a' and not state['done']
    assert len(state['requests']) == 3


@pytest.mark.parametrize('stop,expected', [('budget', 'budget_deferred'), ('malformed', 'failed'),
                                         ('unknown', 'unknown')])
def test_partial_pages_survive_later_interruption_without_false_success(tmp_path, stop, expected):
    research = fixtures.loop.__wrapped__(tmp_path)
    execution, calls, saved = research.execution, [], []
    execution.budgets['open_data'] = 1 if stop == 'budget' else 5

    def send(_method, url, *_args):
        page = int(parse_qs(urlsplit(url).query)['page'][0])
        calls.append(page)
        if page == 2:
            if stop == 'unknown':
                raise HttpError('connection_failed')
            return Response(200, {}, b'{}')
        return Response(200, {}, json.dumps({'results': [work(n) for n in range(20)],
                                           'meta': {'count': 40}}).encode())

    p = plan([candidate() | {'source': 'openalex_topic'}], round_id=execution.job['id'], project_id=execution.job['project_id'])
    ledger = CollectionLedger(execution, p)
    adapter = CollectionSources(execution, Collectors(execution.http(
        Http(send), lambda _: ('open_data', 'synthetic-openalex')), SearchConfig()))

    def ingest(_unit, items):
        saved.extend(items)
        return len(items)

    for _ in range(2):
        if stop == 'unknown':
            with pytest.raises(HttpError):
                run_collection(ledger, collect=adapter.collect, ingest=ingest, guard=research.guard)
        else:
            run_collection(ledger, collect=adapter.collect, ingest=ingest, guard=research.guard)
    result = ledger.coverage()
    assert result['status'] == 'partial' and result['rows'][0]['outcome'] == expected
    assert calls == ([1] if stop == 'budget' else [1, 2])
    if stop == 'unknown':
        assert saved == []
        assert execution.usage()['open_data']['unknown'] == 1
    else:
        assert len(saved) == result['rows'][0]['saved_count'] == 20
        assert ledger.receipts[0]['next_cursor'] == 2
    checkpoint = execution.read_checkpoint('collection_pages:' + p['units'][0]['id'][:40])
    assert len(checkpoint['items']) == 20 and checkpoint['cursor'] == 2
