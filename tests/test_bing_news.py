from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from project_watch.bing_news import (
    BingNewsAdapter,
    bing_news_target,
    is_bing_news_target,
    parse_bing_feed,
    resolve_bing,
)
from project_watch.feeds import FeedError


def rss(link, *, title='Synthetic connector update', date='Mon, 05 Oct 2026 01:00:00 GMT'):
    import xml.etree.ElementTree as ET
    root = ET.Element('rss')
    item = ET.SubElement(ET.SubElement(root, 'channel'), 'item')
    for key, value in dict(title=title, link=link, description='Public abstract', pubDate=date).items():
        ET.SubElement(item, key).text = value
    return ET.tostring(root)


def wrapper(url):
    return 'http://www.bing.com/news/apiclick.aspx?' + urlencode({'ref':'FexRss', 'url':url, 'tid':'transient'})


def test_canonical_query_and_no_arbitrary_endpoint():
    target = bing_news_target('连接器')
    assert is_bing_news_target(target)
    for value in [target+'&url=https://localhost/', target.replace('www.bing.com', 'evil.example'),
                  target.replace('https:', 'http:'), target+'#x']:
        assert not is_bing_news_target(value)
    with pytest.raises(ValueError):
        bing_news_target('a" OR confidential')


def test_chronological_variant_is_fixed_same_query_and_adapter_only():
    target = bing_news_target('Synthetic connector', recent=True)
    assert target.endswith('&sortbydate=1')
    assert not is_bing_news_target(target)  # Not a new user-authorized plan target.
    assert is_bing_news_target(target, allow_recent=True)
    for value in [target+'&q=other', target+'&url=https://localhost/', target.replace('sortbydate=1','sortbydate=2')]:
        assert not is_bing_news_target(value, allow_recent=True)
    calls = []
    adapter = BingNewsAdapter(resolver=lambda _:['93.184.215.14'], connector=lambda *args:(calls.append(args) or (200,{},b'<rss><channel/></rss>')))
    assert adapter.fetch(target).unavailable_error is None
    assert len(calls) == 1 and calls[0][2].endswith('&sortbydate=1')


def test_unwrap_display_only_link_and_stable_id():
    url = 'https://news.example.org/releases/1'
    first = parse_bing_feed(rss(wrapper(url)))[0]
    second = parse_bing_feed(rss(wrapper(url).replace('transient', 'other')))[0]
    assert first.url == url
    assert first.external_id == second.external_id == url
    assert first.published_at == datetime(2026, 10, 5, 1, tzinfo=UTC)


@pytest.mark.parametrize('url', ['http://news.example.org/a', 'https://localhost/a',
    'https://127.0.0.1/a', 'https://10.0.0.1/a', 'file:///secret', 'javascript:alert(1)',
    'https://u:p@example.org/a'])
def test_unsafe_article_targets_rejected_without_fetch(url):
    with pytest.raises(FeedError, match='no_usable_items'):
        parse_bing_feed(rss(wrapper(url)))


def test_wrong_wrapper_and_duplicate_target_rejected():
    for link in ['http://evil.example/news/apiclick.aspx?url=https://example.org/a',
                 wrapper('https://example.org/a')+'&url=https://other.example/b']:
        with pytest.raises(FeedError):
            parse_bing_feed(rss(link))


def test_fake_dns_fallback_is_fixed_host_only_and_revalidates():
    calls = []
    def public(host):
        calls.append(host)
        return ['93.184.215.14']
    assert resolve_bing('www.bing.com', resolver=lambda _:['198.18.0.31'], public_dns=public) == ['93.184.215.14']
    assert calls == ['www.bing.com']
    for host, answers in [('localhost',['198.18.0.1']), ('www.bing.com',['127.0.0.1']),
                          ('www.bing.com',['198.18.0.1','127.0.0.1'])]:
        with pytest.raises(FeedError):
            resolve_bing(host, resolver=lambda _, answers=answers:answers, public_dns=public)
    with pytest.raises(FeedError):
        resolve_bing('www.bing.com', resolver=lambda _:['198.18.0.1'], public_dns=lambda _:['10.0.0.1'])


def test_adapter_no_redirect_and_sanitized_failure():
    calls = []
    def connector(*args):
        calls.append(args)
        return 302, {'location':'https://example.org/a'}, b''
    adapter = BingNewsAdapter(resolver=lambda _:['93.184.215.14'], connector=connector)
    assert adapter.fetch(bing_news_target('Synthetic')).unavailable_error == 'redirect_not_allowed'
    assert len(calls) == 1
    assert adapter.fetch('https://localhost/').unavailable_error == 'disallowed_url'
    assert len(calls) == 1


def test_empty_valid_feed_differs_from_invalid_response():
    assert parse_bing_feed(b'<rss><channel/></rss>') == ()
    with pytest.raises(FeedError):
        parse_bing_feed(b'<html/>')
    with pytest.raises(FeedError):
        parse_bing_feed(b'<!DOCTYPE rss><rss/>')
