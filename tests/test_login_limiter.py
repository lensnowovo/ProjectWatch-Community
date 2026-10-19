"""Login throttling stays bounded and does not trust forwarding headers."""

import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.repository import ProjectRepository
from project_watch.team_auth import TeamAuthStore
from project_watch.team_http import LoginAttemptLimiter


def test_limiter_bounds_peer_and_account_with_sliding_window() -> None:
    now = [100.0]
    limiter = LoginAttemptLimiter(
        per_peer=3, per_account=2, window_seconds=60, clock=lambda: now[0]
    )
    assert limiter.allow("peer-a", " ALICE ")
    assert limiter.allow("peer-a", "alice")
    assert not limiter.allow("peer-b", "Alice")  # account budget spans peers
    assert limiter.allow("peer-a", "bob")
    assert not limiter.allow("peer-a", "carol")  # peer budget spans accounts
    now[0] += 61
    assert limiter.allow("peer-b", "alice")
    assert limiter.allow("peer-a", "carol")


def test_limiter_fails_closed_when_key_table_is_full() -> None:
    now = [1.0]
    limiter = LoginAttemptLimiter(max_keys=2, clock=lambda: now[0])
    assert limiter.allow("peer-a", "alice")
    assert not limiter.allow("peer-b", "bob")
    now[0] += 61
    assert limiter.allow("peer-b", "bob")


def test_sqlite_team_login_throttles_unknown_user_without_forwarded_peer(
    tmp_path: Path, caplog
) -> None:
    path = tmp_path / "synthetic.sqlite3"
    ProjectRepository(path)
    auth = TeamAuthStore(path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    app = create_app(path, team_auth=auth, team_origin="https://testserver")
    app.state.login_limiter = LoginAttemptLimiter(per_peer=2, per_account=2)
    client = TestClient(app, base_url="https://testserver")
    caplog.set_level(logging.WARNING, logger="project_watch.security")
    request_ids = set()
    for index in range(2):
        result = client.post(
            "/api/auth/login",
            json={"username": f"missing-{index}", "password": "wrong-password"},
        )
        assert result.status_code == 401
        request_ids.add(result.headers["x-request-id"])
    blocked = client.post(
        "/api/auth/login",
        json={"username": "admin@example.test", "password": "synthetic-admin-password"},
        headers={"X-Request-ID": "spoofed"},
    )
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    assert "content-security-policy" in blocked.headers
    request_ids.add(blocked.headers["x-request-id"])
    assert len(request_ids) == 3
    assert all(len(value) == 32 and value != "spoofed" for value in request_ids)
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "project_watch.security"
    ]
    assert len(events) == 3
    assert [event["status_code"] for event in events] == [401, 401, 429]
    assert {event["request_id"] for event in events} == request_ids
    assert all(event["event"] == "team_login_rejected" for event in events)
    assert "admin@example.test" not in caplog.text
    assert "wrong-password" not in caplog.text
    assert "203.0.113.99" not in caplog.text


def test_team_mode_rejects_forwarded_identity_headers(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite3"
    ProjectRepository(path)
    auth = TeamAuthStore(path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    client = TestClient(
        create_app(path, team_auth=auth, team_origin="https://testserver"),
        base_url="https://testserver",
    )
    for header in (
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Proto",
        "X-Forwarded-Host",
        "X-Real-IP",
        "CF-Connecting-IP",
    ):
        response = client.get("/", headers={header: "203.0.113.99"})
        assert response.status_code == 400, header
        assert response.json() == {"detail": "forwarded identity headers are not supported"}
