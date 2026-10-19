"""Synthetic passive publisher recovery, never a live coverage benchmark."""

import json
from types import SimpleNamespace

import pytest
from test_studio_competition_research import research_pipeline

from project_watch.studio.competition_research import read_page
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError

TITLE = 'Synthetic Beta announces Organ B for drug screening'


def setup(tmp_path, *, budgets=None):
    pipeline, pid, _, reads = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    execution = JobExecution(pipeline.store, pipeline.store.claim(), budgets=budgets)
    return pipeline, pid, reads, execution


def hit(host, title=TITLE, date='2026-01-01'):
    return {'url': f'https://{host}/report', 'title': title, 'snippet': 'lead only',
            'published_at': date}


@pytest.mark.parametrize('error', ['insufficient_article_text', 'access_restricted',
                                 'unsupported_media_type', 'connection_failed'])
def test_fixed_reader_reason_is_durable_and_arbitrary_diagnostics_are_omitted(tmp_path, error):
    _, _, reads, execution = setup(tmp_path)
    reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'summary_only', 'text': '', 'error_code': error,
        'headers': {'authorization': 'secret'}, 'exception': 'private diagnostic'})
    result = read_page(execution, reader, 'https://blocked.test/report')
    assert result['error_code'] == error
    assert 'secret' not in json.dumps(result) and 'diagnostic' not in json.dumps(result)
    assert read_page(execution, reader, 'https://blocked.test/report') == result
    assert len(reads) == 1


def test_unknown_reader_error_is_not_persisted_verbatim(tmp_path):
    _, _, _, execution = setup(tmp_path)
    result = read_page(execution, SimpleNamespace(read=lambda _: {
        'status': 'unavailable', 'text': '', 'error_code': 'secret-url-or-token'}),
        'https://blocked.test/report')
    assert result['error_code'] == 'fetch_failed'
    assert 'secret' not in json.dumps(result)


def test_replacement_body_retains_publisher_and_lead_and_replays_without_calls(tmp_path):
    pipeline, pid, reads, execution = setup(tmp_path, budgets={'page': 2})
    queries = []
    def search(query, count):
        queries.append(query)
        return [hit('publisher.test')]
    def read(url):
        reads.append(url)
        return ({'status': 'summary_only', 'text': '', 'error_code': 'access_restricted'}
                if 'blocked.test' in url else {'status': 'article_body',
                    'text': 'Beta builds Organ B for drug screening.',
                    'resolved_url': 'https://publisher.test/original'})
    pipeline.article_reader = SimpleNamespace(read=read)
    report = []
    kwargs = dict(execution=execution, collectors=SimpleNamespace(web_search=search), report=report)
    evidence = pipeline.competition_research.originals(pid, [hit('blocked.test')], **kwargs)
    assert queries == ['"' + TITLE + '"']
    assert reads == ['https://blocked.test/report', 'https://publisher.test/report']
    assert len(evidence) == 1 and evidence[0]['source_type'] == 'public_page'
    assert report[0]['error_code'] == 'access_restricted'
    assert report[1]['discovery_url'] == 'https://blocked.test/report'
    assert report[1]['resolved_url'] == 'https://publisher.test/original'
    assert evidence[0]['parts'][0]['locator'] == 'https://publisher.test/original'
    before = list(report)
    report.clear()
    assert pipeline.competition_research.originals(pid, [hit('blocked.test')], **kwargs) == evidence
    assert report == before and len(queries) == 1 and len(reads) == 2


@pytest.mark.parametrize('bad', [
    hit('blocked.test'), hit('sub.blocked.test'), hit('publisher.test', 'Completely different story'),
    hit('publisher.test', date='2023-01-01'),
    {'url': 'https://user:password@publisher.test/report', 'title': TITLE},
    {'url': 'http://publisher.test/report', 'title': TITLE},
    hit('news.google.com'),
    hit('publisher.test', 'Synthetic Beta raises USD 200 million in Series B'),
])
def test_wrong_or_unsafe_replacements_are_not_read(tmp_path, bad):
    pipeline, pid, reads, execution = setup(tmp_path)
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'summary_only', 'text': '', 'error_code': 'insufficient_article_text'})
    result = pipeline.competition_research.originals(pid, [hit('blocked.test')], execution=execution,
        collectors=SimpleNamespace(web_search=lambda *a, **kw: [bad]))
    assert result == [] and reads == ['https://blocked.test/report']


