"""Small HTTPS client for fixed API endpoints (model, search, open data).

Only https URLs are allowed. The system proxy (Windows registry / env) is used
by default so users behind Clash/V2Ray work without extra setup; an explicit
proxy from settings overrides it. Responses are size-capped.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "ProjectWatch/0.2 (+self-hosted investment monitoring)"


class HttpError(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None, detail: str = "",
                 transport_phase: str = "unknown", failure_kind: str = "unknown") -> None:
        super().__init__(f"{code}{f' ({status})' if status else ''}{': ' + detail if detail else ''}")
        self.code = code
        self.status = status
        self.detail = detail
        self.transport_phase, self.failure_kind = transport_phase, failure_kind


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError("invalid_json") from exc

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


Transport = Callable[[str, str, dict[str, str], bytes | None, float], Response]


def system_proxies() -> dict[str, str]:
    """Honor standard env proxies, otherwise use the Windows system proxy.

    urllib treats *any* *_PROXY variable as a proxy, including PW_PROXY.
    Its Windows fallback uses ``env or registry``; an unrelated entry can
    therefore suppress registry detection despite providing no usable route.
    """
    proxies = {k: v for k, v in urllib.request.getproxies_environment().items()
               if k in {"http", "https", "all", "no"}}
    registry = getattr(urllib.request, "getproxies_registry", None)
    if registry and not any(k in proxies for k in ("http", "https", "all")):
        proxies = {**registry(), **proxies}
    return proxies


class ApiRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fixed API endpoints must not forward credentials to a different origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            source, target = urlsplit(req.full_url), urlsplit(newurl)
            safe = (target.scheme == "https" and target.hostname
                    and not target.username and not target.password
                    and (source.scheme, source.hostname, source.port or 443)
                    == (target.scheme, target.hostname, target.port or 443))
        except ValueError:
            safe = False
        if not safe:
            # Neither endpoint nor headers belong in this error (query keys).
            raise HttpError("unsafe_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urllib_transport(proxy_url: str = "") -> Transport:
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else system_proxies()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), ApiRedirectHandler())

    def send(method: str, url: str, headers: dict[str, str], body: bytes | None,
             timeout: float) -> Response:
        if urlsplit(url).scheme != "https":
            raise HttpError("disallowed_url")
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={"User-Agent": USER_AGENT, **headers})
        phase = 'connect_or_headers'
        try:
            with opener.open(request, timeout=timeout) as response:
                phase = 'response_body'
                data = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            data = exc.read(64_000) if exc.fp else b""
            return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, data)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, 'reason', exc)
            raise HttpError("connection_failed", detail=str(reason), transport_phase=phase,
                            failure_kind='timeout' if isinstance(reason, TimeoutError) else 'transport_error') from exc
        if len(data) > MAX_RESPONSE_BYTES:
            raise HttpError("response_too_large")
        return Response(status, response_headers, data)

    return send


class Http:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def get(self, url: str, *, headers: dict[str, str] | None = None,
            timeout: float = 20) -> Response:
        return self.transport("GET", url, headers or {}, None, timeout)

    def post_json(self, url: str, payload: dict, *, headers: dict[str, str] | None = None,
                  timeout: float = 60) -> Response:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.transport("POST", url, {"Content-Type": "application/json", **(headers or {})},
                              body, timeout)

    def get_json(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 20):
        response = self.get(url, headers=headers, timeout=timeout)
        if response.status != 200:
            raise HttpError("http_failure", status=response.status, detail=response.text()[:200])
        return response.json()
