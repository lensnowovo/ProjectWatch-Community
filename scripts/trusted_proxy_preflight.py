"""Opt-in HTTPS proxy preflight using two synthetic, unknown-user logins.

This does not approve deployment: operator logs, client IP trust, shared rate
limiting and browser flows must be checked on the actual target topology.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from typing import Protocol

from project_watch.team_http import validate_team_origin

_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
_SPOOFED_ID = "client-supplied-id"


class _Requester(Protocol):
    def post(self, url: str, **kwargs): ...


def run_preflight(origin: str, requester: _Requester) -> dict[str, object]:
    """Verify a selected HTTPS gateway strips identity headers before team API."""
    try:
        validated = validate_team_origin(origin)
    except ValueError as error:
        raise ValueError("preflight requires an explicit HTTPS origin") from error
    base_url = f"{validated.scheme}://{validated.netloc}"
    headers = {
        "Origin": base_url,
        "Sec-Fetch-Site": "same-origin",
        "X-Request-ID": _SPOOFED_ID,
    }
    first = requester.post(
        f"{base_url}/api/auth/login",
        json={
            "username": f"unknown-{uuid.uuid4().hex}@example.invalid",
            "password": "synthetic-invalid-password",
        },
        headers={
            **headers,
            "Forwarded": "for=203.0.113.99;proto=http",
            "X-Forwarded-For": "203.0.113.99",
            "X-Forwarded-Proto": "http",
            "X-Real-IP": "203.0.113.99",
        },
    )
    second = requester.post(
        f"{base_url}/api/auth/login",
        json={
            "username": f"unknown-{uuid.uuid4().hex}@example.invalid",
            "password": "synthetic-invalid-password",
        },
        headers=headers,
    )
    request_ids = [response.headers.get("x-request-id", "") for response in (first, second)]
    checks = {
        "forwarding_stripped": first.status_code == 401 and second.status_code == 401,
        "request_id_generated": all(
            _REQUEST_ID.fullmatch(value) and value != _SPOOFED_ID for value in request_ids
        ),
        "distinct_request_ids": len(set(request_ids)) == 2,
        "security_headers_present": all(
            "max-age=" in response.headers.get("strict-transport-security", "")
            and "default-src" in response.headers.get("content-security-policy", "")
            for response in (first, second)
        ),
    }
    return {
        "synthetic_only": True,
        "login_attempts": 2,
        "request_ids": request_ids,
        "checks": checks,
        "passed": all(checks.values()),
        "manual_gates_remaining": [
            "gateway log correlation with actual client address",
            "shared rate limit across application instances",
            "trusted browser TLS and session flow",
            "direct backend access isolation",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="dedicated synthetic HTTPS test origin")
    parser.add_argument(
        "--execute-synthetic-logins",
        action="store_true",
        help="acknowledge two failed logins will be sent to the target",
    )
    args = parser.parse_args()
    if not args.execute_synthetic_logins:
        parser.error("--execute-synthetic-logins is required before network access")
    try:
        import httpx

        with httpx.Client(
            timeout=5, verify=True, follow_redirects=False, trust_env=False
        ) as client:
            report = run_preflight(args.origin, client)
    except ModuleNotFoundError:
        print("proxy preflight requires the optional dev HTTP client", file=sys.stderr)
        return 2
    except (ValueError, httpx.HTTPError):
        print(
            "proxy preflight failed: invalid origin, connection or TLS validation", file=sys.stderr
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
