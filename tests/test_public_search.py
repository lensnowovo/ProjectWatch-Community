from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app, create_local_app
from project_watch.feeds import GdeltProxyTransport, PublicFeedAdapter, RoutedFeedTransport
from project_watch.monitoring import FeedItem, ManualSnapshotAdapter
from project_watch.public_search import (
    gdelt_rss_target,
    is_gdelt_rss_target,
    parse_loopback_proxy,
)
from project_watch.repository import ProjectRepository
from project_watch.team_auth import TeamAuthStore


def test_gdelt_target_is_fixed_and_uses_exact_name() -> None:
    target = gdelt_rss_target("合成测试科技")
    parts = urlsplit(target)
    assert parts.scheme == "https"
    assert parts.netloc == "api.gdeltproject.org"
    assert parts.path == "/api/v2/doc/doc"
    assert parse_qs(parts.query) == {
        "query": ['"合成测试科技"'],
        "mode": ["artlist"],
        "format": ["rss"],
        "maxrecords": ["50"],
        "timespan": ["1week"],
        "sort": ["datedesc"],
    }
    assert is_gdelt_rss_target(target)


@pytest.mark.parametrize(
    "target",
    [
        "https://api.gdeltproject.org/private",
        "https://other.example/api/v2/doc/doc?query=%22Synthetic%22",
        "https://user@api.gdeltproject.org/api/v2/doc/doc?query=%22Synthetic%22",
        gdelt_rss_target("Synthetic Company") + "&extra=1",
        gdelt_rss_target("Synthetic Company").replace("mode=artlist", "mode=artgallery"),
        gdelt_rss_target("Synthetic Company").replace("+", "%20"),
        gdelt_rss_target("Synthetic Company") + "#fragment",
    ],
)
def test_only_canonical_gdelt_target_can_use_proxy(target: str) -> None:
    assert not is_gdelt_rss_target(target)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:17891", ("127.0.0.1", 17891)),
        ("http://[::1]:17891", ("::1", 17891)),
    ],
)
def test_loopback_proxy_config(url: str, expected: tuple[str, int]) -> None:
    assert parse_loopback_proxy(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://localhost:17891",
        "http://192.168.1.1:17891",
        "http://example.org:17891",
        "https://127.0.0.1:17891",
        "http://user:secret@127.0.0.1:17891",
        "http://127.0.0.1:17891/path",
        "http://127.0.0.1:17891/?q=1",
        "http://127.0.0.1:0",
    ],
)
def test_loopback_proxy_rejects_unsafe_config(url: str) -> None:
    with pytest.raises(ValueError):
        parse_loopback_proxy(url)


def test_local_factory_proxy_is_explicit_and_invalid_config_fails_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError):
        create_app(tmp_path / "invalid.sqlite3", gdelt_proxy_url="http://example.org:17891")
    monkeypatch.setenv("PROJECT_WATCH_DB_PATH", str(tmp_path / "watch.sqlite3"))
    monkeypatch.setenv("PROJECT_WATCH_GDELT_PROXY_URL", "http://127.0.0.1:17891")
    app = create_local_app()
    transport = app.state.monitoring_scheduler.runner.adapters["rss"].legacy.transport
    assert isinstance(transport, RoutedFeedTransport)
    assert transport.gdelt_proxy.proxy_port == 17891


@pytest.mark.parametrize("name", ["A", 'Synthetic "Quote"', "x\\y", " "])
def test_gdelt_target_rejects_ambiguous_names(name: str) -> None:
    with pytest.raises(ValueError):
        gdelt_rss_target(name)


