from datetime import UTC
from urllib.parse import urlsplit

import pytest

from project_watch.feeds import (
    FeedError,
    GdeltProxyTransport,
    GuardedHTTPS,
    PublicFeedAdapter,
    RoutedFeedTransport,
    parse_feed,
)
from project_watch.public_search import gdelt_rss_target


def test_fixed_feed_proxy_requires_exact_operator_configured_address():
    from project_watch.feeds import FixedFeedProxyTransport, configured_public_feed_adapter

    target = 'https://public.example.org/releases.xml'
    calls = []
    fixed = FixedFeedProxyTransport('http://127.0.0.1:17891', (target,), connector=lambda *args: (
        calls.append(args) or 200, {}, RSS))
    direct = GuardedHTTPS(resolver=lambda host: ['198.18.0.20'])
    adapter = PublicFeedAdapter(RoutedFeedTransport(direct=direct, fixed_feed_proxy=fixed))
    assert len(adapter.fetch(target).items) == 1
    assert len(calls) == 1
    assert adapter.fetch(target+'?private=1').unavailable_error == 'disallowed_address'
    assert adapter.fetch('https://public.example.org/other').unavailable_error == 'disallowed_address'
    assert len(calls) == 1
    with pytest.raises(FeedError, match='disallowed_url'):
        fixed.get(target+'?private=1')
    with pytest.raises(ValueError):
        configured_public_feed_adapter(None, fixed_feed_targets=(target,))


@pytest.mark.parametrize('target', ['https://127.0.0.1/rss', 'https://intranet.local/rss',
    'https://user:pass@example.org/rss', 'https://example.org/rss?key=secret',
    'https://example.org/rss#fragment', 'https://example.org/a/../rss',
    'https://example.org/rss\r\nX-Test: value', 'https://example.org/%2e%2e/admin'])
def test_fixed_feed_proxy_configuration_rejects_unsafe_or_ambiguous_targets(target):
    from project_watch.feeds import FixedFeedProxyTransport
    with pytest.raises(ValueError):
        FixedFeedProxyTransport('http://127.0.0.1:17891', (target,))


@pytest.mark.parametrize('status,expected', [(302,'redirect_not_allowed'), (429,'rate_limited'), (403,'http_failure')])
def test_fixed_feed_proxy_keeps_redirect_and_error_guards(status, expected):
    from project_watch.feeds import FixedFeedProxyTransport
    target = 'https://public.example.org/releases.xml'
    proxy = FixedFeedProxyTransport('http://127.0.0.1:17891', (target,),
        connector=lambda *args: (status, {'location':'https://localhost/'}, b''))
    assert PublicFeedAdapter(proxy).fetch(target).unavailable_error == expected


def test_fixed_feed_configuration_parser_fails_closed():
    from project_watch.feeds import parse_fixed_feed_targets
    assert parse_fixed_feed_targets(None) == ()
    assert parse_fixed_feed_targets('["https://example.org/rss"]') == ('https://example.org/rss',)
    for value in ['broken', '{}', '"https://example.org/rss"', '[42]', '[]']:
        with pytest.raises(ValueError):
            parse_fixed_feed_targets(value)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Synthetic</title>
