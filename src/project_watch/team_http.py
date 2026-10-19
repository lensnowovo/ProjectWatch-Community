"""Shared HTTP origin, session and CSRF boundary for both team storage modes."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse

SESSION_COOKIE = "__Host-PWSession"
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_FORWARDED_IDENTITY_HEADERS = frozenset(
    {"forwarded", "x-real-ip", "x-client-ip", "cf-connecting-ip", "true-client-ip"}
)
_security_log = logging.getLogger("project_watch.security")


class LoginAttemptLimiter:
    """Bound expensive password checks per direct peer and account name.

    This is an in-process, single-node guard, not a distributed rate limit.
    Never use caller-supplied forwarding headers as the peer identity.
    """

    def __init__(
        self,
        *,
        per_peer: int = 40,
        per_account: int = 10,
        window_seconds: int = 60,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_peer = per_peer
        self.per_account = per_account
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self.clock = clock
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, peer: str | None, username: str) -> bool:
        peer = peer or "unknown"
        account_hash = hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()
        keys = ((f"peer:{peer}", self.per_peer), (f"account:{account_hash}", self.per_account))
        now = self.clock()
        cutoff = now - self.window_seconds
        with self._lock:
            if len(self._attempts) >= self.max_keys:
                self._attempts = {
                    key: attempts
                    for key, attempts in self._attempts.items()
                    if attempts and attempts[-1] > cutoff
                }
            if any(key not in self._attempts for key, _ in keys) and (
                len(self._attempts) + sum(key not in self._attempts for key, _ in keys)
                > self.max_keys
            ):
                return False
            for key, limit in keys:
                attempts = self._attempts.setdefault(key, deque())
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                if len(attempts) >= limit:
                    return False
            for key, _ in keys:
                self._attempts[key].append(now)
            return True


class TeamSessionStore(Protocol):
    def session(self, token: str): ...

    def validate_csrf(self, token: str, csrf: str) -> bool: ...


def validate_team_origin(origin: str | None) -> SplitResult:
    try:
        parsed = urlsplit(origin or "")
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError
    except ValueError as exc:
        raise ValueError("team mode needs one explicit HTTPS origin") from exc
    return parsed


def _headers(response: Response, *, team: bool) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if team:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


async def protect_request(
    request: Request,
    call_next,
    *,
    team_auth: TeamSessionStore | None,
    team_origin: SplitResult | None,
) -> Response:
    """Fail closed before route dispatch; attach security headers even on denial."""
    team = team_auth is not None
    login_request_id = (
        uuid.uuid4().hex
        if team and request.method == "POST" and request.url.path == "/api/auth/login"
        else None
    )

    def finish(response: Response) -> Response:
        response = _headers(response, team=team)
        if login_request_id is not None:
            response.headers["X-Request-ID"] = login_request_id
            if response.status_code != 200:
                _security_log.warning(
                    json.dumps(
                        {
                            "event": "team_login_rejected",
                            "request_id": login_request_id,
                            "status_code": response.status_code,
                        },
                        separators=(",", ":"),
                    )
                )
        return response

    def denied(code: int, detail: str) -> Response:
        return finish(JSONResponse(status_code=code, content={"detail": detail}))

    if team:
        assert team_origin is not None
        if any(
            name.lower() in _FORWARDED_IDENTITY_HEADERS or name.lower().startswith("x-forwarded-")
            for name in request.headers
        ):
            return denied(400, "forwarded identity headers are not supported")
        if (
            request.url.scheme != "https"
            or request.headers.get("host", "").lower() != team_origin.netloc.lower()
        ):
            return denied(400, "invalid team origin")
    else:
        allowed_hosts = {"127.0.0.1", "localhost", "::1"}
        if request.client is not None and request.client.host == "testclient":
            allowed_hosts.add("testserver")
        if request.url.hostname not in allowed_hosts:
            return denied(400, "invalid local host")

    if request.method in _WRITE_METHODS:
        origin = request.headers.get("origin")
        try:
            cross_origin = origin is not None and (
                urlsplit(origin).scheme != ("https" if team else "http")
                or urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()
            )
        except ValueError:
            cross_origin = True
        if request.headers.get("sec-fetch-site") == "cross-site" or cross_origin:
            return denied(403, "cross-site write denied")

    if team and request.url.path.startswith("/api/") and request.url.path != "/api/auth/login":
        assert team_auth is not None
        token = request.cookies.get(SESSION_COOKIE, "")
        session = team_auth.session(token)
        if session is None:
            return denied(401, "authentication required")
        request.state.team_session = session
        request.state.team_token = token
        if request.method in _WRITE_METHODS and not team_auth.validate_csrf(
            token, request.headers.get("X-CSRF-Token", "")
        ):
            return denied(403, "invalid CSRF token")
    return finish(await call_next(request))
