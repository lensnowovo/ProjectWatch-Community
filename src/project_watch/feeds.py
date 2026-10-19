"""Bounded public RSS/Atom input. Never fetch intranet or local destinations."""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from project_watch.monitoring import AdapterResult, FeedItem
from project_watch.public_search import is_gdelt_rss_target, parse_loopback_proxy

MAX_FEED_BYTES = 2_000_000
MAX_ITEMS = 500
MAX_REDIRECTS = 3
MAX_CONNECT_ADDRESSES = 3
ATOM_NS = "{http://www.w3.org/2005/Atom}"


class FeedError(RuntimeError):
    """A safe error code that can be exposed in a run receipt."""

    def __init__(self, code, *, retry_after=None, status=None):
        super().__init__(code)
        self.retry_after = retry_after
        self.status = status


class _BeforeRequestFailure(FeedError):
    """Only connection/TLS I/O failed; no HTTP request has been transmitted."""

    def __init__(self, stage):
        super().__init__('connection_failed')
        self.stage = stage


def retry_after_seconds(headers):
    value = next((v for k, v in headers.items() if k.lower() == 'retry-after'), '')
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        try:
            seconds = int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            seconds = 60
    return max(5, min(seconds, 86400))


def _resolve_public(host: str) -> list[str]:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FeedError("dns_failure") from exc
    return list(dict.fromkeys(answer[4][0] for answer in answers))


def _pinned_request(host: str, ip: str, path: str, *, accept: str =
                    "application/rss+xml, application/atom+xml, application/xml, text/xml"
                    ) -> tuple[int, dict[str, str], bytes]:
    class PinnedConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            try:
                raw = socket.create_connection((ip, 443), timeout=5)
            except OSError as exc:
                raise _BeforeRequestFailure('connect') from exc
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=host)
            except ssl.SSLCertVerificationError as exc:
                raw.close()
                # A bad certificate is not an availability retry opportunity.
                raise FeedError('tls_verification_failed') from exc
            except OSError as exc:
                raw.close()
                raise _BeforeRequestFailure('tls') from exc
            except Exception:
                raw.close()
                raise

    connection = PinnedConnection(host, port=443, timeout=5, context=ssl.create_default_context())
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": accept
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_FEED_BYTES + 1)
        return response.status, {key.lower(): value for key, value in response.getheaders()}, body
    finally:
        connection.close()


@dataclass(frozen=True)
class PublicResponse:
    url: str
    headers: dict[str, str]
    body: bytes


class GuardedHTTPS:
    def __init__(
        self,
        *,
        resolver: Callable[[str], list[str]] | None = None,
        connector: Callable[[str, str, str], tuple[int, dict[str, str], bytes]] | None = None,
        accept: str | None = None,
    ) -> None:
        self.resolver = resolver or _resolve_public
        self.connector = connector or (
            (lambda host, ip, path: _pinned_request(host, ip, path, accept=accept))
            if accept else _pinned_request
        )

    @staticmethod
    def _target(url: str) -> tuple[str, str]:
        if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise FeedError("disallowed_url")
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise FeedError("disallowed_url") from exc
        if (
            parsed.scheme != "https"
            or not host
            or not isinstance(url, str)
            or len(url) > 2048
            or parsed.username
            or parsed.password
            or parsed.fragment
            or port not in (None, 443)
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))
            or "." not in host
        ):
            raise FeedError("disallowed_url")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise FeedError("disallowed_url")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return host, path

    def get(self, url: str) -> bytes:
        return self.get_response(url).body

    def get_response(self, url: str, *, allow_redirect=None) -> PublicResponse:
        """Same pinned-IP guard as feeds, retaining final URL and media type."""
        for _redirect in range(MAX_REDIRECTS + 1):
            host, path = self._target(url)
            try:
                addresses = self.resolver(host)
            except FeedError:
                raise
            except Exception as exc:
                raise FeedError("dns_failure") from exc
            if not addresses:
                raise FeedError("dns_failure")
            try:
                if any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                    raise FeedError("disallowed_address")
            except ValueError as exc:
                raise FeedError("dns_failure") from exc
            candidates = list(dict.fromkeys(addresses))[:MAX_CONNECT_ADDRESSES]
            for index, ip in enumerate(candidates):
                try:
                    status, headers, body = self.connector(host, ip, path)
                    break
                except _BeforeRequestFailure:
                    if index == len(candidates)-1:
                        raise
                except FeedError:
                    raise
                except Exception as exc:
                    # An arbitrary connector/read error may follow a sent GET.
                    # Never replay it or retry 403/429/challenge responses.
                    raise FeedError("connection_failed") from exc
            if len(body) > MAX_FEED_BYTES:
                raise FeedError("oversized_feed")
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location") or headers.get("Location")
                if not location:
                    raise FeedError("invalid_redirect")
                target = urljoin(url, location)
                if allow_redirect is not None and not allow_redirect(target):
                    raise FeedError("redirect_disallowed")
                url = target
                continue
            if status == 429:
                raise FeedError("rate_limited", retry_after=retry_after_seconds(headers))
            if status != 200:
                raise FeedError("http_failure", status=status)
            return PublicResponse(url, {k.lower(): v for k, v in headers.items()}, body)
        raise FeedError("too_many_redirects")