<item><guid>news-1</guid><title>Product update</title>
<link>https://example.org/news/1</link><description>Test description</description>
<pubDate>Fri, 25 Sep 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>entry-1</id><title>Company update</title>
<link href="https://example.org/news/2"/>
<summary>Test atom summary</summary><updated>2026-09-25T08:00:00Z</updated></entry>
</feed>"""


@pytest.mark.parametrize("fixture", [RSS, ATOM])
def test_parse_public_feed_into_bounded_items(fixture: bytes) -> None:
    items = parse_feed(fixture)

    assert len(items) == 1
    assert items[0].title in {"Product update", "Company update"}
    assert items[0].url.startswith("https://example.org/news/")
    assert items[0].published_at.tzinfo == UTC


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/rss",
        "https://localhost/rss",
        "https://127.0.0.1/rss",
        "https://10.1.1.1/rss",
        "https://example.org:8443/rss",
        "https://user:pass@example.org/rss",
    ],
)
def test_guarded_transport_rejects_disallowed_urls(url: str) -> None:
    transport = GuardedHTTPS(resolver=lambda host: ["93.184.215.14"], connector=None)

    with pytest.raises(FeedError, match="disallowed_url"):
        transport.get(url)


def test_guarded_transport_rejects_private_dns_answer() -> None:
    transport = GuardedHTTPS(resolver=lambda host: ["192.168.1.9"], connector=None)

    with pytest.raises(FeedError, match="disallowed_address"):
        transport.get("https://example.org/feed")


def test_redirect_is_revalidated_before_second_request() -> None:
    calls = []

    def connector(host, ip, path):
        calls.append((host, ip, path))
        return 302, {"location": "https://localhost/private"}, b""

    transport = GuardedHTTPS(resolver=lambda host: ["93.184.215.14"], connector=connector)

    with pytest.raises(FeedError, match="disallowed_url"):
        transport.get("https://example.org/feed")
    assert calls == [("example.org", "93.184.215.14", "/feed")]


def test_mixed_public_and_private_dns_answers_are_rejected() -> None:
    transport = GuardedHTTPS(resolver=lambda host: ["93.184.215.14", "127.0.0.1"], connector=None)

    with pytest.raises(FeedError, match="disallowed_address"):
        transport.get("https://example.org/feed")


def test_same_host_redirect_revalidates_dns_before_next_connection() -> None:
    answers = iter((["93.184.215.14"], ["127.0.0.1"]))
    calls = []

    def connector(host, ip, path):
        calls.append((host, ip, path))
        return 302, {"location": "/next"}, b""

    transport = GuardedHTTPS(resolver=lambda host: next(answers), connector=connector)
    with pytest.raises(FeedError, match="disallowed_address"):
        transport.get("https://example.org/feed")
    assert calls == [("example.org", "93.184.215.14", "/feed")]


def test_feed_discards_nonpublic_article_links_without_resolving_them() -> None:
    data = b"""<rss><channel>
    <item><guid>private</guid><title>Private</title><link>https://127.0.0.1/admin</link></item>
    <item><guid>creds</guid><title>Credentials</title><link>https://user:pass@example.org/post</link></item>
    <item><guid>port</guid><title>Alternate port</title><link>https://example.org:8443/post</link></item>
    <item><guid>public</guid><title>Public</title><link>https://example.org/post</link></item>
    </channel></rss>"""

    items = parse_feed(data)
    assert [item.external_id for item in items] == ["public"]


def test_feed_with_only_unusable_links_is_unreachable_not_empty() -> None:
    data = b"""<rss><channel>
    <item><guid>private</guid><title>Private</title><link>https://127.0.0.1/admin</link></item>
    </channel></rss>"""
    result = PublicFeedAdapter(
        GuardedHTTPS(
            resolver=lambda host: ["93.184.215.14"],
            connector=lambda host, ip, path: (200, {}, data),
        )
    ).fetch("https://example.org/feed")

    assert result.items == ()
    assert result.unavailable_error == "no_usable_items"


def test_oversized_or_entity_feed_is_unreachable_not_empty() -> None:
    transport = GuardedHTTPS(
        resolver=lambda host: ["93.184.215.14"],
        connector=lambda host, ip, path: (200, {}, b"x" * (2_000_001)),
    )
    result = PublicFeedAdapter(transport).fetch("https://example.org/feed")
    assert result.unavailable_error == "oversized_feed"

    with pytest.raises(FeedError, match="invalid_feed"):
        parse_feed(b'<!DOCTYPE x [<!ENTITY a "boom">]><rss><channel/></rss>')


def test_empty_feed_is_no_results_and_malformed_feed_is_unreachable() -> None:
    empty = PublicFeedAdapter(
        GuardedHTTPS(
            resolver=lambda host: ["93.184.215.14"],
            connector=lambda host, ip, path: (200, {}, b"<rss><channel/></rss>"),
        )
    ).fetch("https://example.org/feed")
    malformed = PublicFeedAdapter(
        GuardedHTTPS(
            resolver=lambda host: ["93.184.215.14"],
            connector=lambda host, ip, path: (200, {}, b"broken xml"),
        )
    ).fetch("https://example.org/feed")

    assert empty.items == () and empty.unavailable_error is None
    assert malformed.unavailable_error == "invalid_feed"


def test_direct_https_429_is_reported_as_rate_limited() -> None:
    adapter = PublicFeedAdapter(
        GuardedHTTPS(
            resolver=lambda host: ["93.184.215.14"],
            connector=lambda host, ip, path: (429, {"retry-after": "60"}, b""),
        )
    )

    assert adapter.fetch(gdelt_rss_target("Synthetic Company")).unavailable_error == "rate_limited"


def test_proxy_only_routes_canonical_gdelt_and_preserves_direct_dns_guard() -> None:
    calls = []

    def proxy_connector(proxy_host, proxy_port, host, path):
        calls.append((proxy_host, proxy_port, host, path))
        return 200, {}, RSS

    direct = GuardedHTTPS(
        resolver=lambda host: ["198.18.0.20"],
        connector=lambda host, ip, path: (200, {}, RSS),
    )
    proxy = GdeltProxyTransport("http://127.0.0.1:17891", connector=proxy_connector)
    adapter = PublicFeedAdapter(RoutedFeedTransport(direct=direct, gdelt_proxy=proxy))
    target = gdelt_rss_target("Synthetic Company")
    assert len(adapter.fetch(target).items) == 1
    assert calls == [
        (
            "127.0.0.1",
            17891,
            "api.gdeltproject.org",
            urlsplit(target).path + "?" + urlsplit(target).query,
        )
    ]
    assert adapter.fetch("https://example.org/feed").unavailable_error == "disallowed_address"
    assert adapter.fetch(target + "&extra=1").unavailable_error == "disallowed_address"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "body_size", "expected"),
    [
        (302, 0, "redirect_not_allowed"),
        (403, 0, "http_failure"),
        (429, 0, "rate_limited"),
        (200, 2_000_001, "oversized_feed"),
    ],
)
def test_proxy_fails_closed_on_redirect_error_and_oversize(
    status: int, body_size: int, expected: str
) -> None:
    proxy = GdeltProxyTransport(
        "http://127.0.0.1:17891",
        connector=lambda proxy_host, proxy_port, host, path: (
            status,
            {"location": "https://localhost/private"},
            b"x" * body_size,
        ),
    )
    assert (
        PublicFeedAdapter(proxy).fetch(gdelt_rss_target("Synthetic Company")).unavailable_error
        == expected
    )


def test_proxy_connection_failure_never_falls_back_to_direct() -> None:
    def unavailable(*args):
        raise OSError("synthetic proxy outage")

    proxy = GdeltProxyTransport("http://127.0.0.1:17891", connector=unavailable)
    direct = GuardedHTTPS(
        resolver=lambda host: ["93.184.215.14"],
        connector=lambda host, ip, path: (200, {}, RSS),
    )
    adapter = PublicFeedAdapter(RoutedFeedTransport(direct=direct, gdelt_proxy=proxy))
    assert (
        adapter.fetch(gdelt_rss_target("Synthetic Company")).unavailable_error
        == "connection_failed"
    )
