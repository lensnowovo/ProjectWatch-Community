"""Synthetic preflight checks forwarded-header stripping without live credentials."""

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.repository import ProjectRepository
from project_watch.team_auth import TeamAuthStore
from scripts.trusted_proxy_preflight import run_preflight


class SyntheticGateway:
    """Simulate a proxy which passes only explicitly allowed request headers."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def post(self, url, **kwargs):
        kwargs["headers"] = {
            name: value
            for name, value in kwargs["headers"].items()
            if name.lower()
            not in {"forwarded", "x-forwarded-for", "x-forwarded-proto", "x-real-ip"}
        }
        return self.client.post(url, **kwargs)


def _team_client(tmp_path: Path) -> TestClient:
    path = tmp_path / "synthetic.sqlite3"
    ProjectRepository(path)
    auth = TeamAuthStore(path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    return TestClient(
        create_app(path, team_auth=auth, team_origin="https://testserver"),
        base_url="https://testserver",
    )


def test_preflight_passes_when_synthetic_gateway_strips_forwarding(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="project_watch.security")
    report = run_preflight("https://testserver", SyntheticGateway(_team_client(tmp_path)))
    assert report["passed"] is True
    assert report["checks"] == {
        "forwarding_stripped": True,
        "request_id_generated": True,
        "distinct_request_ids": True,
        "security_headers_present": True,
    }
    assert report["login_attempts"] == 2
    logged_ids = {
        json.loads(record.message)["request_id"]
        for record in caplog.records
        if record.name == "project_watch.security"
    }
    assert set(report["request_ids"]) == logged_ids
    assert "username" not in str(report)
    assert "password" not in str(report)


def test_preflight_fails_closed_when_proxy_passes_spoofed_identity(tmp_path: Path) -> None:
    report = run_preflight("https://testserver", _team_client(tmp_path))
    assert report["passed"] is False
    assert report["checks"]["forwarding_stripped"] is False


@pytest.mark.parametrize(
    "origin",
    [
        "http://testserver",
        "https://user:password@testserver",
        "https://testserver/hidden",
        "https://testserver?token=synthetic",
    ],
)
def test_preflight_rejects_unsafe_target_before_network(origin: str) -> None:
    with pytest.raises(ValueError, match="HTTPS origin"):
        run_preflight(origin, object())


def test_cli_requires_explicit_network_opt_in() -> None:
    product_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "scripts.trusted_proxy_preflight", "--origin", "https://testserver"],
        cwd=product_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--execute-synthetic-logins is required" in result.stderr
