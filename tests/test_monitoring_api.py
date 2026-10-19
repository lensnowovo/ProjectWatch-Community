from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.feeds import GuardedHTTPS, PublicFeedAdapter
from project_watch.monitoring import FeedItem, ManualSnapshotAdapter
from project_watch.repository import ProjectRepository


def _project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def _plan(client: TestClient, project_id: str) -> str:
    response = client.post(
        f"/api/projects/{project_id}/monitoring/plans",
        json={
            "kind": "rss",
            "target": "https://example.org/feed.xml",
            "cadence_minutes": 60,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_monitoring_api_queue_and_candidate_timeline(tmp_path: Path) -> None:
    adapter = ManualSnapshotAdapter(
        {
            "https://example.org/feed.xml": (
                FeedItem(
                    external_id="synthetic-news-1",
                    title="Synthetic Monitored Company funding update",
                    url="https://example.org/news/1",
                    summary="Test-only content.",
                    published_at=datetime.now(UTC),
                ),
                FeedItem(
                    external_id="other-company-news",
                    title="Another company release",
                    url="https://example.org/news/2",
                    summary="Unrelated issuer.",
                    published_at=datetime.now(UTC),
                ),
            )
        }
    )
    app = create_app(tmp_path / "watch.sqlite3", monitoring_adapters={"rss": adapter})
    client = TestClient(app)
    project_id = _project(client, "Synthetic Monitored Company")
    plan_id = _plan(client, project_id)

    plans = client.get(f"/api/projects/{project_id}/monitoring/plans").json()
    assert plans[0]["enabled"] is False
    assert plans[0]["last_run_status"] == "not_run"
    assert (
        client.patch(
            f"/api/projects/{project_id}/monitoring/plans/{plan_id}", json={"enabled": True}
        ).status_code
        == 200
    )
    queued = client.post(
        f"/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
        json={"idempotency_key": "browser-click-1"},
    )
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"
    assert client.get(f"/api/projects/{project_id}/monitoring/signals").json() == []

    app.state.monitoring_scheduler.process_one()
    runs = client.get(f"/api/projects/{project_id}/monitoring/plans/{plan_id}/runs").json()
    jobs = client.get(f"/api/projects/{project_id}/monitoring/jobs").json()
    signals = client.get(f"/api/projects/{project_id}/monitoring/signals").json()

    assert runs[0]["status"] == "completed" and runs[0]["new_count"] == 1
    assert (runs[0]["item_count"], runs[0]["matched_count"]) == (2, 1)
    updated_plan = client.get(f"/api/projects/{project_id}/monitoring/plans").json()[0]
    assert (updated_plan["last_item_count"], updated_plan["last_matched_count"]) == (2, 1)
    assert jobs[0]["status"] == "done"
    assert signals[0]["title"] == "Synthetic Monitored Company funding update"
    assert signals[0]["status"] == "candidate"
    assert client.get(f"/api/projects/{project_id}/summary").json()["confirmed_count"] == 0
    domains = client.get(f"/api/projects/{project_id}/monitoring/domains").json()["domains"]
    external = next(item for item in domains if item["key"] == "external_signals")
    assert datetime.fromisoformat(external["last_checked_at"]) == datetime.fromisoformat(
        runs[0]["finished_at"]
    )
    assert external["last_recorded_at"] is None
    report = client.get(f"/api/projects/{project_id}/report/current").json()
    assert datetime.fromisoformat(
        next(item for item in report["domains"] if item["key"] == "external_signals")[
            "last_checked_at"
        ]
    ) == datetime.fromisoformat(runs[0]["finished_at"])


def test_cross_source_article_is_one_visible_lead_but_two_raw_records(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    feeds = {
        "https://example.org/feed.xml": (
            FeedItem(
                "item-1",
                "Synthetic Company announcement",
                "https://news.example.org/story?id=9&utm_source=one",
                "First feed",
                now,
            ),
        ),
        "https://other.example.org/feed.xml": (
            FeedItem(
                "item-2",
                "Synthetic Company announcement",
                "https://news.example.org/story?id=9&utm_medium=two",
                "Second feed",
                now,
            ),
        ),
    }
    database = tmp_path / "watch.sqlite3"
    app = create_app(database, monitoring_adapters={"rss": ManualSnapshotAdapter(feeds)})
    client = TestClient(app)
    project_id = _project(client, "Synthetic Company")
    first = _plan(client, project_id)
    second = client.post(
        f"/api/projects/{project_id}/monitoring/plans",
        json={"kind": "rss", "target": "https://other.example.org/feed.xml", "cadence_minutes": 60},
    ).json()["id"]
    for index, plan_id in enumerate((first, second)):
        assert (
            client.post(
                f"/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
                json={"idempotency_key": f"cross-source-{index}"},
            ).status_code
            == 202
        )
        app.state.monitoring_scheduler.process_one()

    assert len(ProjectRepository(database).list_signals(project_id)) == 2
    assert len(client.get(f"/api/projects/{project_id}/monitoring/signals").json()) == 1
    assert (
        len(client.get(f"/api/projects/{project_id}/report/current").json()["external_leads"]) == 1
    )


def test_unusable_feed_links_produce_unreachable_receipt(tmp_path: Path) -> None:
    feed = b"""<rss><channel><item><guid>unsafe</guid><title>Unsafe</title>
    <link>https://127.0.0.1/private</link></item></channel></rss>"""
    adapter = PublicFeedAdapter(
        GuardedHTTPS(
            resolver=lambda host: ["93.184.215.14"],
            connector=lambda host, ip, path: (200, {}, feed),
        )
    )
    app = create_app(tmp_path / "watch.sqlite3", monitoring_adapters={"rss": adapter})
    client = TestClient(app)
    project_id = _project(client, "Synthetic Feed Link Validation")
    plan_id = _plan(client, project_id)
    queued = client.post(
        f"/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
        json={"idempotency_key": "unsafe-feed"},
    )
    assert queued.status_code == 202

    app.state.monitoring_scheduler.process_one()
    receipt = client.get(f"/api/projects/{project_id}/monitoring/plans/{plan_id}/runs").json()[0]
    assert receipt["status"] == "unreachable"
    assert receipt["error_code"] == "no_usable_items"
    assert (
        client.get(f"/api/projects/{project_id}/monitoring/plans").json()[0]["last_run_error_code"]
        == "no_usable_items"
    )
    assert client.get(f"/api/projects/{project_id}/monitoring/signals").json() == []


def test_plan_toggle_and_project_isolation(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    first = _project(client, "Synthetic One")
    second = _project(client, "Synthetic Two")
    plan_id = _plan(client, first)

    assert (
        client.patch(
            f"/api/projects/{first}/monitoring/plans/{plan_id}", json={"enabled": True}
        ).json()["enabled"]
        is True
    )
    assert client.get(f"/api/projects/{second}/monitoring/plans/{plan_id}/runs").status_code == 404
    assert (
        client.post(
            f"/api/projects/{second}/monitoring/plans/{plan_id}/runs",
            json={"idempotency_key": "x"},
        ).status_code
        == 404
    )


def test_local_preview_rejects_cross_site_write_and_bad_host(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))

    assert (
        client.post(
            "/api/projects",
            json={"name": "Should Not Exist"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert client.get("/api/projects", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/projects").json() == []