def _gdelt_proxy_request(
    proxy_host: str, proxy_port: int, target_host: str, path: str
) -> tuple[int, dict[str, str], bytes]:
    context = ssl.create_default_context()

    class VerifiedTunnel(http.client.HTTPConnection):
        def connect(self) -> None:
            super().connect()  # Opens the local proxy and sends CONNECT.
            self.sock = context.wrap_socket(self.sock, server_hostname=target_host)

    connection = VerifiedTunnel(proxy_host, proxy_port, timeout=20)
    connection.set_tunnel(target_host, 443)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Host": target_host,
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_FEED_BYTES + 1)
        return response.status, {key.lower(): value for key, value in response.getheaders()}, body
    finally:
        connection.close()


class GdeltProxyTransport:
    """A fixed GDELT-only HTTPS route through an explicitly configured loopback proxy."""

    def __init__(
        self,
        proxy_url: str,
        *,
        connector: Callable[[str, int, str, str], tuple[int, dict[str, str], bytes]] | None = None,
    ) -> None:
        self.proxy_host, self.proxy_port = parse_loopback_proxy(proxy_url)
        self.connector = connector or _gdelt_proxy_request

    def _allows(self, url: str) -> bool:
        return is_gdelt_rss_target(url)

    def get(self, url: str) -> bytes:
        if not self._allows(url):
            raise FeedError("disallowed_url")
        host, path = GuardedHTTPS._target(url)
        try:
            status, _headers, body = self.connector(self.proxy_host, self.proxy_port, host, path)
        except FeedError:
            raise
        except Exception as exc:
            raise FeedError("connection_failed") from exc
        if len(body) > MAX_FEED_BYTES:
            raise FeedError("oversized_feed")
        if status in {301, 302, 303, 307, 308}:
            raise FeedError("redirect_not_allowed")
        if status == 429:
            raise FeedError("rate_limited", retry_after=retry_after_seconds(_headers))
        if status != 200:
            raise FeedError("http_failure")
        return body


def validate_fixed_feed_targets(targets) -> tuple[str, ...]:
    """Trusted operator config, never derived from a project, feed or web request.

    These exact public endpoints are resolved by the configured proxy. The operator
    must verify ownership/public purpose. No credential/query/path wildcard grants.
    """
    if not isinstance(targets, (tuple, list)) or not 1 <= len(targets) <= 20:
        raise ValueError('fixed public feed targets must contain 1-20 exact URLs')
    for target in targets:
        if not isinstance(target, str):
            raise ValueError('invalid fixed public feed target')
        try:
            host, path = GuardedHTTPS._target(target)
        except FeedError as exc:
            raise ValueError('invalid fixed public feed target') from exc
        if (not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}', host)
                or not re.fullmatch(r'/[a-zA-Z0-9/_.~-]*', path)
                or any(p in {'.', '..'} for p in path.split('/'))
                or target != f'https://{host}{path}'):
            raise ValueError('invalid fixed public feed target')
    if len(set(targets)) != len(targets):
        raise ValueError('duplicate fixed public feed target')
    return tuple(targets)


