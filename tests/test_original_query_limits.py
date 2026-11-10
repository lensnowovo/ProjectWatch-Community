"""Offline regression for long-title original discovery and source isolation."""
import json
from types import SimpleNamespace

import pytest
from test_studio_original_lookup import hit, setup

from project_watch.studio.article_lookup import matches_original, title_query
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import SearchConfig
from project_watch.studio.sources import Collectors


@pytest.mark.parametrize('size', [126, 127, 128, 132, 133, 200, 400])
def test_deepseek_original_query_fits_but_original_identity_is_retained(size):
    title = '合成公司' + '新产品技术进展' * size
    lead = hit('blocked.test', title)
    sent = []
    def transport(method, url, headers, body, timeout):
        payload = json.loads(body)
        sent.append(payload['query'])
        assert 1 <= len(payload['query']) <= 128
        return Response(200, {}, json.dumps({'results': []}).encode())
    collectors = Collectors(Http(transport), SearchConfig('deepseek', 'synthetic'))
    assert collectors.original_search(lead) == []
    assert len(sent) == 1 and lead['title'] == title


def test_full_title_conflict_beyond_retrieval_hint_is_rejected():
    prefix = 'Synthetic Corporation announces a major financing for product development ' * 4
    title = prefix + 'USD 100 million Series B financing'
    other = prefix + 'USD 200 million Series B financing'
    assert title_query(title, max_chars=128) == title_query(other, max_chars=128)
    assert not matches_original(hit('blocked.test', title), hit('publisher.test', other))
    assert matches_original(hit('blocked.test', title), hit('publisher.test', title))


def test_general_queries_are_not_silently_truncated():
    sent = []
    collectors = Collectors(Http(lambda *args: sent.append(args)), SearchConfig('deepseek', 'synthetic'))
    with pytest.raises(HttpError, match='search_query_invalid'):
        collectors.web_search('site:synthetic.example ' + 'long query ' * 30)
    assert not sent


@pytest.mark.parametrize('code', ['search_query_invalid', 'request_outcome_unknown', 'provider_backoff'])
def test_invalid_lookup_keeps_next_original_but_unknown_still_stops(tmp_path, code):
    pipeline, pid, reads, execution = setup(tmp_path)
    def read(url):
        reads.append(url)
        if 'blocked.test' in url:
            return {'status': 'summary_only', 'text': '', 'error_code': 'access_restricted'}
        return {'status': 'article_body', 'text': 'Synthetic Beta builds Organ B for drug screening.'}
    pipeline.article_reader = SimpleNamespace(read=read)
    def search(*args, **kwargs):
        raise HttpError(code)
    report = []
    def run():
        return pipeline.competition_research.originals(pid, [hit('blocked.test'), hit('publisher.test')],
            execution=execution, collectors=SimpleNamespace(web_search=search), report=report)
    if code == 'search_query_invalid':
        assert len(run()) == 1
        assert report[0]['lookup_error_code'] == code
        assert 'https://publisher.test/report' in reads
    else:
        with pytest.raises(HttpError, match=code):
            run()
        assert 'https://publisher.test/report' not in reads


def test_invalid_query_cannot_hide_an_unsettled_request(tmp_path, monkeypatch):
    pipeline, pid, reads, execution = setup(tmp_path)
    pipeline.article_reader = SimpleNamespace(read=lambda url: reads.append(url) or {
        'status': 'summary_only', 'text': '', 'error_code': 'access_restricted'})
    def search(*args, **kwargs):
        monkeypatch.setattr(execution, 'usage', lambda: {'search': {'unknown': 1}})
        raise HttpError('search_query_invalid')
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        pipeline.competition_research.originals(pid, [hit('blocked.test'), hit('publisher.test')],
            execution=execution, collectors=SimpleNamespace(web_search=search))
    assert 'https://publisher.test/report' not in reads


def test_changed_query_cannot_send_around_old_unknown(tmp_path, monkeypatch):
    pipeline, _, _, execution = setup(tmp_path)
    monkeypatch.setattr(execution, 'usage', lambda: {'search': {'unknown': 1}})
    sent = []
    collectors = SimpleNamespace(original_search=lambda *args, **kwargs: sent.append(1) or [])
    with pytest.raises(HttpError, match='request_outcome_unknown'):
        pipeline.competition_research._original_lookup(hit('blocked.test', 'long title ' * 30), execution, collectors, None)
    assert not sent


def test_cached_lookup_is_rechecked_against_full_title(tmp_path):
    pipeline, _, _, execution = setup(tmp_path)
    prefix = 'Synthetic Corporation announces financing for product development ' * 5
    lead = hit('blocked.test', prefix + 'USD 100 million Series B financing')
    other = hit('blocked.test', prefix + 'USD 200 million Series B financing')
    calls = []
    collectors = SimpleNamespace(original_search=lambda *args, **kwargs:
        calls.append(1) or [hit('publisher.test', lead['title'])])
    lookup = pipeline.competition_research._original_lookup
    assert len(lookup(lead, execution, collectors, None)) == 1
    assert lookup(other, execution, collectors, None) == []
    assert calls == [1]
