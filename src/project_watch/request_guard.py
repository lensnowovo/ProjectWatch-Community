"""Request boundary of the local app: local hosts only, no cross-site writes, safe headers."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def protect_request(request: Request, call_next) -> Response:
    """Fail closed before route dispatch; attach security headers even on denial."""

    def denied(code: int, detail: str) -> Response:
        return _headers(JSONResponse(status_code=code, content={"detail": detail}))

    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if request.client is not None and request.client.host == "testclient":
        allowed_hosts.add("testserver")
    if request.url.hostname not in allowed_hosts:
        return denied(400, "invalid local host")

    if request.method in _WRITE_METHODS:
        origin = request.headers.get("origin")
        try:
            cross_origin = origin is not None and (
                urlsplit(origin).scheme != "http"
                or urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()
            )
        except ValueError:
            cross_origin = True
        if request.headers.get("sec-fetch-site") == "cross-site" or cross_origin:
            return denied(403, "cross-site write denied")
    return _headers(await call_next(request))
