"""News source defaults (v30): readable direct links first; Google News opt-in."""

from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import SearchConfig, SettingsStore
from project_watch.studio.sources import Collectors

BING = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>b</title>
<item><title>Peer wins order</title><link>https://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fpub.example%2fa</link>
<description>direct</description><pubDate>Tue, 06 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>"""
GOOGLE = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>g</title>
<item><title>Peer wins order - Outlet</title><link>https://news.google.com/rss/articles/x</link>
<guid>x</guid><pubDate>Tue, 06 Oct 2026 08:00:00 GMT</pubDate><description>x</description></item></channel></rss>"""


def collectors(config, calls, *, bing_status=200):
    def transport(method, url, headers, body, timeout):
        calls.append(url.split('?')[0])
        if 'bing.com/news' in url:
            return Response(bing_status, {}, BING if bing_status == 200 else b'')
        if 'news.google.com' in url:
            return Response(200, {}, GOOGLE)
        raise HttpError('connection_failed')
    return Collectors(Http(transport), config)


def test_default_news_uses_bing_direct_links_and_never_google():
    calls = []
    items = collectors(SearchConfig(), calls).news('Peer')
    assert [i['url'] for i in items] == ['https://pub.example/a']
    assert not any('google' in c for c in calls)


def test_google_news_only_when_enabled_and_a_failing_source_is_tolerated():
    calls = []
    items = collectors(SearchConfig(google_news=True), calls, bing_status=503).news('Peer')
    assert [i['url'] for i in items] == ['https://news.google.com/rss/articles/x']


def test_settings_toggle_round_trips(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    assert store.load().search.google_news is False
    store.update({'search': {'google_news': True}})
    assert store.load().search.google_news is True
    assert SettingsStore.public_view(store.load())['search']['google_news'] is True


def test_a_held_search_provider_does_not_hide_bing_results():
    """R46: Bocha quota exhausted put the provider on hold; Bing News still answered."""
    calls = []
    c = collectors(SearchConfig(providers=['bocha'], keys={'bocha': 'synthetic'}, mode='auto'), calls)

    def held(*_a, **_k):
        raise HttpError('provider_backoff')
    c.web_search = held
    assert [i['url'] for i in c.news('Peer', search_count=4)] == ['https://pub.example/a']


def test_unknown_search_outcome_still_stops_news():
    import pytest

    c = collectors(SearchConfig(providers=['bocha'], keys={'bocha': 'synthetic'}, mode='auto'), [])

    def unknown(*_a, **_k):
        raise HttpError('request_outcome_unknown')
    c.web_search = unknown
    with pytest.raises(HttpError):
        c.news('Peer', search_count=4)


def test_collection_continues_past_a_held_provider(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / 'db.sqlite3')
    project = repo.create_project('合成公司')
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {'legal_name': '合成公司'}, consent=True)
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / 's.json'))

    class Held:
        has_search = True
        search_config = SearchConfig()
        search_checks: tuple = ()

        def bing_news(self, query):
            return [{'key': query, 'title': f'{query} 新闻', 'url': f'https://pub.example/{abs(hash(query))}',
                     'source': 'pub', 'snippet': '', 'published_at': None, 'extra': {}}]

        def web_search(self, *_a, **_k):
            raise HttpError('provider_backoff')

        def news(self, query, *, search_count=0, limit=30):
            return self.bing_news(query)

        has_registry = False

        def openalex(self, *_a, **_k):
            return []

        def cninfo(self, *_a, **_k):
            raise HttpError('provider_backoff')

    plan = {'company_queries': ['合成公司', '合成公司 融资'], 'competitors': [{'name': '对手公司'}],
            'industry_queries': ['合成赛道'], 'policy_queries': []}
    report = pipeline.collect(project.id, plan, Held(), lambda _: None, store.get_project(project.id))
    assert report['company']['added'] >= 2 and report['competitor']['added'] == 1
    assert any('provider_backoff' in e for e in report['company']['errors'])