def test_public_search_requires_explicit_post_and_reuses_plan_and_job(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    project = client.post("/api/projects", json={"name": "Synthetic Monitored Company"}).json()
    path = f"/api/projects/{project['id']}/monitoring/public-search"

    preview = client.get(path)
    assert preview.status_code == 200
    assert preview.json()["eligible"] is True
    assert client.get(f"/api/projects/{project['id']}/monitoring/plans").json() == []

    started = client.post(path, json={"provider": "gdelt"})
    assert started.status_code == 202
    assert started.json()["plan"]["enabled"] is True
    assert started.json()["plan"]["cadence_minutes"] == 1440
    assert started.json()["job"]["status"] == "queued"
    repeated = client.post(path, json={"provider": "gdelt"})
    assert repeated.status_code == 202
    assert repeated.json() == started.json()
    assert len(client.get(f"/api/projects/{project['id']}/monitoring/plans").json()) == 1
    assert len(client.get(f"/api/projects/{project['id']}/monitoring/jobs").json()) == 1
    plan_id = started.json()["plan"]["id"]
    assert (
        client.patch(
            f"/api/projects/{project['id']}/monitoring/plans/{plan_id}", json={"enabled": False}
        ).status_code
        == 200
    )
    restarted = client.post(path, json={"provider": "gdelt"})
    assert restarted.status_code == 202
    assert restarted.json()["plan"]["enabled"] is True
    assert restarted.json()["job"]["id"] == started.json()["job"]["id"]


def test_public_search_invalid_name_never_creates_plan(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    project = client.post("/api/projects", json={"name": "A"}).json()
    path = f"/api/projects/{project['id']}/monitoring/public-search"
    assert client.get(path).json()["eligible"] is False
    assert client.post(path, json={"provider": "gdelt"}).status_code == 422
    assert client.get(f"/api/projects/{project['id']}/monitoring/plans").json() == []
    assert client.post(path, json={"provider": "other"}).status_code == 422


def test_public_search_rate_limit_has_visible_receipt(tmp_path: Path) -> None:
    adapter = PublicFeedAdapter(
        GdeltProxyTransport(
            "http://127.0.0.1:17891",
            connector=lambda proxy_host, proxy_port, host, path: (429, {}, b""),
        )
    )
    app = create_app(tmp_path / "watch.sqlite3", monitoring_adapters={"rss": adapter})
    client = TestClient(app)
    project = client.post("/api/projects", json={"name": "Synthetic Rate Limit Company"}).json()
    project_path = f"/api/projects/{project['id']}/monitoring"
    started = client.post(f"{project_path}/public-search", json={"provider": "gdelt"})
    assert started.status_code == 202

    app.state.monitoring_scheduler.process_one()
    plan_id = started.json()["plan"]["id"]
    receipt = client.get(f"{project_path}/plans/{plan_id}/runs").json()[0]
    plan = client.get(f"{project_path}/plans").json()[0]
    assert (receipt["status"], receipt["error_code"]) == ("rate_limited", "rate_limited")
    assert (plan["last_run_status"], plan["last_run_error_code"]) == (
        "rate_limited",
        "rate_limited",
    )


def test_public_search_uses_synthetic_feed_and_retains_candidate_boundary(tmp_path: Path) -> None:
    name = "Synthetic Monitored Company"
    target = gdelt_rss_target(name)
    adapter = ManualSnapshotAdapter(
        {
            target: (
                FeedItem(
                    "match",
                    f"{name} product update",
                    "https://example.org/match",
                    "Test item",
                    datetime.now(UTC),
                ),
                FeedItem(
                    "other",
                    "Another company update",
                    "https://example.org/other",
                    "Test item",
                    datetime.now(UTC),
                ),
            )
        }
    )
    app = create_app(tmp_path / "watch.sqlite3", monitoring_adapters={"rss": adapter})
    client = TestClient(app)
    project = client.post("/api/projects", json={"name": name}).json()
    path = f"/api/projects/{project['id']}/monitoring/public-search"
    started = client.post(path, json={"provider": "gdelt"})
    assert started.status_code == 202
    assert app.state.monitoring_scheduler.process_one().status == "done"
    plan_id = started.json()["plan"]["id"]
    receipt = client.get(f"/api/projects/{project['id']}/monitoring/plans/{plan_id}/runs").json()[0]
    assert (receipt["item_count"], receipt["matched_count"]) == (2, 1)
    signals = client.get(f"/api/projects/{project['id']}/monitoring/signals").json()
    assert len(signals) == 1 and signals[0]["status"] == "candidate"
    assert client.get(f"/api/projects/{project['id']}/summary").json()["confirmed_count"] == 0


def test_public_search_team_permissions_and_csrf(tmp_path: Path) -> None:
    database = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(database)
    auth = TeamAuthStore(database)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    project = repo.create_project("Synthetic Monitored Company")
    other = repo.create_project("Synthetic Other Company")
    member = auth.create_user("viewer@example.test", "synthetic-viewer-password")
    auth.grant_project_role(project.id, member.id, "viewer")
    app = create_app(database, team_auth=auth, team_origin="https://testserver")
    admin_client = TestClient(app, base_url="https://testserver")
    viewer_client = TestClient(app, base_url="https://testserver")
    admin_csrf = admin_client.post(
        "/api/auth/login",
        json={"username": "admin@example.test", "password": "synthetic-admin-password"},
    ).json()["csrf_token"]
    viewer_csrf = viewer_client.post(
        "/api/auth/login",
        json={"username": "viewer@example.test", "password": "synthetic-viewer-password"},
    ).json()["csrf_token"]
    path = f"/api/projects/{project.id}/monitoring/public-search"

    assert viewer_client.get(path).status_code == 200
    assert (
        viewer_client.get(f"/api/projects/{other.id}/monitoring/public-search").status_code == 404
    )
    assert (
        viewer_client.post(
            path, json={"provider": "gdelt"}, headers={"X-CSRF-Token": viewer_csrf}
        ).status_code
        == 404
    )
    assert admin_client.post(path, json={"provider": "gdelt"}).status_code == 403
    assert (
        admin_client.post(
            path, json={"provider": "gdelt"}, headers={"X-CSRF-Token": admin_csrf}
        ).status_code
        == 202
    )
