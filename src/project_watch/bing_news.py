"""Optional public Bing RSS search, not a guaranteed-coverage commercial API.

Only a canonical fixed endpoint is contacted. Article URLs are display-only;
the provider's tracking wrapper is decoded, never followed over HTTP.
"""
import hashlib
import ipaddress
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urlsplit

from project_watch.feeds import (
    MAX_FEED_BYTES,
    MAX_ITEMS,
    FeedError,
    GuardedHTTPS,
    _date,
    _node_text,
    _pinned_request,
    _resolve_public,
    retry_after_seconds,
)
from project_watch.monitoring import AdapterResult, FeedItem
from project_watch.public_search import normalized_public_name


def bing_news_target(term, *, recent=False):
    return 'https://www.bing.com/news/search?' + urlencode({
        'q': normalized_public_name(term), 'format':'rss',
        **({'sortbydate':'1'} if recent else {}),
    })


def is_bing_news_target(target, *, allow_recent=False):
    try:
        p = urlsplit(target)
        values = parse_qs(p.query, strict_parsing=True)
        query = values['q']
        return (p.scheme == 'https' and p.netloc == 'www.bing.com'
                and p.path == '/news/search' and not p.fragment
                and len(query) == 1 and (bing_news_target(query[0]) == target
                    or (allow_recent and bing_news_target(query[0],recent=True) == target)))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def resolve_bing(host, *, resolver=None, public_dns=None):
    if host != 'www.bing.com':
        raise FeedError('disallowed_url')
    from project_watch.model_transport import _public_dns
    try:
        addresses = (resolver or _resolve_public)(host)
        # Handle only the known fake-IP range, not private or mixed DNS answers.
        if addresses and all(ipaddress.ip_address(a) in ipaddress.ip_network('198.18.0.0/15') for a in addresses):
            addresses = (public_dns or _public_dns)(host)
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise FeedError('disallowed_address')
        return addresses
    except FeedError:
        raise
    except Exception as exc:
        raise FeedError('dns_failure') from exc


def _article_url(link):
    p = urlsplit(link)
    if (p.scheme in {'http', 'https'} and p.netloc == 'www.bing.com'
            and p.path == '/news/apiclick.aspx' and not p.fragment):
        urls = parse_qs(p.query).get('url', [])
        if len(urls) != 1:
            raise FeedError('disallowed_url')
        link = urls[0]
    GuardedHTTPS._target(link)
    return link


def parse_bing_feed(data):
    if len(data) > MAX_FEED_BYTES or re.search(rb'<!\s*(?:DOCTYPE|ENTITY)\b', data, re.I):
        raise FeedError('invalid_feed')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError('invalid_feed') from exc
    if root.tag != 'rss' or root.find('channel') is None or sum(1 for _ in root.iter()) > 20_000:
        raise FeedError('invalid_feed')
    entries = root.findall('./channel/item')[:MAX_ITEMS]
    items, seen = [], set()
    for entry in entries:
        try:
            url = _article_url(_node_text(entry.find('link')))
        except (FeedError, ValueError):
            continue
        title = _node_text(entry.find('title'))
        if not title or url in seen:
            continue
        seen.add(url)
        identity = url if len(url) <= 500 else hashlib.sha256(url.encode()).hexdigest()
        items.append(FeedItem(identity, title[:300], url,
                              _node_text(entry.find('description'))[:2000],
                              _date(_node_text(entry.find('pubDate')))))
    if entries and not items:
        raise FeedError('no_usable_items')
    return tuple(items)


def fetch_bing_body(target, *, resolver, connector):
    """Shared direct/host-worker contract: canonical RSS only, no redirects."""
    if not is_bing_news_target(target, allow_recent=True):
        raise FeedError('disallowed_url')
    host, path = GuardedHTTPS._target(target)
    addresses = resolver(host)
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise FeedError('disallowed_address')
    status, headers, body = connector(host, addresses[0], path)
    if len(body) > MAX_FEED_BYTES:
        raise FeedError('oversized_feed')
    if status in {301, 302, 303, 307, 308}:
        raise FeedError('redirect_not_allowed')
    if status == 429:
        raise FeedError('rate_limited', retry_after=retry_after_seconds(headers))
    if status != 200:
        raise FeedError('http_failure')
    return body


class BingNewsAdapter:
    def __init__(self, *, resolver=None, connector=None):
        from project_watch.host_network import configured_host_python
        self.resolver = resolver or resolve_bing
        self.connector = connector or _pinned_request
        self.host_python = configured_host_python() if resolver is None and connector is None else None

    def fetch(self, target):
        if not is_bing_news_target(target, allow_recent=True):
            return AdapterResult(unavailable_error='disallowed_url')
        try:
            if self.host_python:
                from project_watch.host_network import decode_body, exchange
                body = decode_body(exchange(self.host_python, {'version': 1, 'operation': 'bing', 'url': target}))
            else:
                body = fetch_bing_body(target, resolver=self.resolver, connector=self.connector)
            return AdapterResult(items=parse_bing_feed(body))
        except FeedError as exc:
            return AdapterResult(unavailable_error=str(exc), retry_after_seconds=exc.retry_after)
        except Exception:
            return AdapterResult(unavailable_error='connection_failed')


class SearchFeedAdapter:
    """Route canonical Bing queries explicitly, without changing legacy feeds."""
    def __init__(self, legacy, bing=None):
        self.legacy, self.bing = legacy, bing or BingNewsAdapter()

    def fetch(self, target):
        return (self.bing if is_bing_news_target(target, allow_recent=True) else self.legacy).fetch(target)
