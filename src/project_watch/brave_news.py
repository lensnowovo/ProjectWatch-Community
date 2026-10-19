"""Explicit, bounded Brave News source. Results are candidates, never facts."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlsplit

from project_watch.feeds import FeedError, GuardedHTTPS, _resolve_public
from project_watch.monitoring import AdapterResult, FeedItem
from project_watch.public_search import normalized_public_name, parse_loopback_proxy

MAX_RESPONSE_BYTES = 1_000_000
MAX_RESULTS = 20
HOST = "api.search.brave.com"
PATH = "/res/v1/news/search"
DISCLOSURE = (
    "开启后会将项目名称发送给 Brave News 公开检索服务，并每日检查；此来源需要管理员配置 API Key，"
    "调用计入机构月度上限。结果仅是候选线索，同名可能误报，别名可能漏报。"
)


def _brave_news_target(name: str, *, include_language: bool) -> str:
    normalized = normalized_public_name(name)
    if len(normalized.split()) > 50:
        raise ValueError("project name is unsuitable for public search")
    params = {
        "q": f'"{normalized}"',
        "country": "ALL",
        "count": "20",
        "freshness": "pw",
        "spellcheck": "false",
    }
    if include_language and any("\u3400" <= character <= "\u9fff" for character in normalized):
        params["search_lang"] = "zh-hans"
    return f"https://{HOST}{PATH}?{urlencode(params)}"


def brave_news_target(name: str) -> str:
    return _brave_news_target(name, include_language=True)


def is_brave_news_target(target: str) -> bool:
    if not isinstance(target, str):
        return False
    try:
        parsed = urlsplit(target)
        if (
            parsed.scheme != "https"
            or parsed.netloc != HOST
            or parsed.path != PATH
            or parsed.fragment
        ):
            return False
        params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        required = {"q", "country", "count", "freshness", "spellcheck"}
        if set(params) not in (required, required | {"search_lang"}):
            return False
        query = params["q"]
        return (
            len(query) == 1
            and query[0].startswith('"')
            and query[0].endswith('"')
            and target
            in {
                brave_news_target(query[0][1:-1]),
                _brave_news_target(query[0][1:-1], include_language=False),
            }
        )
    except ValueError:
        return False


def _request(url: str, key: str) -> tuple[int, bytes]:
    """Pinned TLS connection; redirects and private DNS answers are never followed."""
    if not is_brave_news_target(url):
        raise FeedError("disallowed_url")
    _host, path = GuardedHTTPS._target(url)
    addresses = _resolve_public(HOST)
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise FeedError("disallowed_address")

    class PinnedConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            raw = socket.create_connection((addresses[0], 443), timeout=8)
            self.sock = self._context.wrap_socket(raw, server_hostname=HOST)

    connection = PinnedConnection(HOST, port=443, timeout=8, context=ssl.create_default_context())
    try:
        connection.request(
            "GET", path, headers={"Accept": "application/json", "X-Subscription-Token": key}
        )
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise FeedError("oversized_response")
        return response.status, body
    finally:
        connection.close()


def _proxy_request(proxy_url: str, url: str, key: str) -> tuple[int, bytes]:
    """An explicitly configured loopback CONNECT proxy; TLS still verifies Brave."""
    if not is_brave_news_target(url):
        raise FeedError("disallowed_url")
    proxy_host, proxy_port = parse_loopback_proxy(proxy_url)
    _host, path = GuardedHTTPS._target(url)
    context = ssl.create_default_context()

    class VerifiedTunnel(http.client.HTTPConnection):
        def connect(self) -> None:
            super().connect()
            self.sock = context.wrap_socket(self.sock, server_hostname=HOST)

    connection = VerifiedTunnel(proxy_host, proxy_port, timeout=20)
    connection.set_tunnel(HOST, 443)
    try:
        connection.request(
            "GET",
            path,
            headers={"Host": HOST, "Accept": "application/json", "X-Subscription-Token": key},
        )
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise FeedError("oversized_response")
        return response.status, body
    finally:
        connection.close()


def _date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def parse_brave_news(data: bytes) -> tuple[FeedItem, ...]:
    if len(data) > MAX_RESPONSE_BYTES:
        raise FeedError("oversized_response")
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise FeedError("invalid_response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise FeedError("invalid_response")
    items = []
    for entry in payload["results"][:MAX_RESULTS]:
        if not isinstance(entry, dict):
            continue
        title, url = entry.get("title"), entry.get("url")
        if not isinstance(title, str) or not title.strip() or not isinstance(url, str):
            continue
        try:
            GuardedHTTPS._target(url)  # Display-only URL; no article fetch here.
        except FeedError:
            continue
        summary = entry.get("description")
        items.append(
            FeedItem(
                external_id=url[:500],
                title=title[:300],
                url=url[:2048],
                summary=summary[:2000] if isinstance(summary, str) else "",
                published_at=_date(entry.get("page_age")),
            )
        )
    return tuple(items)


def _provider_error_code(data: bytes) -> str | None:
    """Interpret only documented error codes; never expose provider detail text."""
    try:
        payload = json.loads(data[:MAX_RESPONSE_BYTES])
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return None
    code = payload["error"].get("code")
    return code if isinstance(code, str) else None


def validate_brave_news_configuration(
    api_key: str | None, monthly_limit: int, proxy_url: str | None
) -> None:
    if api_key and ("\r" in api_key or "\n" in api_key or len(api_key) > 512):
        raise ValueError("invalid Brave API key")
    if isinstance(monthly_limit, bool) or not 0 <= monthly_limit <= 100_000:
        raise ValueError("invalid Brave monthly limit")
    if proxy_url is not None:
        parse_loopback_proxy(proxy_url)


class BraveNewsAdapter:
    def __init__(
        self,
        api_key: str | None,
        *,
        monthly_limit: int,
        reserve: Callable[[int], bool],
        proxy_url: str | None = None,
        transport: Callable[[str, str], tuple[int, bytes]] | None = None,
    ) -> None:
        validate_brave_news_configuration(api_key, monthly_limit, proxy_url)
        self.api_key = api_key
        self.monthly_limit = monthly_limit
        self.reserve = reserve
        self.transport = transport or (
            (lambda url, key: _proxy_request(proxy_url, url, key))
            if proxy_url is not None
            else _request
        )

    def fetch(self, target: str) -> AdapterResult:
        if not is_brave_news_target(target):
            return AdapterResult(unavailable_error="disallowed_url")
        if not self.api_key:
            return AdapterResult(unavailable_error="credential_missing")
        if not self.reserve(self.monthly_limit):
            return AdapterResult(unavailable_error="quota_exhausted")
        try:
            status, data = self.transport(target, self.api_key)
            provider_code = _provider_error_code(data) if status != 200 else None
            if status == 429 or provider_code in {"QUOTA_LIMITED", "RATE_LIMITED"}:
                return AdapterResult(unavailable_error="rate_limited")
            if status in (401, 403):
                return AdapterResult(unavailable_error="credential_rejected")
            if status != 200:
                return AdapterResult(unavailable_error="http_failure")
            return AdapterResult(items=parse_brave_news(data))
        except FeedError as exc:
            return AdapterResult(unavailable_error=str(exc))
        except Exception:
            return AdapterResult(unavailable_error="connection_failed")