def parse_fixed_feed_targets(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        targets = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError('fixed public feed targets must be a JSON array') from exc
    return validate_fixed_feed_targets(targets)


class FixedFeedProxyTransport(GdeltProxyTransport):
    """Optional exact-URL allowlist; inherits verified TLS, bounds and no redirects."""

    def __init__(self, proxy_url, targets, *, connector=None):
        self.targets = validate_fixed_feed_targets(targets)
        super().__init__(proxy_url, connector=connector)

    def _allows(self, url):
        return url in self.targets


class FeedTransport(Protocol):
    def get(self, url: str) -> bytes: ...


class RoutedFeedTransport:
    """Explicit bounded proxy routes; all other feeds retain direct DNS/IP guards."""

    def __init__(
        self, *, direct: GuardedHTTPS | None = None, gdelt_proxy: GdeltProxyTransport | None = None,
        fixed_feed_proxy: FixedFeedProxyTransport | None = None,
    ) -> None:
        self.direct = direct or GuardedHTTPS()
        self.gdelt_proxy = gdelt_proxy
        self.fixed_feed_proxy = fixed_feed_proxy

    def get(self, url: str) -> bytes:
        if self.gdelt_proxy and is_gdelt_rss_target(url):
            return self.gdelt_proxy.get(url)
        if self.fixed_feed_proxy and url in self.fixed_feed_proxy.targets:
            return self.fixed_feed_proxy.get(url)
        return self.direct.get(url)


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _node_text(node: ET.Element | None) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def parse_feed(data: bytes) -> tuple[FeedItem, ...]:
    if len(data) > MAX_FEED_BYTES or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise FeedError("invalid_feed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError("invalid_feed") from exc
    if sum(1 for _ in root.iter()) > 20_000:
        raise FeedError("invalid_feed")
    if root.tag == "rss":
        entries = root.findall("./channel/item")
        atom = False
    elif root.tag == f"{ATOM_NS}feed":
        entries = root.findall(f"{ATOM_NS}entry")
        atom = True
    else:
        raise FeedError("invalid_feed")
    items: list[FeedItem] = []
    for entry in entries[:MAX_ITEMS]:
        if atom:
            link = next(
                (
                    node.get("href", "")
                    for node in entry.findall(f"{ATOM_NS}link")
                    if node.get("rel", "alternate") == "alternate"
                ),
                "",
            )
            external_id = _node_text(entry.find(f"{ATOM_NS}id")) or link
            title = _node_text(entry.find(f"{ATOM_NS}title"))
            summary = _node_text(entry.find(f"{ATOM_NS}summary")) or _node_text(
                entry.find(f"{ATOM_NS}content")
            )
            published = _date(
                _node_text(entry.find(f"{ATOM_NS}published"))
                or _node_text(entry.find(f"{ATOM_NS}updated"))
            )
        else:
            link = _node_text(entry.find("link"))
            external_id = _node_text(entry.find("guid")) or link
            title = _node_text(entry.find("title"))
            summary = _node_text(entry.find("description"))
            published = _date(_node_text(entry.find("pubDate")))
        if not title or not external_id:
            continue
        try:
            # Article links are display-only; reuse the URL policy without resolving them.
            GuardedHTTPS._target(link)
        except FeedError:
            continue
        items.append(
            FeedItem(
                external_id=external_id[:500],
                title=title[:300],
                url=link[:2048],
                summary=summary[:2000],
                published_at=published,
            )
        )
    if entries and not items:
        raise FeedError("no_usable_items")
    return tuple(items)


class PublicFeedAdapter:
    def __init__(self, transport: FeedTransport | None = None) -> None:
        self.transport = transport or GuardedHTTPS()

    def fetch(self, target: str) -> AdapterResult:
        try:
            return AdapterResult(items=parse_feed(self.transport.get(target)))
        except FeedError as exc:
            return AdapterResult(unavailable_error=str(exc), retry_after_seconds=exc.retry_after)


def configured_public_feed_adapter(gdelt_proxy_url: str | None, *,
                                  fixed_feed_proxy_url: str | None = None,
                                  fixed_feed_targets: tuple[str, ...] = ()) -> PublicFeedAdapter:
    """Opt-in fixed-source proxy; no configured proxy changes the direct behavior."""
    if bool(fixed_feed_proxy_url) != bool(fixed_feed_targets):
        raise ValueError('fixed feed proxy requires both local proxy URL and exact targets')
    if gdelt_proxy_url is None and fixed_feed_proxy_url is None:
        return PublicFeedAdapter()
    return PublicFeedAdapter(RoutedFeedTransport(
        gdelt_proxy=GdeltProxyTransport(gdelt_proxy_url) if gdelt_proxy_url else None,
        fixed_feed_proxy=FixedFeedProxyTransport(fixed_feed_proxy_url, fixed_feed_targets) if fixed_feed_proxy_url else None,
    ))