def test_three_page_bound_includes_donors_and_global_lookup_limit_survives_replay(tmp_path):
    pipeline, pid, reads, execution = setup(tmp_path)
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'summary_only', 'text': '', 'error_code': 'insufficient_article_text'})
    searches = []
    collectors = SimpleNamespace(web_search=lambda q, **kw: searches.append(q) or [
        hit('donor-a.test'), hit('donor-b.test'), hit('donor-c.test')])
    leads = [hit(f'blocked-{i}.test', TITLE + str(i)) for i in range(7)]
    for lead in leads:
        pipeline.competition_research.originals(pid, [lead], execution=execution, collectors=collectors)
    assert len(searches) == 6
    assert len(reads) <= 21
    # Each company has at most one lookup, even when both replacements also fail.
    before = len(searches), len(reads)
    for lead in leads:
        pipeline.competition_research.originals(pid, [lead], execution=execution, collectors=collectors)
    assert (len(searches), len(reads)) == before


def test_unknown_lookup_does_not_fall_through_or_resend(tmp_path):
    pipeline, pid, reads, execution = setup(tmp_path)
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'summary_only', 'text': '', 'error_code': 'insufficient_article_text'})
    calls = []
    def search(*args, **kwargs):
        def lost():
            calls.append('accepted')
            raise HttpError('request_outcome_unknown')
        return execution.call('search', 'lost-search', lost)
    for _ in range(2):
        with pytest.raises(HttpError, match='request_outcome_unknown'):
            pipeline.competition_research.originals(pid, [hit('blocked.test')], execution=execution,
                collectors=SimpleNamespace(web_search=search))
    assert calls == ['accepted'] and len(reads) == 1


def test_permission_changed_by_lookup_stops_before_donor_read(tmp_path):
    pipeline, pid, reads, execution = setup(tmp_path)
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'summary_only', 'text': '', 'error_code': 'insufficient_article_text'})
    def guard():
        if pipeline.store.get_project(pid)['status'] != 'active':
            raise ValueError('permission changed')
    def search(*args, **kwargs):
        pipeline.store.set_status(pid, 'paused')
        return [hit('publisher.test')]
    with pytest.raises(ValueError, match='permission changed'):
        pipeline.competition_research.originals(pid, [hit('blocked.test')], execution=execution,
            collectors=SimpleNamespace(web_search=search), guard=guard)
    assert reads == ['https://blocked.test/report']


def test_no_search_spend_when_page_budget_is_already_exhausted(tmp_path):
    pipeline, pid, reads, execution = setup(tmp_path, budgets={'page': 1})
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'summary_only', 'text': '', 'error_code': 'insufficient_article_text'})
    def search(*args, **kwargs):
        raise AssertionError('no reading capacity: do not spend on a title lookup')
    assert pipeline.competition_research.originals(pid, [hit('blocked.test')], execution=execution,
        collectors=SimpleNamespace(web_search=search)) == []
    assert len(reads) == 1


def test_similar_title_with_conflicting_financing_amount_is_not_a_donor():
    from project_watch.studio.article_lookup import matches_original
    title = 'Synthetic Beta announces USD 100 million Series B financing for drug screening product development'
    assert not matches_original(hit('blocked.test', title),
        hit('publisher.test', title.replace('100', '200')), different_publisher=True)
    assert matches_original(hit('blocked.test', title), hit('publisher.test', title), different_publisher=True)


def test_empty_completed_lookup_is_not_repeated(tmp_path):
    pipeline, pid, reads, execution = setup(tmp_path)
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'summary_only', 'text': '', 'error_code': 'insufficient_article_text'})
    queries = []
    collectors = SimpleNamespace(web_search=lambda q, **kw: queries.append(q) or [])
    for _ in range(2):
        assert pipeline.competition_research.originals(pid, [hit('blocked.test')], execution=execution,
            collectors=collectors) == []
    assert len(queries) == 1 and len(reads) == 1


def test_short_directory_title_does_not_consume_company_lookup(tmp_path):
    pipeline, pid, reads, execution = setup(tmp_path, budgets={'page': 3})
    def read(url):
        reads.append(url)
        return ({'status': 'article_body', 'text': 'Beta builds Organ B for drug screening.'}
                if 'publisher.test' in url else {'status': 'summary_only', 'text': '',
                    'error_code': 'access_restricted'})
    pipeline.article_reader = SimpleNamespace(read=read)
    queries = []
    collectors = SimpleNamespace(web_search=lambda q, **kw: queries.append(q) or [hit('publisher.test')])
    leads = [hit('directory.test', 'Organ B'), hit('blocked.test')]
    for _ in range(2):
        result = pipeline.competition_research.originals(pid, leads, execution=execution, collectors=collectors)
        assert len(result) == 1
    assert reads == ['https://directory.test/report', 'https://blocked.test/report', 'https://publisher.test/report']
    assert queries == ['"' + TITLE + '"']
