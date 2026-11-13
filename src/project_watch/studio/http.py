"""Small HTTPS client for fixed API endpoints (model, search, open data).

Only https URLs are allowed. The system proxy (Windows registry / env) is used
by default so users behind Clash/V2Ray work without extra setup; an explicit
proxy from settings overrides it. Responses are size-capped.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import urlsplit

from project_watch.studio.model_stream import IncompleteStream, read_completion

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = "ProjectWatch/0.2 (+self-hosted investment monitoring)"


class HttpError(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None, detail: str = "",
                 transport_phase: str = "unknown", failure_kind: str = "unknown", diagnostics=None) -> None:
        super().__init__(f"{code}{f' ({status})' if status else ''}{': ' + detail if detail else ''}")
        self.code = code
        self.status = status
        self.detail = detail
        self.transport_phase, self.failure_kind = transport_phase, failure_kind
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    diagnostics: dict = field(default_factory=dict)

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError("invalid_json") from exc

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


Transport = Callable[[str, str, dict[str, str], bytes | None, float | None], Response]

TRANSPORT_OWNER = ContextVar('projectwatch_transport_owner', default=None)
_PATIENT_LOCK = threading.Lock()
_PATIENT_REQUESTS = set()


def safe_diagnostics(value):
    """Journal boundary: do not trust arbitrary transport implementations."""
    if not isinstance(value, dict):
        return {}
    enums = {
        'route': {'configured_proxy', 'direct'},
        'stage': {'connect', 'sending', 'response_headers', 'response_body'},
        'exception': {'TimeoutError', 'ConnectionResetError', 'ConnectionRefusedError',
                      'gaierror', 'RemoteDisconnected', 'IncompleteRead', 'OSError', 'IncompleteStream', 'other'},
        'cancel_reason': {'explicit_cancel', 'owner_lost'},
        'stream_failure': {'missing_end', 'oversized_stream', 'invalid_encoding', 'invalid_event',
                           'error_event', 'invalid_choices', 'unexpected_choice', 'unexpected_delta',
                           'invalid_delta', 'invalid_finish', 'missing_finish', 'unexpected_content_type'},
    }
    numbers = {'connected_ms', 'sent_ms', 'headers_ms', 'first_chunk_ms', 'last_chunk_ms',
               'end_ms', 'received_bytes', 'errno', 'winerror'}
    result = {k: v for k, v in value.items() if (k in numbers and type(v) is int)
              or (k in enums and isinstance(v, str) and v in enums[k])}
    request_id = value.get('provider_request_id')
    if isinstance(request_id, str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', request_id):
        result['provider_request_id'] = request_id
    finish = value.get('unexpected_finish')
    if isinstance(finish, str) and re.fullmatch(r'[a-z_]{1,40}', finish):
        result['unexpected_finish'] = finish  # A provider enum word, never reply text.
    return result


class TransportTrace:
    """Only structural timing/error information, never URLs or exception text."""

    def __init__(self, proxy):
        self.start = time.monotonic()
        self.data = {'route': 'configured_proxy' if proxy else 'direct', 'stage': 'connect'}

    def mark(self, name):
        self.data[name + '_ms'] = round((time.monotonic() - self.start) * 1000)

    def chunk(self, count):
        if 'first_chunk_ms' not in self.data:
            self.mark('first_chunk')
        self.mark('last_chunk')
        self.data['received_bytes'] = self.data.get('received_bytes', 0) + count

    def error(self, exc):
        reason = getattr(exc, 'reason', exc)
        # Closed vocabulary: even exception class names can be user-defined.
        names = (TimeoutError, ConnectionResetError, ConnectionRefusedError,
                 socket.gaierror, http.client.RemoteDisconnected,
                 http.client.IncompleteRead, OSError, IncompleteStream)
        self.data['exception'] = next((t.__name__ for t in names if isinstance(reason, t)), 'other')
        if isinstance(reason, IncompleteStream):
            self.data['stream_failure'] = str(reason)
            if isinstance(reason.finish, str) and re.fullmatch(r'[a-z_]{1,40}', reason.finish):
                self.data['unexpected_finish'] = reason.finish
        for name in ('errno', 'winerror'):
            value = getattr(reason, name, None)
            if type(value) is int:
                self.data[name] = value
        self.mark('end')

    def headers(self, headers):
        self.mark('headers')
        self.data['stage'] = 'response_body'
        for key in ('x-request-id', 'request-id', 'x-log-id'):
            value = headers.get(key)
            if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', value):
                self.data['provider_request_id'] = value
                break

    def identify(self, value):
        if ('provider_request_id' not in self.data and isinstance(value, str)
                and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', value)):
            self.data['provider_request_id'] = value


class _PatientControl:
    def __init__(self):
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.socket = None
        self.trace = None

    def attach(self, sock):
        with self.lock:
            self.socket = sock
        if self.cancelled.is_set():
            self.cancel()
            raise HttpError('connection_failed', transport_phase='connect_or_headers', failure_kind='transport_error')

    def cancel(self, reason='explicit_cancel'):
        if self.trace and not self.cancelled.is_set():
            self.trace.data['cancel_reason'] = reason
        self.cancelled.set()
        with self.lock:
            if self.socket is not None:
                sock, self.socket = self.socket, None
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                if os.name == 'nt':
                    # Winsock shutdown alone need not wake a blocked buffered
                    # read. Transfer ownership before closing so later response
                    # cleanup cannot close a reused OS handle.
                    with suppress(OSError):
                        handle = sock.detach()
                        if handle != -1:
                            socket.close(handle)


def cancel_patient_requests():
    """Explicit owner stop only. Interrupted paid requests remain unknown."""
    with _PATIENT_LOCK:
        active = tuple(_PATIENT_REQUESTS)
    for control in active:
        control.cancel()
    return len(active)


@contextmanager
def _patient_control(enabled):
    if not enabled:
        yield None
        return
    control = _PatientControl()
    owner = TRANSPORT_OWNER.get()
    def monitor():
        while not control.done.wait(1):
            try:
                valid = owner()
            except Exception:
                # Transient DB errors are not permission to cancel a paid call;
                # a subsequent successful check still enforces the lease.
                continue
            if not valid:
                control.cancel('owner_lost')
                return
    watcher = threading.Thread(target=monitor, name='model-request-owner', daemon=True) if owner else None
    with _PATIENT_LOCK:
        _PATIENT_REQUESTS.add(control)
    if watcher:
        watcher.start()
    try:
        yield control
    finally:
        control.done.set()
        with _PATIENT_LOCK:
            _PATIENT_REQUESTS.discard(control)
        if watcher:
            watcher.join(timeout=2)


class PatientHTTPSConnection(http.client.HTTPSConnection):
    """Bound connection setup, then let the provider finish its response.

    TCP keepalive detects a dead peer without declaring silent model inference
    a failed request. It is not evidence of application-level progress. An
    established but silent provider can still require an explicit user stop.
    """

    def __init__(self, *args, control=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.control = control

    def connect(self):
        self.timeout = 30  # DNS/TCP/proxy/TLS setup, never model processing.
        super().connect()
        if self.control:
            self.control.attach(self.sock)
            if self.control.trace:
                self.control.trace.mark('connected')
                self.control.trace.data['stage'] = 'sending'
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            if hasattr(socket, 'SIO_KEEPALIVE_VALS'):
                self.sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60_000, 10_000))
            else:
                for name, value in (('TCP_KEEPIDLE', 60), ('TCP_KEEPINTVL', 10), ('TCP_KEEPCNT', 6)):
                    if hasattr(socket, name):
                        self.sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
        except OSError:
            pass  # System keepalive defaults still apply on unsupported platforms.

    def send(self, data):
        if self.control and self.control.trace and 'connected_ms' in self.control.trace.data:
            self.control.trace.data['stage'] = 'sending'
        super().send(data)
        if self.control and self.control.trace and 'connected_ms' in self.control.trace.data:
            self.control.trace.mark('sent')
            self.control.trace.data['stage'] = 'response_headers'


class PatientHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, control):
        super().__init__()
        self.control = control

    def https_open(self, req):
        return self.do_open(partial(PatientHTTPSConnection, control=self.control), req, context=self._context)


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
        self.seen_redirect = True
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
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected._pw_redirected = True
        req._pw_redirected = True
        return redirected


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A finite exact-GET grant never authorizes a second URL implicitly."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HttpError('unsafe_redirect')


def urllib_transport(proxy_url: str = "", *, allow_redirects: bool = True) -> Transport:
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else system_proxies()

    def read_body(response, limit, control):
        if control is None:
            return response.read(limit)
        expected = response.length
        chunked = response.chunked
        data = response.read(limit)
        complete = ((expected is not None and len(data) == expected)
                    or (chunked and response.isclosed()))
        if control and control.cancelled.is_set() and not complete:
            raise HttpError('connection_failed', transport_phase='response_body', failure_kind='transport_error')
        return data

    def perform(method, url, headers, body, timeout, control):
        if urlsplit(url).scheme != "https":
            raise HttpError("disallowed_url")
        redirect = ApiRedirectHandler() if allow_redirects else NoRedirectHandler()
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={"User-Agent": USER_AGENT, **headers})
        phase = 'connect_or_headers'
        trace = TransportTrace(bool(proxies.get('https') or proxies.get('all')))
        if control:
            control.trace = trace
        streaming = False
        if method == 'POST' and body:
            with suppress(ValueError, AttributeError):
                streaming = json.loads(body).get('stream') is True
        redirect.seen_redirect = False
        try:
            handlers = [urllib.request.ProxyHandler(proxies), redirect]
            if control:
                handlers.append(PatientHTTPSHandler(control))
            opener = urllib.request.build_opener(*handlers)
            try:
                with opener.open(request, timeout=timeout) as response:
                    phase = 'response_body'
                    status = response.status
                    response_headers = {k.lower(): v for k, v in response.headers.items()}
                    trace.headers(response_headers)
                    if streaming and status == 200:
                        if response_headers.get('content-type', '').split(';')[0].strip().lower() != 'text/event-stream':
                            raise IncompleteStream('unexpected_content_type')
                        data = read_completion(response, MAX_RESPONSE_BYTES, trace.chunk, trace.identify)
                        response_headers = {**response_headers, 'content-type': 'application/json'}
                    else:
                        data = read_body(response, MAX_RESPONSE_BYTES + 1, control)
                        trace.chunk(len(data))
            except urllib.error.HTTPError as exc:
                phase = 'response_body'
                data = read_body(exc.fp, 64_000, control) if exc.fp else b""
                response_headers = {k.lower(): v for k, v in exc.headers.items()}
                trace.headers(response_headers)
                trace.chunk(len(data))
                trace.mark('end')
                return Response(exc.code, response_headers, data, trace.data)
        except HttpError as exc:
            trace.mark('end')
            exc.diagnostics = trace.data
            raise
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, IncompleteStream) as exc:
            trace.error(exc)
            reason = getattr(exc, 'reason', exc)
            # A patient connection marks `connected` only after TCP, proxy
            # tunnel and TLS setup; before that no request byte has left.
            unconnected = (control is not None and trace.data.get('stage') == 'connect'
                           and 'connected_ms' not in trace.data)
            if (phase == "connect_or_headers" and not getattr(redirect, "seen_redirect", False)
                    and (unconnected or isinstance(reason, (socket.gaierror, ConnectionRefusedError)))):
                raise HttpError("connection_not_sent", transport_phase=phase,
                                failure_kind='timeout' if isinstance(reason, TimeoutError) else 'transport_error',
                                diagnostics=trace.data) from exc
            raise HttpError("connection_failed", detail=str(reason), transport_phase=phase,
                            failure_kind='timeout' if isinstance(reason, TimeoutError) else 'transport_error',
                            diagnostics=trace.data) from exc
        if len(data) > MAX_RESPONSE_BYTES:
            raise HttpError("response_too_large")
        trace.mark('end')
        return Response(status, response_headers, data, trace.data)

    def send(method: str, url: str, headers: dict[str, str], body: bytes | None,
             timeout: float | None) -> Response:
        with _patient_control(timeout is None) as control:
            return perform(method, url, headers, body, timeout, control)

    return send


class Http:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def get(self, url: str, *, headers: dict[str, str] | None = None,
            timeout: float = 20) -> Response:
        return self.transport("GET", url, headers or {}, None, timeout)

    def post_json(self, url: str, payload: dict, *, headers: dict[str, str] | None = None,
                  timeout: float | None = 60) -> Response:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.transport("POST", url, {"Content-Type": "application/json", **(headers or {})},
                              body, timeout)

    def get_json(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 20):
        response = self.get(url, headers=headers, timeout=timeout)
        if response.status != 200:
            raise HttpError("http_failure", status=response.status, detail=response.text()[:200])
        return response.json()
