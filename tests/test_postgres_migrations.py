"""Integration tests run only against an isolated database ending in _test."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from project_watch import postgres_backup, postgres_migrations, postgres_team_auth
from project_watch.analysis.operating_mapping import map_operating_fragments
from project_watch.backup import BackupError
from project_watch.blob_store import BlobStore
from project_watch.brave_news import BraveNewsAdapter, brave_news_target
from project_watch.domain import FragmentInput, SignalInput
from project_watch.monitoring import AdapterResult, FeedItem, ManualSnapshotAdapter
from project_watch.native_intake import NativeCandidate
from project_watch.postgres_core import PostgresCoreRepository
from project_watch.postgres_mcp_server import build_postgres_mcp_server
from project_watch.postgres_tasks import PostgresTaskService
from project_watch.postgres_team_api import (
    create_postgres_team_app,
    create_postgres_team_app_from_env,
)
from project_watch.postgres_team_auth import PostgresTeamAuthStore
from project_watch.postgres_workers import PostgresIntakeWorker, PostgresMonitoringScheduler
from project_watch.repository import IntakeConflictError, NotFoundError, ReviewConflictError
from project_watch.tasks import EvidenceConflictError
from project_watch.team_auth import AuthenticationError, AuthorizationError
from project_watch.team_http import LoginAttemptLimiter

psycopg = pytest.importorskip("psycopg")


def test_postgres_holding_snapshot_is_reviewed_source_bound_and_project_isolated(
    empty_test_database,
) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Synthetic Holding Subject", verified_actor_id="admin")
        foreign = repo.create_project("Synthetic Holding Other", verified_actor_id="admin")
        source = repo.add_source(
            project.id, kind="document", reference="holdings.txt", content_sha256="a" * 64
        )
        repo.enqueue_intake(project.id, source.id)
        job = repo.claim_next_intake()
        repo.complete_intake(
            project.id, job.id,
            [FragmentInput(1, "line 1", "甲基金 2026-06-30 成本 1,100 万元，持股 8%，价值 1,430 万元")],
            expected_attempt=job.attempts,
        )
        params = dict(
            fund_name="甲基金", as_of=date(2026, 6, 30),
            source_id=source.id, source_locator="line 1",
            invested_cost_wan=Decimal("1100"), ownership_pct=Decimal("8"),
            current_value_wan=Decimal("1430"), verified_actor_id="analyst",
        )
        with pytest.raises(NotFoundError):
            repo.propose_holding_snapshot(foreign.id, **params)
        with pytest.raises(ValueError, match="does not occur"):
            repo.propose_holding_snapshot(project.id, **{**params, "ownership_pct": Decimal("9")})
        proposed = repo.propose_holding_snapshot(project.id, **params)
        assert proposed.status == "candidate"
        assert repo.list_holding_snapshots(project.id)[0].id == proposed.id
        with pytest.raises(ValueError, match="own proposal"):
            repo.review_holding_snapshot(
                project.id, proposed.id, action="confirm", expected_version=1,
                verified_reviewer_id="analyst",
            )
        reviewed = repo.review_holding_snapshot(
            project.id, proposed.id, action="confirm", expected_version=1,
            verified_reviewer_id="reviewer",
        )
        assert reviewed.status == "confirmed"
        assert reviewed.version == 2
        with pytest.raises(ReviewConflictError):
            repo.review_holding_snapshot(
                project.id, proposed.id, action="reject", expected_version=1,
                verified_reviewer_id="reviewer",
            )
        assert repo.list_holding_snapshots(foreign.id) == []


def test_v11_upgrade_adds_empty_holdings_without_rewriting_project(empty_test_database) -> None:
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in range(1, 12):
            connection.execute(postgres_migrations._migration_sql(version))
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, postgres_migrations._checksum(version)),
            )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('held-project', 'Synthetic Existing', 'synthetic existing',
                       'unverified', '2026-09-30T00:00:00+00:00')"""
        )
    assert postgres_migrations.inspect_postgres(dsn).version == 11
    with pytest.raises(postgres_migrations.MigrationError, match="verified backup"):
        postgres_migrations.upgrade_postgres(dsn)
    assert postgres_migrations.upgrade_postgres(dsn, backup_verified=True).version == 12
    with PostgresCoreRepository(dsn) as repo:
        assert repo.get_project("held-project").name == "Synthetic Existing"
        assert repo.list_holding_snapshots("held-project") == []


def test_postgres_operating_mapping_creates_reviewable_candidate(empty_test_database) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    fragments = [
        FragmentInput(1, "sheet 产能统计 row 1", "A1=产品 | B1=指标 | C1=单位 | D1=2025年实际"),
        FragmentInput(
            2,
            "sheet 产能统计 row 2",
            "A2=连接器A | B2=设计产能 | C2=万件 | D2[header:2025年实际]=120",
        ),
    ]
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Operating Mapping Synthetic", verified_actor_id="admin")
        source = repo.add_source(
            project.id, kind="document", reference="synthetic.xlsx", content_sha256="a" * 64
        )
        repo.enqueue_intake(project.id, source.id)
        job = repo.claim_next_intake()
        candidates = map_operating_fragments(source.reference, fragments).candidates
        assert len(candidates) == 1
        repo.complete_intake(
            project.id,
            job.id,
            fragments,
            expected_attempt=job.attempts,
            native_operating_candidates=candidates,
        )
        stored = [row for row in repo.list_observations(project.id) if row.statement == "设计产能"]
        assert len(stored) == 1
        assert (stored[0].status, stored[0].unit, stored[0].scope, stored[0].period) == (
            "candidate",
            "万件",
            "连接器A",
            "2025",
        )


def test_postgres_batch_review_is_atomic_and_project_bound(
    empty_test_database, tmp_path: Path
) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Batch review synthetic", verified_actor_id="admin")
        source = repo.add_source(
            project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64
        )
        first = repo.add_observation(
            project.id,
            source_id=source.id,
            subject="经营情况",
            statement="已获订单",
            source_locator="line 1",
            verified_actor_id="admin",
        )
        second = repo.add_observation(
            project.id,
            source_id=source.id,
            subject="经营情况",
            statement="计划扩产",
            source_locator="line 2",
            verified_actor_id="admin",
        )
        outside = repo.create_project("Outside batch synthetic", verified_actor_id="admin")
        outside_source = repo.add_source(
            outside.id, kind="document", reference="outside.txt", content_sha256="b" * 64
        )
        foreign = repo.add_observation(
            outside.id,
            source_id=outside_source.id,
            subject="经营情况",
            statement="外部记录",
            source_locator="line 1",
            verified_actor_id="admin",
        )
        with pytest.raises(ReviewConflictError):
            repo.review_observations_batch(
                project.id,
                ((first.id, 1, "confirm"), (second.id, 2, "confirm")),
                verified_reviewer_id="admin",
            )
        with pytest.raises(NotFoundError):
            repo.review_observations_batch(
                project.id,
                ((first.id, 1, "confirm"), (foreign.id, 1, "confirm")),
                verified_reviewer_id="admin",
            )
        assert all(row.status == "candidate" for row in repo.list_observations(project.id))
    with PostgresTeamAuthStore(dsn) as auth:
        auth.bootstrap_admin("batch-admin", "long-synthetic-password")
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    with TestClient(app, base_url="https://testserver") as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "batch-admin", "password": "long-synthetic-password"},
        )
        assert login.status_code == 200
        path = f"/api/projects/{project.id}/observations/batch-review"
        payload = {
            "items": [
                {"observation_id": first.id, "expected_version": 1, "action": "confirm"},
                {"observation_id": second.id, "expected_version": 1, "action": "reject"},
            ]
        }
        assert client.post(path, json=payload).status_code == 403
        response = client.post(
            path, json=payload, headers={"X-CSRF-Token": login.json()["csrf_token"]}
        )
        assert response.status_code == 200
        assert [row["status"] for row in response.json()] == ["confirmed", "rejected"]


def test_postgres_rate_limit_result_is_a_distinct_valid_status() -> None:
    PostgresCoreRepository._validate_run_result("rate_limited", "rate_limited", ())
    PostgresCoreRepository._validate_run_result("rate_limited", "quota_exhausted", ())
    with pytest.raises(ValueError):
        PostgresCoreRepository._validate_run_result("rate_limited", "timeout", ())


def test_postgres_worker_records_rate_limit_without_claiming_no_results(
    empty_test_database,
) -> None:
    class LimitedAdapter:
        def fetch(self, target: str) -> AdapterResult:
            return AdapterResult(unavailable_error="rate_limited")

    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Limited Synthetic", verified_actor_id="admin")
        plan = repo.create_monitoring_plan(
            project.id, kind="rss", target="https://example.org/feed", cadence_minutes=1440
        )
        repo.request_monitoring_run(project.id, plan.id, idempotency_key="limited")
        scheduler = PostgresMonitoringScheduler(repo, {"rss": LimitedAdapter()})
        assert scheduler.process_one().status == "done"
        receipt = repo.list_source_runs(project.id, plan.id)[0]
        assert (receipt.status, receipt.error_code, receipt.item_count) == (
            "rate_limited",
            "rate_limited",
            0,
        )
        assert repo.get_monitoring_plan(project.id, plan.id).last_run_status == "rate_limited"
        assert repo.list_signals(project.id) == []


def test_postgres_public_search_requires_manager_opt_in_and_reuses_first_job(
    empty_test_database, tmp_path: Path
) -> None:
    from project_watch.public_search import gdelt_rss_target

    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth:
        admin = auth.bootstrap_admin("public-admin", "long-synthetic-password")
        viewer = auth.create_user(
            "public-viewer", "long-synthetic-password", actor_user_id=admin.id
        )
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Synthetic Public Company", verified_actor_id=admin.id)
        other = repo.create_project("Synthetic Other Company", verified_actor_id=admin.id)
    with PostgresTeamAuthStore(dsn) as auth:
        auth.grant_project_role(project.id, viewer.id, "viewer", actor_user_id=admin.id)
    target = gdelt_rss_target(project.name)
    app = create_postgres_team_app(
        dsn,
        object_root=tmp_path / "objects",
        team_origin="https://testserver",
        monitoring_adapters={"rss": ManualSnapshotAdapter({target: ()})},
    )
    with TestClient(app, base_url="https://testserver") as admin_client:
        admin_csrf = admin_client.post(
            "/api/auth/login",
            json={"username": "public-admin", "password": "long-synthetic-password"},
        ).json()["csrf_token"]
        path = f"/api/projects/{project.id}/monitoring/public-search"
        assert admin_client.get(path).json()["eligible"] is True
        assert admin_client.get(f"/api/projects/{project.id}/monitoring/plans").json() == []
        assert admin_client.post(path, json={"provider": "gdelt"}).status_code == 403
        started = admin_client.post(
            path, json={"provider": "gdelt"}, headers={"X-CSRF-Token": admin_csrf}
        )
        assert started.status_code == 202
        assert started.json()["plan"]["enabled"] is True
        assert (
            admin_client.post(
                path, json={"provider": "gdelt"}, headers={"X-CSRF-Token": admin_csrf}
            ).json()
            == started.json()
        )
        assert len(admin_client.get(f"/api/projects/{project.id}/monitoring/jobs").json()) == 1
        assert app.state.monitoring_scheduler.process_one().status == "done"
        plan_id = started.json()["plan"]["id"]
        runs = admin_client.get(
            f"/api/projects/{project.id}/monitoring/plans/{plan_id}/runs"
        ).json()
        assert runs[0]["status"] == "no_results"

        with TestClient(app, base_url="https://testserver") as viewer_client:
            viewer_csrf = viewer_client.post(
                "/api/auth/login",
                json={"username": "public-viewer", "password": "long-synthetic-password"},
            ).json()["csrf_token"]
            assert viewer_client.get(path).status_code == 200
            assert (
                viewer_client.get(f"/api/projects/{other.id}/monitoring/public-search").status_code
                == 404
            )
            assert (
                viewer_client.post(
                    path, json={"provider": "gdelt"}, headers={"X-CSRF-Token": viewer_csrf}
                ).status_code
                == 404
            )


def test_postgres_public_proxy_config_does_not_change_other_adapters(
    empty_test_database, tmp_path: Path
) -> None:
    from project_watch.feeds import RoutedFeedTransport

    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth:
        auth.bootstrap_admin("proxy-admin", "long-synthetic-password")
    with pytest.raises(ValueError):
        create_postgres_team_app(
            dsn,
            object_root=tmp_path / "objects",
            team_origin="https://testserver",
            gdelt_proxy_url="http://example.org:17891",
        )
    with pytest.raises(ValueError):
        create_postgres_team_app(
            dsn,
            object_root=tmp_path / "objects",
            team_origin="https://testserver",
            brave_api_key="synthetic-key",
            brave_monthly_limit=1,
            brave_proxy_url="http://example.org:17891",
        )
    app = create_postgres_team_app(
        dsn,
        object_root=tmp_path / "objects",
        team_origin="https://testserver",
        gdelt_proxy_url="http://127.0.0.1:17891",
    )
    with TestClient(app, base_url="https://testserver"):
        adapter = app.state.monitoring_scheduler.adapters["rss"]
        assert isinstance(adapter.transport, RoutedFeedTransport)
        assert adapter.transport.gdelt_proxy.proxy_port == 17891


def test_postgres_team_login_throttles_before_password_check(
    empty_test_database, tmp_path: Path, caplog
):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth:
        auth.bootstrap_admin("limited-admin", "long-synthetic-password")
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    app.state.login_limiter = LoginAttemptLimiter(per_peer=2, per_account=2)
    caplog.set_level(logging.WARNING, logger="project_watch.security")
    with TestClient(app, base_url="https://testserver") as client:
        for index in range(2):
            denied = client.post(
                "/api/auth/login",
                json={"username": f"missing-{index}", "password": "wrong-password"},
            )
            assert denied.status_code == 401
        blocked = client.post(
            "/api/auth/login",
            json={"username": "limited-admin", "password": "long-synthetic-password"},
        )
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "60"
        assert client.get("/", headers={"Forwarded": "for=203.0.113.99"}).status_code == 400
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "project_watch.security"
    ]
    assert [event["status_code"] for event in events] == [401, 401, 429]
    assert events[-1]["request_id"] == blocked.headers["x-request-id"]
    assert "limited-admin" not in caplog.text
    assert "long-synthetic-password" not in caplog.text


def test_postgres_source_comparison_is_project_scoped_and_read_only(
    empty_test_database, tmp_path: Path
):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth:
        auth.bootstrap_admin("compare-admin", "long-synthetic-password")
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Comparison Project", verified_actor_id="admin")
        other = repo.create_project("Other Project", verified_actor_id="admin")
        sources = []
        for index, text in enumerate(("Before", "After")):
            source = repo.add_source(
                project.id,
                kind="document",
                reference="report.txt",
                content_sha256=f"{index + 1}" * 64,
            )
            repo.enqueue_intake(project.id, source.id)
            claimed = repo.claim_next_intake()
            repo.complete_intake(
                project.id,
                claimed.id,
                [FragmentInput(ordinal=1, locator="line 1", text=text)],
                expected_attempt=claimed.attempts,
            )
            sources.append(source)
        outside = repo.add_source(
            other.id, kind="document", reference="private.txt", content_sha256="a" * 64
        )
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    with TestClient(app, base_url="https://testserver") as client:
        assert (
            client.get(
                f"/api/projects/{project.id}/sources/{sources[1].id}/compare",
                params={"before_source_id": sources[0].id},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "compare-admin", "password": "long-synthetic-password"},
            ).status_code
            == 200
        )
        path = f"/api/projects/{project.id}/sources/{sources[1].id}/compare"
        result = client.get(path, params={"before_source_id": sources[0].id})
        assert result.status_code == 200
        assert result.json()["counts"]["changed"] == 1
        assert client.get(path, params={"before_source_id": outside.id}).status_code == 404
        assert client.get(f"/api/projects/{project.id}/observations").json() == []


@pytest.fixture
def empty_test_database():
    dsn = os.environ.get("PROJECT_WATCH_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set PROJECT_WATCH_TEST_DATABASE_URL for isolated PostgreSQL tests")
    with psycopg.connect(dsn) as connection:
        database = connection.execute("SELECT current_database()").fetchone()[0]
        if not database.endswith("_test"):
            pytest.fail("PostgreSQL integration tests require a database ending in _test")
        if postgres_migrations.inspect_postgres(dsn).version != 0:
            pytest.fail("PostgreSQL integration tests require a fresh database")
        existing = connection.execute(
            """SELECT count(*) FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE c.relkind = 'r' AND n.nspname = 'public'"""
        ).fetchone()[0]
        if existing:
            pytest.fail("PostgreSQL integration tests require an empty public schema")
    try:
        yield dsn
    finally:
        # The only target is the versioned schema created by this test in a
        # verified *_test database; no external or production DB is touched.
        with psycopg.connect(dsn) as connection:
            connection.execute("DROP SCHEMA IF EXISTS project_watch CASCADE")
            connection.execute("DROP TABLE IF EXISTS public.project_watch_test_sentinel")


def test_competitive_peers_are_reviewed_and_cross_project_comparison_checks_access(
    empty_test_database, tmp_path: Path
) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth:
        admin = auth.bootstrap_admin("peer-admin", "long-synthetic-password")
        analyst = auth.create_user(
            "peer-analyst", "long-synthetic-password", actor_user_id=admin.id
        )
    with PostgresCoreRepository(dsn) as repo:
        subject = repo.create_project("Synthetic Peer Subject", verified_actor_id=admin.id)
        other = repo.create_project("Synthetic Peer Other", verified_actor_id=admin.id)
        for project, digest, value in (
            (subject, "a" * 64, "120"),
            (other, "b" * 64, "100"),
        ):
            source = repo.add_source(
                project.id, kind="document", reference="synthetic.pdf", content_sha256=digest
            )
            proposed = repo.add_observation(
                project.id,
                source_id=source.id,
                subject="财务表现",
                statement="营业收入",
                source_locator="利润表:B3",
                number=Decimal(value),
                unit="万元",
                period="2025",
                scope="合并",
                basis="actual",
                verified_actor_id=admin.id,
            )
            repo.confirm_observation(
                project.id, proposed.id, verified_reviewer_id=admin.id, expected_version=1
            )
        peer = repo.propose_competitive_peer(
            subject.id,
            name=other.name,
            linked_project_id=other.id,
            verified_actor_id=admin.id,
        )
        assert peer.status == "candidate"
        assert repo.list_competitive_peers(subject.id)[0].id == peer.id
        with pytest.raises(ReviewConflictError):
            repo.review_competitive_peer(
                subject.id,
                peer.id,
                action="confirm",
                expected_version=2,
                verified_reviewer_id=admin.id,
            )
        repo.review_competitive_peer(
            subject.id,
            peer.id,
            action="confirm",
            expected_version=1,
            verified_reviewer_id=admin.id,
        )
        name_only = repo.propose_competitive_peer(
            subject.id, name="Name Only Synthetic", verified_actor_id=admin.id
        )
        repo.review_competitive_peer(
            subject.id,
            name_only.id,
            action="confirm",
            expected_version=1,
            verified_reviewer_id=admin.id,
        )
        relinked = repo.propose_competitive_peer(
            subject.id,
            name=name_only.name,
            linked_project_id=other.id,
            verified_actor_id=admin.id,
        )
        assert relinked.id == name_only.id
        assert (relinked.status, relinked.version) == ("candidate", 3)
    with PostgresTeamAuthStore(dsn) as auth:
        auth.grant_project_role(subject.id, analyst.id, "reviewer", actor_user_id=admin.id)
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    with TestClient(app, base_url="https://testserver") as client:
        csrf = client.post(
            "/api/auth/login",
            json={"username": "peer-analyst", "password": "long-synthetic-password"},
        ).json()["csrf_token"]
        path = f"/api/projects/{subject.id}"
        hidden = client.get(f"{path}/competitive-position")
        assert hidden.status_code == 200
        assert hidden.json()["peer_access"][peer.id] == "unavailable"
        assert hidden.json()["comparison"]["comparisons"] == []
        assert (
            next(row for row in hidden.json()["peers"] if row["id"] == peer.id)["linked_project_id"]
            is None
        )
        assert (
            next(
                row
                for row in client.get(f"{path}/competitive-peers").json()
                if row["id"] == peer.id
            )["linked_project_id"]
            is None
        )
        assert (
            client.post(
                f"{path}/competitive-peers",
                json={"name": "Hidden", "linked_project_id": other.id},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 404
        )
        with PostgresTeamAuthStore(dsn) as auth:
            auth.grant_project_role(other.id, analyst.id, "viewer", actor_user_id=admin.id)
        visible = client.get(f"{path}/competitive-position")
        assert visible.status_code == 200
        assert (
            next(row for row in visible.json()["peers"] if row["id"] == peer.id)[
                "linked_project_id"
            ]
            == other.id
        )
        assert visible.json()["comparison"]["comparisons"][0]["peers"][0]["value"] == "100"


def test_postgres_brave_budget_is_atomic_across_worker_connections(empty_test_database) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        with ThreadPoolExecutor(max_workers=8) as pool:
            grants = list(pool.map(repo.reserve_brave_news_request, [3] * 12))
        assert sum(grants) == 3
        assert repo.brave_news_usage() == 3


def test_postgres_watch_item_review_is_source_bound_and_versioned(
    empty_test_database, tmp_path: Path
) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Synthetic Watch Item", verified_actor_id="reviewer")
        other = repo.create_project("Synthetic Other Watch Item", verified_actor_id="reviewer")
        source = repo.add_source(
            project.id, kind="document", reference="synthetic.txt", content_sha256="c" * 64
        )
        observation = repo.add_observation(
            project.id,
            source_id=source.id,
            subject="风险与合规",
            statement="需核对续约条件",
            source_locator="line 4",
            verified_actor_id="analyst",
        )
        with pytest.raises(ValueError, match="confirmed"):
            repo.propose_watch_item(
                project.id,
                observation_id=observation.id,
                title="核对续约",
                question="取得协议",
                verified_actor_id="analyst",
            )
        repo.confirm_observation(
            project.id, observation.id, verified_reviewer_id="reviewer", expected_version=1
        )
        with pytest.raises(NotFoundError):
            repo.propose_watch_item(
                other.id,
                observation_id=observation.id,
                title="核对续约",
                question="取得协议",
                verified_actor_id="analyst",
            )
        item = repo.propose_watch_item(
            project.id,
            observation_id=observation.id,
            title="核对续约",
            question="取得协议",
            owner="项目组",
            verified_actor_id="analyst",
        )
        assert (
            repo.propose_watch_item(
                project.id,
                observation_id=observation.id,
                title="核对续约",
                question="取得协议",
                owner="项目组",
                verified_actor_id="analyst",
            ).id
            == item.id
        )
        with pytest.raises(ReviewConflictError):
            repo.propose_watch_item(
                project.id,
                observation_id=observation.id,
                title="核对续约",
                question="另一项核对要求",
                owner="项目组",
                verified_actor_id="analyst",
            )
        reviewed = repo.review_watch_item(
            project.id,
            item.id,
            action="require_action",
            expected_version=1,
            verified_reviewer_id="reviewer",
        )
        assert reviewed.status == "action_required"
        assert reviewed.version == 2
        with pytest.raises(ReviewConflictError):
            repo.review_watch_item(
                project.id,
                item.id,
                action="reject",
                expected_version=1,
                verified_reviewer_id="reviewer",
            )
        with psycopg.connect(dsn) as connection:
            connection.execute("SET search_path TO project_watch, public")
            events = connection.execute(
                "SELECT version, to_status FROM watch_item_events WHERE watch_item_id = %s ORDER BY version",
                (item.id,),
            ).fetchall()
        assert events == [(1, "candidate"), (2, "action_required")]
    with PostgresTeamAuthStore(dsn) as auth:
        auth.bootstrap_admin("watch-admin", "long-synthetic-password")
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    with TestClient(app, base_url="https://testserver") as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "watch-admin", "password": "long-synthetic-password"},
        )
        assert login.status_code == 200
        root = f"/api/projects/{project.id}"
        visible = client.get(f"{root}/watch-items")
        assert visible.status_code == 200
        assert visible.json()[0]["source_locator"] == "line 4"
        assert visible.json()[0]["status"] == "action_required"
        report = client.get(f"{root}/report/current")
        assert report.status_code == 200
        assert report.json()["watch_items"][0]["id"] == item.id
        body = {"observation_id": observation.id, "title": "再核对一次", "question": "补充合同"}
        assert client.post(f"{root}/watch-items", json=body).status_code == 403
        proposed = client.post(
            f"{root}/watch-items",
            json=body,
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        )
        assert proposed.status_code == 201
        assert proposed.json()["status"] == "candidate"
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
        first_review = client.post(
            f"{root}/watch-items/{proposed.json()['id']}/review",
            json={"action": "watch", "expected_version": 1},
            headers=csrf,
        )
        assert first_review.status_code == 200
        change_body = {
            "expected_version": 2,
            "target_status": "resolved",
            "question": "补充合同并核对条款",
            "owner": "投资组",
            "due_on": None,
            "reason": "已完成本轮资料核对",
        }
        assert (
            client.post(
                f"{root}/watch-items/{proposed.json()['id']}/changes", json=change_body
            ).status_code
            == 403
        )
        change = client.post(
            f"{root}/watch-items/{proposed.json()['id']}/changes",
            json=change_body,
            headers=csrf,
        )
        assert change.status_code == 201
        assert client.get(f"{root}/watch-item-changes").json()[0]["status"] == "candidate"
        assert client.get(f"{root}/report/current").json()["watch_items"]
        completed = client.post(
            f"{root}/watch-items/{proposed.json()['id']}/changes/{change.json()['id']}/review",
            json={"action": "accept", "expected_version": 2, "reviewer": "untrusted-supplied"},
            headers=csrf,
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "resolved"
        assert completed.json()["reviewed_by"] == login.json()["user_id"]
        assert client.get(f"{root}/report/current").json()["resolved_watch_items"]


def test_postgres_watch_item_change_is_reviewed_and_project_scoped(empty_test_database) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Synthetic Watch Lifecycle", verified_actor_id="reviewer")
        other = repo.create_project("Synthetic Other Lifecycle", verified_actor_id="reviewer")
        source = repo.add_source(
            project.id, kind="document", reference="source.txt", content_sha256="e" * 64
        )
        observation = repo.add_observation(
            project.id,
            source_id=source.id,
            subject="风险与合规",
            statement="核对客户续约",
            source_locator="line 5",
            verified_actor_id="analyst",
        )
        repo.confirm_observation(
            project.id, observation.id, verified_reviewer_id="reviewer", expected_version=1
        )
        item = repo.propose_watch_item(
            project.id,
            observation_id=observation.id,
            title="跟进续约",
            question="取得协议",
            verified_actor_id="analyst",
        )
        item = repo.review_watch_item(
            project.id,
            item.id,
            action="require_action",
            expected_version=1,
            verified_reviewer_id="reviewer",
        )
        with pytest.raises(NotFoundError):
            repo.propose_watch_item_change(
                other.id,
                item.id,
                expected_version=2,
                target_status="resolved",
                question="取得协议",
                owner=None,
                due_on=None,
                reason="跨项目不允许改动",
                verified_actor_id="analyst",
            )
        change = repo.propose_watch_item_change(
            project.id,
            item.id,
            expected_version=2,
            target_status="resolved",
            question="核对续约条款",
            owner="投资组",
            due_on=None,
            reason="已完成本轮材料核对",
            verified_actor_id="analyst",
        )
        assert change.status == "candidate"
        assert repo.list_watch_items(project.id)[0].resolved_at is None
        reviewed = repo.review_watch_item_change(
            project.id,
            item.id,
            change.id,
            action="accept",
            expected_version=2,
            verified_reviewer_id="reviewer",
        )
        assert reviewed.version == 3
        assert reviewed.resolved_at is not None
        assert reviewed.question == "核对续约条款"
        with pytest.raises(ReviewConflictError):
            repo.review_watch_item_change(
                project.id,
                item.id,
                change.id,
                action="accept",
                expected_version=2,
                verified_reviewer_id="reviewer",
            )
        assert repo.list_watch_item_changes(project.id)[0].status == "accepted"


def test_v10_upgrade_preserves_reviewed_watch_item_and_enables_changes(
    empty_test_database,
) -> None:
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in range(1, 11):
            script = postgres_migrations._migration_sql(version)
            connection.execute(script)
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, postgres_migrations._checksum(version)),
            )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('p', 'Synthetic Before', 'synthetic before', 'unverified', now()::text)"""
        )
        connection.execute(
            """INSERT INTO sources (id, project_id, kind, reference, content_sha256, created_at)
               VALUES ('s', 'p', 'document', 'old.txt', repeat('a', 64), now()::text)"""
        )
        connection.execute(
            """INSERT INTO observations
               (id, project_id, source_id, subject, statement, source_locator, status, version,
                created_at, fingerprint)
               VALUES ('o', 'p', 's', '风险与合规', '待核对', 'line 1', 'confirmed', 2,
                       now()::text, 'synthetic-fingerprint')"""
        )
        connection.execute(
            """INSERT INTO watch_items
               (id, project_id, observation_id, title, title_key, question, status, version,
                created_at, updated_at)
               VALUES ('w', 'p', 'o', '核对', '核对', '旧问题', 'watching', 2,
                       now()::text, now()::text)"""
        )
    assert postgres_migrations.inspect_postgres(dsn).version == 10
    assert postgres_migrations.upgrade_postgres(dsn, backup_verified=True).version == 12
    with psycopg.connect(dsn) as connection:
        connection.execute("SET search_path TO project_watch, public")
        row = connection.execute(
            "SELECT initial_question, current_observation_id, resolved_at FROM watch_items WHERE id = 'w'"
        ).fetchone()
        assert row == ("旧问题", "o", None)


def test_v9_upgrade_adds_empty_watch_items_without_changing_existing_project(
    empty_test_database,
) -> None:
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in range(1, 10):
            script = postgres_migrations._migration_sql(version)
            connection.execute(script)
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, hashlib.sha256(script.encode()).hexdigest()),
            )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('kept', 'Synthetic Existing', 'synthetic existing', 'unverified',
                       '2026-09-30T00:00:00+00:00')"""
        )
    assert postgres_migrations.inspect_postgres(dsn).version == 9
    with pytest.raises(postgres_migrations.MigrationError, match="verified backup"):
        postgres_migrations.upgrade_postgres(dsn)
    assert postgres_migrations.upgrade_postgres(dsn, backup_verified=True).version == 12
    with PostgresCoreRepository(dsn) as repo:
        assert repo.get_project("kept").name == "Synthetic Existing"
        assert repo.list_watch_items("kept") == []


def test_postgres_brave_worker_stops_paid_egress_after_budget(empty_test_database) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Synthetic Company", verified_actor_id="admin")
        plan = repo.create_monitoring_plan(
            project.id,
            kind="brave_news",
            target=brave_news_target(project.name),
            cadence_minutes=1440,
        )
        outbound = []

        def transport(url: str, key: str):
            outbound.append(url)
            return 200, json.dumps(
                {
                    "results": [
                        {
                            "title": "Synthetic Company announcement",
                            "url": "https://example.org/news",
                        }
                    ]
                }
            ).encode()

        adapter = BraveNewsAdapter(
            "synthetic-key",
            monthly_limit=1,
            reserve=repo.reserve_brave_news_request,
            transport=transport,
        )
        scheduler = PostgresMonitoringScheduler(repo, {"brave_news": adapter})
        repo.request_monitoring_run(project.id, plan.id, idempotency_key="first")
        assert scheduler.process_one().status == "done"
        assert repo.list_signals(project.id)[0].status == "candidate"
        repo.request_monitoring_run(project.id, plan.id, idempotency_key="second")
        assert scheduler.process_one().status == "done"
        runs = repo.list_source_runs(project.id, plan.id)
        assert runs[-1].status == "rate_limited"
        assert runs[-1].error_code == "quota_exhausted"
        assert len(outbound) == 1


def test_v8_upgrade_adds_paid_source_budget_without_changing_existing_project(
    empty_test_database,
) -> None:
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in range(1, 9):
            script = postgres_migrations._migration_sql(version)
            connection.execute(script)
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, hashlib.sha256(script.encode()).hexdigest()),
            )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('kept', 'Synthetic Existing', 'synthetic existing', 'unverified',
                       '2026-09-30T00:00:00+00:00')"""
        )
    assert postgres_migrations.inspect_postgres(dsn).version == 8
    assert postgres_migrations.upgrade_postgres(dsn, backup_verified=True).version == 12
    with PostgresCoreRepository(dsn) as repo:
        assert repo.get_project("kept").name == "Synthetic Existing"
        assert repo.brave_news_usage() == 0
        assert repo.reserve_brave_news_request(1)
        assert not repo.reserve_brave_news_request(1)


def test_v7_upgrade_adds_empty_peer_set_without_rewriting_existing_records(
    empty_test_database,
) -> None:
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in range(1, 8):
            script = postgres_migrations._migration_sql(version)
            connection.execute(script)
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, hashlib.sha256(script.encode()).hexdigest()),
            )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('kept', 'Synthetic Existing', 'synthetic existing', 'unverified',
                       '2026-09-30T00:00:00+00:00')"""
        )
    assert postgres_migrations.inspect_postgres(dsn).version == 7
    with pytest.raises(postgres_migrations.MigrationError, match="verified backup"):
        postgres_migrations.upgrade_postgres(dsn)
    assert postgres_migrations.upgrade_postgres(dsn, backup_verified=True).version == 12
    with PostgresCoreRepository(dsn) as repo:
        assert repo.get_project("kept").name == "Synthetic Existing"
        assert repo.list_competitive_peers("kept") == []
        assert repo.brave_news_usage() == 0


def test_postgres_bootstrap_is_explicit_atomic_and_project_scoped(empty_test_database, monkeypatch):
    dsn = empty_test_database
    assert postgres_migrations.inspect_postgres(dsn).version == 0

    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE TABLE public.project_watch_test_sentinel (id integer)")
    with pytest.raises(postgres_migrations.MigrationError, match="empty dedicated"):
        postgres_migrations.bootstrap_postgres(dsn)
    with psycopg.connect(dsn) as connection:
        connection.execute("DROP TABLE public.project_watch_test_sentinel")

    scripts = {
        version: postgres_migrations._migration_sql(version)
        for version in range(1, postgres_migrations.VERSION + 1)
    }
    monkeypatch.setattr(
        postgres_migrations,
        "_migration_sql",
        lambda version=1: (
            "CREATE TABLE probe (id int); BAD SQL" if version == 1 else scripts[version]
        ),
    )
    with pytest.raises(psycopg.Error):
        postgres_migrations.bootstrap_postgres(dsn)
    assert postgres_migrations.inspect_postgres(dsn).version == 0
    monkeypatch.setattr(
        postgres_migrations,
        "_migration_sql",
        lambda version=1: scripts[version],
    )

    state = postgres_migrations.bootstrap_postgres(dsn)
    assert state.version == postgres_migrations.VERSION
    assert len(state.checksum or "") == 64
    assert postgres_migrations.bootstrap_postgres(dsn) == state
    assert postgres_migrations.inspect_postgres(dsn) == state
    assert postgres_migrations.verify_postgres_schema(dsn) == state

    with psycopg.connect(dsn, options="-c search_path=project_watch,public") as connection:
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('a', 'A', 'a', 'unverified', '2026-09-26T00:00:00+00:00'),
                      ('b', 'B', 'b', 'unverified', '2026-09-26T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO sources (id, project_id, kind, reference, content_sha256, created_at)
               VALUES ('source-a', 'a', 'file', 'synthetic.txt', repeat('a', 64),
                       '2026-09-26T00:00:00+00:00')"""
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
            connection.execute(
                """INSERT INTO observations
                       (id, project_id, source_id, subject, statement, source_locator,
                        status, version, created_at, fingerprint)
                       VALUES ('cross-project', 'b', 'source-a', 'x', 'x', 'page 1',
                               'candidate', 1, '2026-09-26T00:00:00+00:00', 'x')"""
            )

    def insert_duplicate_source(source_id: str) -> bool:
        try:
            with psycopg.connect(dsn, options="-c search_path=project_watch,public") as conn:
                conn.execute(
                    """INSERT INTO sources
                       (id, project_id, kind, reference, content_sha256, created_at)
                       VALUES (%s, 'a', 'file', 'same.txt', repeat('b', 64),
                               '2026-09-26T00:00:01+00:00')""",
                    (source_id,),
                )
            return True
        except psycopg.errors.UniqueViolation:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(insert_duplicate_source, ("dup-1", "dup-2"))) == [False, True]

    monkeypatch.setattr(
        postgres_migrations,
        "_migration_sql",
        lambda version=1: scripts[version] + ("\n-- drift" if version == 3 else ""),
    )
    with pytest.raises(postgres_migrations.MigrationError, match="checksum"):
        postgres_migrations.bootstrap_postgres(dsn)
    with pytest.raises(postgres_migrations.MigrationError, match="checksum"):
        postgres_migrations.verify_postgres_schema(dsn)


def test_v1_upgrade_requires_backup_and_preserves_sources(empty_test_database, monkeypatch):
    dsn = empty_test_database
    scripts = {
        version: postgres_migrations._migration_sql(version)
        for version in range(1, postgres_migrations.VERSION + 1)
    }
    initial = scripts[1]
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        connection.execute(initial)
        connection.execute(
            "INSERT INTO schema_version (version, checksum) VALUES (1, %s)",
            (hashlib.sha256(initial.encode()).hexdigest(),),
        )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('project-v1', 'V1', 'v1', 'unverified', '2026-09-26T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO sources (id, project_id, kind, reference, content_sha256, created_at)
               VALUES ('source-v1', 'project-v1', 'document', 'old.txt', repeat('a', 64),
                       '2026-09-26T00:00:00+00:00')"""
        )
    assert postgres_migrations.inspect_postgres(dsn).version == 1
    with pytest.raises(postgres_migrations.MigrationError, match="explicit upgrade"):
        postgres_migrations.bootstrap_postgres(dsn)
    with pytest.raises(postgres_migrations.MigrationError, match="verified backup"):
        postgres_migrations.upgrade_postgres(dsn)
    with pytest.raises(postgres_migrations.MigrationError, match="mismatch"):
        postgres_migrations.verify_postgres_schema(dsn)

    monkeypatch.setattr(
        postgres_migrations,
        "_migration_sql",
        lambda version=1: (
            "CREATE TABLE probe (id int); BAD SQL" if version == 2 else scripts[version]
        ),
    )
    with pytest.raises(psycopg.Error):
        postgres_migrations.upgrade_postgres(dsn, backup_verified=True)
    assert postgres_migrations.inspect_postgres(dsn).version == 1
    monkeypatch.setattr(
        postgres_migrations,
        "_migration_sql",
        lambda version=1: scripts[version],
    )
    state = postgres_migrations.upgrade_postgres(dsn, backup_verified=True)
    assert state.version == postgres_migrations.VERSION
    assert postgres_migrations.verify_postgres_schema(dsn) == state
    with PostgresCoreRepository(dsn) as repo:
        sources = repo.list_sources("project-v1")
    assert len(sources) == 1 and sources[0].topic is None


def test_v2_upgrade_adds_review_impacts_without_touching_materials(empty_test_database):
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in (1, 2):
            script = postgres_migrations._migration_sql(version)
            connection.execute(script)
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, hashlib.sha256(script.encode()).hexdigest()),
            )
    assert postgres_migrations.inspect_postgres(dsn).version == 2
    with pytest.raises(postgres_migrations.MigrationError, match="verified backup"):
        postgres_migrations.upgrade_postgres(dsn)
    state = postgres_migrations.upgrade_postgres(dsn, backup_verified=True)
    assert state.version == postgres_migrations.VERSION
    assert postgres_migrations.verify_postgres_schema(dsn) == state
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("V2 Upgraded", verified_actor_id="admin")
        assert repo.list_review_impacts(project.id) == []


def test_v3_upgrade_preserves_source_receipts_and_requires_backup(empty_test_database, monkeypatch):
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in (1, 2, 3):
            script = postgres_migrations._migration_sql(version)
            connection.execute(script)
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, hashlib.sha256(script.encode()).hexdigest()),
            )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('legacy-project', 'Legacy', 'legacy', 'unverified',
                       '2026-09-26T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO monitoring_plans
               (id, project_id, kind, target, cadence_minutes, enabled, created_at)
               VALUES ('legacy-plan', 'legacy-project', 'rss',
                       'https://example.org/legacy.xml', 60, 0, '2026-09-26T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO source_runs
               (id, project_id, plan_id, status, started_at, finished_at, item_count, new_count)
               VALUES ('legacy-run', 'legacy-project', 'legacy-plan', 'completed',
                       '2026-09-26T00:00:00+00:00', '2026-09-26T00:00:01+00:00', 3, 2)"""
        )
    with pytest.raises(postgres_migrations.MigrationError, match="verified backup"):
        postgres_migrations.upgrade_postgres(dsn)
    assert postgres_migrations.inspect_postgres(dsn).version == 3
    original_migration_sql = postgres_migrations._migration_sql
    monkeypatch.setattr(
        postgres_migrations,
        "_migration_sql",
        lambda version: (
            "ALTER TABLE source_runs ADD COLUMN probe integer; BAD SQL"
            if version == 4
            else original_migration_sql(version)
        ),
    )
    with pytest.raises(psycopg.Error):
        postgres_migrations.upgrade_postgres(dsn, backup_verified=True)
    assert postgres_migrations.inspect_postgres(dsn).version == 3
    monkeypatch.setattr(postgres_migrations, "_migration_sql", original_migration_sql)
    state = postgres_migrations.upgrade_postgres(dsn, backup_verified=True)
    assert state.version == postgres_migrations.VERSION
    with PostgresCoreRepository(dsn) as repo:
        receipt = repo.list_source_runs("legacy-project", "legacy-plan")[0]
        assert (receipt.item_count, receipt.matched_count, receipt.new_count) == (3, None, 2)
        plan = repo.get_monitoring_plan("legacy-project", "legacy-plan")
        assert (plan.last_item_count, plan.last_matched_count) == (3, None)


def test_v4_identity_upgrade_is_explicit_atomic_and_preserves_project(
    empty_test_database, monkeypatch
):
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in range(1, 5):
            script = postgres_migrations._migration_sql(version)
            connection.execute(script)
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, hashlib.sha256(script.encode()).hexdigest()),
            )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('older-project', 'Older', 'older', 'unverified',
                       '2026-09-26T00:00:00+00:00')"""
        )
    with pytest.raises(postgres_migrations.MigrationError, match="verified backup"):
        postgres_migrations.upgrade_postgres(dsn)
    original = postgres_migrations._migration_sql
    monkeypatch.setattr(
        postgres_migrations,
        "_migration_sql",
        lambda version: (
            "CREATE TABLE project_identity_reviews (id text); BAD SQL"
            if version == 5
            else original(version)
        ),
    )
    with pytest.raises(psycopg.Error):
        postgres_migrations.upgrade_postgres(dsn, backup_verified=True)
    assert postgres_migrations.inspect_postgres(dsn).version == 4
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT to_regclass('project_watch.project_identity_reviews')"
            ).fetchone()[0]
            is None
        )
    monkeypatch.setattr(postgres_migrations, "_migration_sql", original)
    assert (
        postgres_migrations.upgrade_postgres(dsn, backup_verified=True).version
        == postgres_migrations.VERSION
    )
    with PostgresCoreRepository(dsn) as repo:
        project = repo.get_project("older-project")
        assert project is not None and project.identity_status == "unverified"
        assert repo.list_identity_reviews(project.id) == []


def test_v5_upgrade_preserves_numeric_observation_as_unknown_basis(empty_test_database):
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in range(1, 6):
            script = postgres_migrations._migration_sql(version)
            connection.execute(script)
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, hashlib.sha256(script.encode()).hexdigest()),
            )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('p', 'Synthetic', 'synthetic', 'unverified', '2026-09-26T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO sources (id, project_id, kind, reference, content_sha256, created_at)
               VALUES ('s', 'p', 'document', 'synthetic.xls', %s,
                       '2026-09-26T00:00:00+00:00')""",
            ("a" * 64,),
        )
        connection.execute(
            """INSERT INTO observations
               (id, project_id, source_id, subject, statement, number, unit, period,
                scope, source_locator, status, version, created_at, fingerprint)
               VALUES ('o', 'p', 's', '财务表现', '营业收入', '120', '万元', '2025',
                       NULL, 'sheet Synthetic row 2', 'confirmed', 2,
                       '2026-09-26T00:00:00+00:00', 'old-fingerprint')"""
        )
    with pytest.raises(postgres_migrations.MigrationError, match="verified backup"):
        postgres_migrations.upgrade_postgres(dsn)
    assert (
        postgres_migrations.upgrade_postgres(dsn, backup_verified=True).version
        == postgres_migrations.VERSION
    )
    with PostgresCoreRepository(dsn) as repo:
        old = repo.get_observation("p", "o")
        assert old is not None and old.basis == "unknown"
        assert old.number == Decimal("120") and old.status == "confirmed"


def test_v6_upgrade_keeps_legacy_rate_limit_and_accepts_new_status(empty_test_database):
    dsn = empty_test_database
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                version integer PRIMARY KEY CHECK (version > 0),
                checksum text NOT NULL CHECK (length(checksum) = 64),
                applied_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        for version in range(1, 7):
            script = postgres_migrations._migration_sql(version)
            connection.execute(script)
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, hashlib.sha256(script.encode()).hexdigest()),
            )
        connection.execute(
            """INSERT INTO projects (id, name, name_key, identity_status, created_at)
               VALUES ('p', 'Synthetic', 'synthetic', 'unverified', '2026-09-26T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO monitoring_plans
               (id, project_id, kind, target, cadence_minutes, enabled, created_at)
               VALUES ('plan', 'p', 'rss', 'https://example.org/feed', 1440, 1,
                       '2026-09-26T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO source_runs
               (id, project_id, plan_id, status, error_code, started_at, finished_at)
               VALUES ('old', 'p', 'plan', 'unreachable', 'rate_limited',
                       '2026-09-26T00:00:00+00:00', '2026-09-26T00:00:01+00:00')"""
        )
    with pytest.raises(postgres_migrations.MigrationError, match="verified backup"):
        postgres_migrations.upgrade_postgres(dsn)
    assert (
        postgres_migrations.upgrade_postgres(dsn, backup_verified=True).version
        == postgres_migrations.VERSION
    )
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL search_path TO project_watch, public")
        legacy = connection.execute(
            "SELECT status, error_code FROM source_runs WHERE id = 'old'"
        ).fetchone()
        assert legacy == ("unreachable", "rate_limited")
        connection.execute(
            """INSERT INTO source_runs
               (id, project_id, plan_id, status, error_code, started_at, finished_at)
               VALUES ('new', 'p', 'plan', 'rate_limited', 'rate_limited',
                       '2026-09-27T00:00:00+00:00', '2026-09-27T00:00:01+00:00')"""
        )
    with PostgresCoreRepository(dsn) as repo:
        latest = repo.get_monitoring_plan("p", "plan")
        assert latest.last_run_status == "rate_limited"
        assert latest.last_run_error_code == "rate_limited"


def test_postgres_project_identity_review_keeps_evidence_and_history(empty_test_database):
    from project_watch.repository import IdentityConflictError

    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Example Project", verified_actor_id="admin")
        other = repo.create_project("Other Project", verified_actor_id="admin")
        foreign = repo.add_source(
            other.id,
            kind="document",
            reference="other.txt",
            content_sha256="a" * 64,
        )
        with pytest.raises(ValueError, match="selected project"):
            repo.verify_project_identity(
                project.id,
                legal_name="Example Company Limited",
                registration_code=None,
                evidence_source_id=foreign.id,
                evidence_locator="page:1",
                evidence_url=None,
                note="Checked the name against this source",
                expected_version=0,
                actor_user_id="admin",
            )
        verified = repo.verify_project_identity(
            project.id,
            legal_name="Example Company Limited",
            registration_code="123456",
            evidence_source_id=None,
            evidence_locator=None,
            evidence_url="https://example.org/company",
            note="Checked the public company page",
            expected_version=0,
            actor_user_id="admin",
        )
        assert verified.version == 1
        assert repo.get_project(project.id).identity_status == "verified"
        with pytest.raises(IdentityConflictError):
            repo.reopen_project_identity(
                project.id,
                note="Name needs another review",
                expected_version=0,
                actor_user_id="admin",
            )
        reopened = repo.reopen_project_identity(
            project.id,
            note="Name needs another review",
            expected_version=1,
            actor_user_id="admin",
        )
        assert reopened.version == 2
        assert repo.get_project(project.id).identity_status == "unverified"
        assert [item.action for item in repo.list_identity_reviews(project.id)] == [
            "reopen",
            "verify",
        ]
        assert [
            event.action
            for event in repo.list_audit_events(project.id)
            if event.action.startswith("identity.")
        ] == ["identity.verified", "identity.reopened"]


def test_postgres_identity_api_requires_manager_and_csrf(empty_test_database, tmp_path: Path):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth:
        admin = auth.bootstrap_admin("identity-admin", "long-synthetic-password")
        viewer = auth.create_user(
            "identity-viewer", "long-synthetic-password", actor_user_id=admin.id
        )
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Identity API Project", verified_actor_id=admin.id)
        other = repo.create_project("Other API Project", verified_actor_id=admin.id)
    with PostgresTeamAuthStore(dsn) as auth:
        auth.grant_project_role(project.id, viewer.id, "viewer", actor_user_id=admin.id)
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    path = f"/api/projects/{project.id}/identity"
    with TestClient(app, base_url="https://testserver") as admin_client:
        assert admin_client.get(path).status_code == 401
        csrf = admin_client.post(
            "/api/auth/login",
            json={"username": "identity-admin", "password": "long-synthetic-password"},
        ).json()["csrf_token"]
        payload = {
            "legal_name": "Identity API Company Limited",
            "registration_code": None,
            "evidence_source_id": None,
            "evidence_locator": None,
            "evidence_url": "https://example.org/company",
            "note": "Checked the public entity page",
            "expected_version": 0,
        }
        assert admin_client.post(path, json=payload).status_code == 403
        created = admin_client.post(path, json=payload, headers={"X-CSRF-Token": csrf})
        assert created.status_code == 201
        assert created.json()["actor_name"] == "identity-admin"
        assert admin_client.get(path).json()["status"] == "verified"
        assert (
            admin_client.post(path, json=payload, headers={"X-CSRF-Token": csrf}).status_code == 409
        )
        with TestClient(app, base_url="https://testserver") as viewer_client:
            viewer_csrf = viewer_client.post(
                "/api/auth/login",
                json={"username": "identity-viewer", "password": "long-synthetic-password"},
            ).json()["csrf_token"]
            assert viewer_client.get(path).status_code == 200
            assert viewer_client.get(f"/api/projects/{other.id}/identity").status_code == 404
            assert (
                viewer_client.post(
                    f"{path}/reopen",
                    json={"note": "Need another check", "expected_version": 1},
                    headers={"X-CSRF-Token": viewer_csrf},
                ).status_code
                == 404
            )


def test_postgres_new_material_flags_only_matching_confirmed_observation(
    empty_test_database, tmp_path: Path
):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Incremental Synthetic", verified_actor_id="admin")
        other = repo.create_project("Other Synthetic", verified_actor_id="admin")
        first = repo.add_source(
            project.id, kind="document", reference="finance-1.txt", content_sha256="a" * 64
        )
        repo.enqueue_intake(project.id, first.id)
        first_job = repo.claim_next_intake()
        repo.complete_intake(
            project.id,
            first_job.id,
            [FragmentInput(1, "line 1", "营业收入待核对")],
            expected_attempt=first_job.attempts,
            native_topic="财务表现",
            native_topic_basis="content",
        )
        observation = repo.add_observation(
            project.id,
            source_id=first.id,
            subject="收入判断",
            statement="营业收入待核对",
            source_locator="line 1",
            verified_actor_id="admin",
        )
        repo.confirm_observation(
            project.id, observation.id, verified_reviewer_id="admin", expected_version=1
        )
        newer = repo.add_source(
            project.id, kind="document", reference="finance-2.txt", content_sha256="b" * 64
        )
        repo.enqueue_intake(project.id, newer.id)
        job = repo.claim_next_intake()
        repo.complete_intake(
            project.id,
            job.id,
            [FragmentInput(1, "line 1", "财务变化")],
            expected_attempt=job.attempts,
            native_topic="财务表现",
            native_topic_basis="content",
        )
        impacts = repo.list_review_impacts(project.id)
        assert len(impacts) == 1
        assert impacts[0].trigger_source_id == newer.id
        assert impacts[0].observation_id == observation.id
        assert repo.list_review_impacts(other.id) == []
    with PostgresTeamAuthStore(dsn) as auth:
        auth.bootstrap_admin("impact-admin", "long-synthetic-password")
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    with TestClient(app, base_url="https://testserver") as client:
        path = f"/api/projects/{project.id}/review-impacts"
        assert client.get(path).status_code == 401
        login = client.post(
            "/api/auth/login",
            json={"username": "impact-admin", "password": "long-synthetic-password"},
        )
        assert login.status_code == 200
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
        assert client.get(path).json()[0]["id"] == impacts[0].id
        report_path = f"/api/projects/{project.id}/report/current"
        pending_brief = client.get(report_path).json()
        assert pending_brief["pending_review_count"] == 1
        assert pending_brief["review_impacts"][0]["impact_id"] == impacts[0].id
        assert pending_brief["review_impacts"][0]["old_source_locator"] == "line 1"
        assert pending_brief["review_impacts"][0]["new_source_reference"] == "finance-2.txt"
        assert (
            pending_brief["confirmed_facts"][0]["review_proof"]["reviewer_authentication"]
            == "authenticated_team"
        )
        assert pending_brief["confirmed_facts"][0]["review_proof"]["reviewer_ref"] == "admin"
        assert (
            client.post(
                f"{path}/{impacts[0].id}/resolve",
                json={"resolution": "unchanged", "note": "已读新资料，暂保留原判断。"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/projects/{other.id}/review-impacts/{impacts[0].id}/resolve",
                json={"resolution": "unchanged", "note": "跨项目不得处理"},
                headers=csrf,
            ).status_code
            == 404
        )
        resolved = client.post(
            f"{path}/{impacts[0].id}/resolve",
            json={"resolution": "unchanged", "note": "已读新资料，暂保留原判断。"},
            headers=csrf,
        )
        assert resolved.status_code == 200
        assert resolved.json()["reviewer_ref"] == login.json()["user_id"]
        reviewed_brief = client.get(report_path).json()
        assert reviewed_brief["pending_review_count"] == 0
        assert reviewed_brief["review_impacts"][0]["status"] == "reviewed"
        assert reviewed_brief["review_impacts"][0]["note"] == "已读新资料，暂保留原判断。"
        assert reviewed_brief["confirmed_facts"][0]["statement"] == "营业收入待核对"
        assert (
            client.post(
                f"{path}/{impacts[0].id}/resolve",
                json={"resolution": "follow_up", "note": "重复提交"},
                headers=csrf,
            ).status_code
            == 409
        )
    with PostgresCoreRepository(dsn) as repo:
        assert repo.get_observation(project.id, observation.id).status == "confirmed"
        assert any(
            event.action == "impact.reviewed" for event in repo.list_audit_events(project.id)
        )


def test_postgres_mixed_material_flags_each_located_topic_once(empty_test_database) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Mixed Update Synthetic", verified_actor_id="admin")
        existing: set[str] = set()
        for topic, digest, statement in (
            ("财务表现", "a" * 64, "旧收入判断"),
            ("产品与技术", "b" * 64, "旧产品判断"),
        ):
            source = repo.add_source(
                project.id, kind="document", reference=f"{topic}.txt", content_sha256=digest
            )
            repo.enqueue_intake(project.id, source.id)
            job = repo.claim_next_intake()
            repo.complete_intake(
                project.id,
                job.id,
                [FragmentInput(1, "line 1", statement)],
                expected_attempt=job.attempts,
                native_topic=topic,
                native_topic_basis="content",
            )
            observation = repo.add_observation(
                project.id,
                source_id=source.id,
                subject=topic,
                statement=statement,
                source_locator="line 1",
                verified_actor_id="admin",
            )
            repo.confirm_observation(
                project.id, observation.id, verified_reviewer_id="admin", expected_version=1
            )
            existing.add(observation.id)

        updated = repo.add_source(
            project.id, kind="document", reference="mixed-update.txt", content_sha256="c" * 64
        )
        fragments = [
            FragmentInput(1, "line 1", "营业收入有变化。"),
            FragmentInput(2, "line 2", "产品路线有变化。"),
        ]
        repo.enqueue_intake(project.id, updated.id)
        job = repo.claim_next_intake()
        repo.complete_intake(
            project.id,
            job.id,
            fragments,
            expected_attempt=job.attempts,
            native_topic="财务表现",
            native_topic_basis="content",
            native_candidates=(
                NativeCandidate("财务表现", fragments[0].text, fragments[0].locator),
                NativeCandidate("产品与技术", fragments[1].text, fragments[1].locator),
            ),
        )
        impacts = repo.list_review_impacts(project.id)
        assert {impact.observation_id for impact in impacts} == existing
        assert len(impacts) == 2
        assert all(impact.trigger_source_id == updated.id for impact in impacts)


def test_postgres_metric_series_is_confirmed_and_project_scoped(
    empty_test_database, tmp_path: Path
) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Metric Synthetic", verified_actor_id="admin")
        other = repo.create_project("Other Metric Synthetic", verified_actor_id="admin")
        source = repo.add_source(
            project.id, kind="document", reference="synthetic.xlsx", content_sha256="c" * 64
        )
        candidate = repo.add_observation(
            project.id,
            source_id=source.id,
            subject="财务表现",
            statement="营业收入",
            number=Decimal("12.5"),
            unit="万元",
            period="2025",
            scope="合并",
            source_locator="利润表!B3",
            verified_actor_id="admin",
        )
    with PostgresTeamAuthStore(dsn) as auth:
        auth.bootstrap_admin("metric-admin", "long-synthetic-password")
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    with TestClient(app, base_url="https://testserver") as client:
        route = f"/api/projects/{project.id}/metric-series"
        assert client.get(route).status_code == 401
        login = client.post(
            "/api/auth/login",
            json={"username": "metric-admin", "password": "long-synthetic-password"},
        )
        assert login.status_code == 200
        assert client.get(route).json()["series"] == []
        assert client.get(f"/api/projects/{other.id}/metric-series").json()["series"] == []
        with PostgresCoreRepository(dsn) as repo:
            repo.confirm_observation(
                project.id, candidate.id, verified_reviewer_id="admin", expected_version=1
            )
        result = client.get(route).json()
        assert result["series"][0]["points"][0]["number"] == "12.5"
        assert result["series"][0]["points"][0]["source_sha256"] == "c" * 64
        assert client.get(f"/api/projects/{other.id}/metric-series").json()["series"] == []


def test_postgres_core_concurrency_restart_and_keyset_pages(empty_test_database):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Synthetic A", verified_actor_id="user-a")
        other = repo.create_project("Synthetic B", verified_actor_id="user-a")

        def source(_index):
            return repo.add_source(
                project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            sources = list(pool.map(source, range(16)))
        assert len({item.id for item in sources}) == 1
        assert repo.get_source(other.id, sources[0].id) is None

        def propose(_index):
            return repo.add_observation(
                project.id,
                source_id=sources[0].id,
                subject="Revenue",
                statement="Synthetic revenue is unknown",
                source_locator="page 1",
                verified_actor_id="user-a",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            observations = list(pool.map(propose, range(16)))
        assert len({item.id for item in observations}) == 1
        candidate = observations[0]
        assert repo.get_observation(other.id, candidate.id) is None
        assert [event.action for event in repo.list_audit_events(project.id)].count(
            "candidate.created"
        ) == 1

        def confirm(_index):
            try:
                return repo.confirm_observation(
                    project.id, candidate.id, verified_reviewer_id="reviewer-a", expected_version=1
                ).status
            except ReviewConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(confirm, range(2))) == ["confirmed", "conflict"]
        assert len(repo.review_history(project.id, candidate.id)) == 1
        assert [event.action for event in repo.list_audit_events(project.id)].count(
            "candidate.confirmed"
        ) == 1

        for index in range(230):
            repo.create_project(f"Synthetic {index:03d}", verified_actor_id="user-a")
        seen = []
        cursor = None
        while page := repo.list_projects_page(after=cursor, limit=37):
            seen.extend(item.id for item in page)
            last = page[-1]
            cursor = (last.created_at.isoformat(), last.id)
        assert len(seen) == 232
        assert len(set(seen)) == 232

    with PostgresCoreRepository(dsn) as reopened:
        assert reopened.get_observation(project.id, candidate.id).status == "confirmed"
        assert reopened.get_source(other.id, sources[0].id) is None
        assert len(reopened.review_history(project.id, candidate.id)) == 1


def test_postgres_coverage_tasks_are_project_scoped_and_idempotent(empty_test_database):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Coverage Synthetic", verified_actor_id="admin")
        other = repo.create_project("Other Coverage", verified_actor_id="admin")
        source = repo.add_source(
            project.id, kind="document", reference="source.txt", content_sha256="a" * 64
        )
        foreign = repo.add_source(
            other.id, kind="document", reference="other.txt", content_sha256="b" * 64
        )
        with pytest.raises(ValueError, match="another project"):
            repo.save_coverage_task(
                project.id,
                source_ids=(foreign.id,),
                idempotency_key="wrong-source",
                outcome={"source_count": 1},
            )

        def save(_index):
            return repo.save_coverage_task(
                project.id,
                source_ids=(source.id,),
                idempotency_key="same-request",
                outcome={"source_count": 1, "covered_topics": []},
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = list(pool.map(save, range(16)))
        assert len({task.id for task in tasks}) == 1
        task = tasks[0]
        assert task.kind == "coverage" and task.status == "completed"
        assert task.source_ids == (source.id,)
        assert repo.get_execution_task(other.id, task.id) is None
        assert repo.list_execution_tasks(other.id) == []
        with pytest.raises(ValueError, match="different request"):
            repo.save_coverage_task(
                project.id,
                source_ids=(),
                idempotency_key="same-request",
                outcome={"source_count": 0},
            )
        assert [entry.id for entry in repo.list_execution_tasks(project.id)] == [task.id]
        with pytest.raises(ValueError, match="limit"):
            repo.list_execution_tasks(project.id, limit=101)
    with PostgresCoreRepository(dsn) as reopened:
        assert reopened.get_execution_task(project.id, task.id) == task


def test_postgres_task_service_uses_only_current_extracted_evidence(empty_test_database):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Task Synthetic", verified_actor_id="reviewer")
        source = repo.add_source(
            project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64
        )
        intake = repo.enqueue_intake(project.id, source.id)
        claimed = repo.claim_next_intake()
        assert claimed.id == intake.id
        repo.complete_intake(
            project.id,
            intake.id,
            [FragmentInput(ordinal=1, locator="page 1", text="Synthetic")],
            expected_attempt=claimed.attempts,
        )
        service = PostgresTaskService(repo)
        with pytest.raises(EvidenceConflictError):
            service.propose_candidate(
                project.id,
                source_id=source.id,
                expected_source_sha256="b" * 64,
                source_locator="page 1",
                subject="公司与团队",
                statement="Only synthetic",
                verified_actor_id="reviewer",
            )
        with pytest.raises(ValueError, match="locator"):
            service.propose_candidate(
                project.id,
                source_id=source.id,
                expected_source_sha256="a" * 64,
                source_locator="page 2",
                subject="公司与团队",
                statement="Only synthetic",
                verified_actor_id="reviewer",
            )
        candidate = service.propose_candidate(
            project.id,
            source_id=source.id,
            expected_source_sha256="a" * 64,
            source_locator="page 1",
            subject="公司与团队",
            statement="Only synthetic",
            verified_actor_id="reviewer",
        )
        assert candidate.status == "candidate"
        task = service.run_coverage(
            project.id, source_ids=(source.id,), idempotency_key="coverage-1"
        )
        assert task.outcome["candidate_count"] == 1
        assert task.outcome["covered_topics"] == ["公司与团队"]
        assert task.outcome["confirmed_count"] == 0


def test_postgres_intake_creates_system_candidate_without_confirming(
    empty_test_database, tmp_path: Path
) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    objects = BlobStore(tmp_path / "objects")
    blob = objects.put_stream("财务报表.txt", io.BytesIO("营业收入 1,200 万元。".encode()))
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Native Intake Synthetic", verified_actor_id="admin")
        source = repo.add_source(
            project.id, kind="document", reference="财务报表.txt", content_sha256=blob.sha256
        )
        repo.enqueue_intake(project.id, source.id)

        assert PostgresIntakeWorker(repo, objects).process_one().status == "completed"
        indexed = repo.list_sources(project.id)[0]
        assert indexed.topic == "财务表现"
        assert indexed.topic_basis == "filename_and_content"
        candidates = repo.list_observations(project.id)
        assert len(candidates) == 1
        assert candidates[0].statement == "营业收入 1,200 万元。"
        assert candidates[0].status == "candidate"
        audit = [
            row for row in repo.list_audit_events(project.id) if row.channel == "native_intake"
        ]
        assert len(audit) == 1
        assert audit[0].actor_kind == "system"
        assert audit[0].source_sha256 == blob.sha256


def test_postgres_xlsx_intake_proposes_numeric_financial_candidate(
    empty_test_database, tmp_path: Path
) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "合并利润表"
    sheet.append(["单位：万元"])
    sheet.append(["项目", "2025年实际"])
    sheet.append(["营业收入", 120])
    payload = io.BytesIO()
    workbook.save(payload)
    objects = BlobStore(tmp_path / "objects")
    blob = objects.put_stream("synthetic.xlsx", io.BytesIO(payload.getvalue()))
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Synthetic Finance Map", verified_actor_id="admin")
        source = repo.add_source(
            project.id, kind="document", reference="synthetic.xlsx", content_sha256=blob.sha256
        )
        repo.enqueue_intake(project.id, source.id)
        assert PostgresIntakeWorker(repo, objects).process_one().status == "completed"
        candidates = [
            item for item in repo.list_observations(project.id) if item.statement == "营业收入"
        ]
        assert len(candidates) == 1
        assert candidates[0].number == Decimal("120")
        assert candidates[0].period == "2025"
        assert candidates[0].basis == "actual"
        assert candidates[0].status == "candidate"
        assert candidates[0].source_locator == "sheet 合并利润表 row 3"
        assert repo.report_locator_status(
            project.id,
            [(source.id, candidates[0].source_locator), (source.id, "sheet 合并利润表 row 99")],
        ) == {
            (source.id, candidates[0].source_locator): "indexed_exact",
            (source.id, "sheet 合并利润表 row 99"): "locator_not_found",
        }
        unindexed = repo.add_source(
            project.id, kind="document", reference="not-yet-parsed.txt", content_sha256="b" * 64
        )
        assert repo.report_locator_status(project.id, [(unindexed.id, "line 1")]) == {
            (unindexed.id, "line 1"): "not_indexed"
        }
        repo.confirm_observation(
            project.id, candidates[0].id, verified_reviewer_id="admin", expected_version=1
        )
    with PostgresTeamAuthStore(dsn) as auth:
        auth.bootstrap_admin("mapping-admin", "long-synthetic-password")
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/api/auth/login",
            json={"username": "mapping-admin", "password": "long-synthetic-password"},
        )
        mapping = client.get(f"/api/projects/{project.id}/sources/{source.id}/financial-mapping")
        assert mapping.status_code == 200
        assert {k: mapping.json()[k] for k in ("candidate_count", "truncated_count", "issues")} == {"candidate_count": 1, "truncated_count": 0, "issues": []}
        assert len(mapping.json()["review_drafts"]) == 1
        brief = client.get(f"/api/projects/{project.id}/report/current")
        assert brief.status_code == 200
        assert brief.json()["confirmed_facts"][0]["locator_status"] == "indexed_exact"


def test_postgres_invalid_native_candidate_rolls_back_intake(empty_test_database) -> None:
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Native Rollback Synthetic", verified_actor_id="admin")
        source = repo.add_source(
            project.id, kind="document", reference="财务.txt", content_sha256="a" * 64
        )
        repo.enqueue_intake(project.id, source.id)
        job = repo.claim_next_intake()

        with pytest.raises(ValueError, match="source excerpt"):
            repo.complete_intake(
                project.id,
                job.id,
                [FragmentInput(1, "line 1", "营业收入仍需核实")],
                expected_attempt=job.attempts,
                native_candidates=(NativeCandidate("财务表现", "收入已核实", "line 1"),),
            )

        assert repo.list_fragments(project.id, source.id) == []
        assert repo.list_observations(project.id) == []
        assert repo.get_intake_job(project.id, job.id).status == "processing"
        with pytest.raises(ValueError, match="index"):
            repo.complete_intake(
                project.id,
                job.id,
                [FragmentInput(1, "line 1", "营业收入仍需核实")],
                expected_attempt=job.attempts,
                native_topic="错误",
                native_topic_basis="content",
            )
        assert repo.list_sources(project.id)[0].topic is None


def test_postgres_workers_process_synthetic_material_and_monitoring_receipts(
    empty_test_database, tmp_path: Path
):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    objects = BlobStore(tmp_path / "objects")
    blob = objects.put_stream("synthetic.txt", io.BytesIO(b"Only synthetic research text"))
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Workers Synthetic", verified_actor_id="admin")
        source = repo.add_source(
            project.id,
            kind="document",
            reference="synthetic.txt",
            content_sha256=blob.sha256,
        )
        intake = repo.enqueue_intake(project.id, source.id)
        processed = PostgresIntakeWorker(repo, objects).process_one()
        assert processed.id == intake.id and processed.status == "completed"
        assert [fragment.text for fragment in repo.list_fragments(project.id, source.id)] == [
            "Only synthetic research text"
        ]

        target = "https://example.org/synthetic-feed.xml"
        plan = repo.create_monitoring_plan(
            project.id, kind="rss", target=target, cadence_minutes=60
        )
        adapter = ManualSnapshotAdapter(
            {
                target: (
                    FeedItem(
                        "synthetic-1",
                        "Workers Synthetic release",
                        "https://example.org/item/1",
                        "Synthetic only",
                        datetime.now(UTC),
                    ),
                    FeedItem(
                        "synthetic-2",
                        "Unrelated release",
                        "https://example.org/item/2",
                        "No project mention",
                        datetime.now(UTC),
                    ),
                )
            }
        )
        scheduler = PostgresMonitoringScheduler(repo, {"rss": adapter})
        repo.request_monitoring_run(project.id, plan.id, idempotency_key="manual-synthetic")
        done = scheduler.process_one()
        assert done.status == "done"
        receipt = repo.list_source_runs(project.id, plan.id)[0]
        assert receipt.status == "completed" and receipt.new_count == 1
        assert (receipt.item_count, receipt.matched_count) == (2, 1)
        assert repo.get_monitoring_plan(project.id, plan.id).last_matched_count == 1
        assert len(repo.list_signals(project.id)) == 1

        other_project = repo.create_project("Elsewhere Synthetic", verified_actor_id="admin")
        other_plan = repo.create_monitoring_plan(
            other_project.id, kind="rss", target=target, cadence_minutes=60
        )
        repo.request_monitoring_run(other_project.id, other_plan.id, idempotency_key="other")
        assert scheduler.process_one().status == "done"
        other_receipt = repo.list_source_runs(other_project.id, other_plan.id)[0]
        assert (other_receipt.item_count, other_receipt.matched_count) == (2, 0)
        assert repo.list_signals(other_project.id) == []

        missing = repo.create_monitoring_plan(
            project.id,
            kind="rss",
            target="https://example.org/missing.xml",
            cadence_minutes=60,
        )
        repo.request_monitoring_run(project.id, missing.id, idempotency_key="missing-feed")
        assert scheduler.process_one().status == "done"
        missing_receipt = repo.list_source_runs(project.id, missing.id)[0]
        assert missing_receipt.status == "unreachable"
        assert missing_receipt.error_code == "snapshot_missing"


def test_postgres_team_api_binds_auth_project_and_review_to_verified_users(
    empty_test_database, tmp_path: Path
):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth:
        admin = auth.bootstrap_admin("api-admin", "long-synthetic-password")
    target = "https://example.org/team-api-synthetic.xml"
    second_target = "https://other.example.org/team-api-synthetic.xml"
    app = create_postgres_team_app(
        dsn,
        object_root=tmp_path / "objects",
        team_origin="https://testserver",
        monitoring_adapters={
            "rss": ManualSnapshotAdapter(
                {
                    target: (
                        FeedItem(
                            "team-api-item",
                            "Team API Synthetic monitoring item",
                            "https://example.org/team-api-item",
                            "Synthetic summary",
                            datetime.now(UTC),
                        ),
                    ),
                    second_target: (
                        FeedItem(
                            "other-feed-item",
                            "Team API Synthetic monitoring item",
                            "https://example.org/team-api-item?utm_source=second",
                            "Other feed summary",
                            datetime.now(UTC),
                        ),
                    ),
                }
            )
        },
    )
    with TestClient(app, base_url="https://testserver") as client:
        browser = client.get("/")
        assert browser.status_code == 200
        assert "text/html" in browser.headers["content-type"]
        assert "login-screen" in browser.text
        static_script = client.get("/static/app.js")
        assert static_script.status_code == 200
        assert static_script.headers["cache-control"] == "no-store"
        assert client.get("/api/projects").status_code == 401
        assert TestClient(app, base_url="http://testserver").get("/").status_code == 400
        logged = client.post(
            "/api/auth/login",
            json={"username": "api-admin", "password": "long-synthetic-password"},
        )
        assert logged.status_code == 200
        csrf = logged.json()["csrf_token"]
        assert client.post("/api/projects", json={"name": "Denied"}).status_code == 403
        headers = {"X-CSRF-Token": csrf}
        project = client.post("/api/projects", json={"name": "Team API Synthetic"}, headers=headers)
        assert project.status_code == 201
        project_id = project.json()["id"]
        assert client.get(f"/api/projects/{project_id}/capabilities").json() == {
            "read": True,
            "propose": True,
            "review": True,
            "manage": True,
        }
        other = client.post("/api/projects", json={"name": "Other Team API"}, headers=headers)
        assert other.status_code == 201
        other_id = other.json()["id"]
        plan = client.post(
            f"/api/projects/{project_id}/monitoring/plans",
            json={"kind": "rss", "target": target, "cadence_minutes": 60},
            headers=headers,
        )
        assert plan.status_code == 201
        plan_id = plan.json()["id"]
        assert plan.json()["last_run_status"] == "not_run"
        assert (
            client.patch(
                f"/api/projects/{other_id}/monitoring/plans/{plan_id}",
                json={"enabled": True},
                headers=headers,
            ).status_code
            == 404
        )
        queued = client.post(
            f"/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
            json={"idempotency_key": "team-api-run"},
            headers=headers,
        )
        assert queued.status_code == 202
        assert (
            client.post(
                f"/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
                json={"idempotency_key": "team-api-run"},
                headers=headers,
            ).json()["id"]
            == queued.json()["id"]
        )
        assert app.state.monitoring_scheduler.process_one().status == "done"
        runs = client.get(f"/api/projects/{project_id}/monitoring/plans/{plan_id}/runs").json()
        assert runs[0]["status"] == "completed" and runs[0]["new_count"] == 1
        assert (runs[0]["item_count"], runs[0]["matched_count"]) == (1, 1)
        assert len(client.get(f"/api/projects/{project_id}/monitoring/signals").json()) == 1
        second_plan = client.post(
            f"/api/projects/{project_id}/monitoring/plans",
            json={"kind": "rss", "target": second_target, "cadence_minutes": 60},
            headers=headers,
        ).json()["id"]
        assert (
            client.post(
                f"/api/projects/{project_id}/monitoring/plans/{second_plan}/runs",
                json={"idempotency_key": "team-api-second-source"},
                headers=headers,
            ).status_code
            == 202
        )
        assert app.state.monitoring_scheduler.process_one().status == "done"
        with PostgresCoreRepository(dsn) as repo:
            assert len(repo.list_signals(project_id)) == 2
        assert len(client.get(f"/api/projects/{project_id}/monitoring/signals").json()) == 1
        assert (
            len(client.get(f"/api/projects/{project_id}/report/current").json()["external_leads"])
            == 1
        )
        issued = client.post(
            f"/api/projects/{project_id}/executor-tokens",
            json={"label": "synthetic-executor", "permission": "propose", "duration_minutes": 5},
            headers=headers,
        )
        assert issued.status_code == 201
        token_id = issued.json()["id"]
        secret = issued.json()["token"]
        token_list = client.get(f"/api/projects/{project_id}/executor-tokens").json()
        assert token_list[0]["id"] == token_id and "token" not in token_list[0]
        with PostgresTeamAuthStore(dsn) as token_auth:
            assert token_auth.executor_grant(secret).project_id == project_id
        assert (
            client.delete(
                f"/api/projects/{other_id}/executor-tokens/{token_id}", headers=headers
            ).status_code
            == 404
        )
        revoked_token = client.delete(
            f"/api/projects/{project_id}/executor-tokens/{token_id}", headers=headers
        )
        assert revoked_token.status_code == 204
        assert revoked_token.content == b""
        with PostgresTeamAuthStore(dsn) as token_auth:
            assert token_auth.executor_grant(secret) is None
        created_analyst = client.post(
            "/api/team/users",
            json={"username": "api-analyst", "password": "analyst-synthetic-password"},
            headers=headers,
        )
        created_reviewer = client.post(
            "/api/team/users",
            json={"username": "api-reviewer", "password": "reviewer-synthetic-password"},
            headers=headers,
        )
        assert created_analyst.status_code == created_reviewer.status_code == 201
        analyst_id = created_analyst.json()["id"]
        reviewer_id = created_reviewer.json()["id"]
        for user_id, role in ((analyst_id, "analyst"), (reviewer_id, "reviewer")):
            granted = client.put(
                f"/api/projects/{project_id}/members/{user_id}",
                json={"role": role},
                headers=headers,
            )
            assert granted.status_code == 204
            assert granted.content == b""
        members_path = f"/api/projects/{project_id}/members"
        assert client.get(members_path).status_code == 200
        assert {row["username"] for row in client.get(members_path).json()} == {
            "api-analyst",
            "api-reviewer",
        }
        assert client.get(members_path).headers["cache-control"] == "no-store"
        assert (
            client.post(
                members_path, json={"username": "api-reviewer", "role": "reviewer"}
            ).status_code
            == 403
        )
        reassigned = client.post(
            members_path,
            json={"username": " API-REVIEWER ", "role": "reviewer"},
            headers=headers,
        )
        assert reassigned.status_code == 201
        assert reassigned.json()["user_id"] == reviewer_id
        assert (
            client.post(
                members_path,
                json={"username": "missing-synthetic", "role": "viewer"},
                headers=headers,
            ).status_code
            == 404
        )
        assert client.get(members_path, params={"limit": 1}).json()[0]["username"] == "api-analyst"
        with PostgresTeamAuthStore(dsn) as directory:
            assert directory.find_active_user(" API-REVIEWER ").id == reviewer_id
            assert len(directory.list_project_members(other_id)) == 0

        analyst = TestClient(app, base_url="https://testserver")
        analyst_login = analyst.post(
            "/api/auth/login",
            json={"username": "api-analyst", "password": "analyst-synthetic-password"},
        )
        analyst_csrf = {"X-CSRF-Token": analyst_login.json()["csrf_token"]}
        assert [row["id"] for row in analyst.get("/api/projects").json()] == [project_id]
        assert analyst.get(f"/api/projects/{project_id}/capabilities").json() == {
            "read": True,
            "propose": True,
            "review": False,
            "manage": False,
        }
        assert analyst.get(f"/api/projects/{other_id}").status_code == 404
        assert analyst.get(f"/api/projects/{other_id}/sources").status_code == 404
        assert analyst.get(f"/api/projects/{other_id}/monitoring/plans").status_code == 404
        assert (
            analyst.get(f"/api/projects/{project_id}/monitoring/plans").json()[0]["last_run_status"]
            == "completed"
        )
        assert (
            analyst.post(
                f"/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
                json={"idempotency_key": "analyst-must-not-run"},
                headers=analyst_csrf,
            ).status_code
            == 404
        )
        assert analyst.get(f"/api/projects/{project_id}/executor-tokens").status_code == 404
        assert analyst.get(members_path).status_code == 404
        assert analyst.get(f"/api/projects/{other_id}/members").status_code == 404
        assert (
            analyst.post(
                members_path,
                json={"username": "api-reviewer", "role": "admin"},
                headers=analyst_csrf,
            ).status_code
            == 404
        )
        assert (
            analyst.post(
                f"/api/projects/{other_id}/files",
                files={"file": ("synthetic.txt", b"synthetic")},
                headers=analyst_csrf,
            ).status_code
            == 404
        )
        uploaded = analyst.post(
            f"/api/projects/{project_id}/files",
            files={"file": ("synthetic.txt", b"synthetic project evidence")},
            headers=analyst_csrf,
        )
        assert uploaded.status_code == 202
        source = uploaded.json()["source"]
        assert "path" not in source
        assert app.state.intake_worker.process_one().status == "completed"
        failed_source = client.post(
            f"/api/projects/{project_id}/sources",
            json={"kind": "document", "reference": "missing.txt", "content_sha256": "f" * 64},
            headers=headers,
        ).json()
        failed_job = app.state.intake_worker.repository.enqueue_intake(
            project_id, failed_source["id"]
        )
        assert app.state.intake_worker.process_one().status == "failed"
        assert (
            analyst.post(
                f"/api/projects/{other_id}/intake/{failed_job.id}/retry",
                headers=analyst_csrf,
            ).status_code
            == 404
        )
        assert (
            analyst.post(
                f"/api/projects/{project_id}/intake/{failed_job.id}/retry",
                headers=analyst_csrf,
            ).json()["status"]
            == "queued"
        )
        fragment = analyst.get(
            f"/api/projects/{project_id}/sources/{source['id']}/fragments"
        ).json()[0]
        candidate = analyst.post(
            f"/api/projects/{project_id}/candidate-proposals",
            json={
                "source_id": source["id"],
                "expected_source_sha256": source["content_sha256"],
                "source_locator": fragment["locator"],
                "subject": "Synthetic topic",
                "statement": "Synthetic fact, not yet confirmed",
            },
            headers=analyst_csrf,
        )
        assert candidate.status_code == 201 and candidate.json()["status"] == "candidate"
        candidate_id = candidate.json()["id"]
        assert (
            analyst.post(
                f"/api/projects/{project_id}/observations/{candidate_id}/confirm",
                json={"reviewer": "forged", "expected_version": 1},
                headers=analyst_csrf,
            ).status_code
            == 404
        )

        reviewer = TestClient(app, base_url="https://testserver")
        reviewer_login = reviewer.post(
            "/api/auth/login",
            json={"username": "api-reviewer", "password": "reviewer-synthetic-password"},
        )
        reviewer_csrf = {"X-CSRF-Token": reviewer_login.json()["csrf_token"]}
        assert reviewer.get(f"/api/projects/{project_id}/capabilities").json() == {
            "read": True,
            "propose": True,
            "review": True,
            "manage": False,
        }
        confirmed = reviewer.post(
            f"/api/projects/{project_id}/observations/{candidate_id}/confirm",
            json={"reviewer": "forged", "expected_version": 1},
            headers=reviewer_csrf,
        )
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"
        reviews = reviewer.get(
            f"/api/projects/{project_id}/observations/{candidate_id}/reviews"
        ).json()
        assert reviews[0]["reviewer"] == reviewer_id
        audit = reviewer.get(f"/api/projects/{project_id}/audit").json()
        assert any(
            row["action"] == "candidate.confirmed" and row["actor_ref"] == reviewer_id
            for row in audit
        )
        assert (
            reviewer.post(
                f"/api/projects/{project_id}/observations/{candidate_id}/confirm",
                json={"expected_version": 1},
                headers=reviewer_csrf,
            ).status_code
            == 409
        )
        assert (
            reviewer.post(
                f"/api/projects/{project_id}/observations/{candidate_id}/confirm",
                json={"expected_version": 1},
                headers={"X-CSRF-Token": "wrong"},
            ).status_code
            == 403
        )
        second_candidate = analyst.post(
            f"/api/projects/{project_id}/candidate-proposals",
            json={
                "source_id": source["id"],
                "expected_source_sha256": source["content_sha256"],
                "source_locator": fragment["locator"],
                "subject": "Synthetic topic",
                "statement": "Unsupported claim for rejection",
            },
            headers=analyst_csrf,
        ).json()
        reject_path = f"/api/projects/{project_id}/observations/{second_candidate['id']}/reject"
        assert (
            analyst.post(
                reject_path, json={"expected_version": 1}, headers=analyst_csrf
            ).status_code
            == 404
        )
        assert reviewer.post(reject_path, json={"expected_version": 1}).status_code == 403
        rejected = reviewer.post(
            reject_path,
            json={"reviewer": "forged", "expected_version": 1},
            headers=reviewer_csrf,
        )
        assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
        assert (
            reviewer.get(
                f"/api/projects/{project_id}/observations", params={"status_filter": "rejected"}
            ).json()[0]["id"]
            == second_candidate["id"]
        )
        assert (
            reviewer.get(
                f"/api/projects/{project_id}/observations/{second_candidate['id']}/reviews"
            ).json()[0]["action"]
            == "reject"
        )
        assert any(
            row["action"] == "candidate.rejected" and row["actor_ref"] == reviewer_id
            for row in reviewer.get(f"/api/projects/{project_id}/audit").json()
        )
        assert (
            reviewer.post(
                f"/api/projects/{other_id}/observations/{second_candidate['id']}/reject",
                json={"expected_version": 1},
                headers=reviewer_csrf,
            ).status_code
            == 404
        )
        assert (
            reviewer.post(
                reject_path, json={"expected_version": 2}, headers=reviewer_csrf
            ).status_code
            == 409
        )
        assert (
            analyst.get(f"/api/projects/{project_id}/observations").json()[0]["status"]
            == "confirmed"
        )
        assert analyst.get(f"/api/projects/{other_id}/audit").status_code == 404
        assert reviewer.get(f"/api/projects/{other_id}/observations").status_code == 404
        revoked = client.delete(f"/api/projects/{project_id}/members/{analyst_id}", headers=headers)
        assert revoked.status_code == 204
        assert revoked.content == b""
        assert analyst.get("/api/projects").json() == []
        assert analyst.get(f"/api/projects/{project_id}/capabilities").status_code == 404
        assert analyst.get(f"/api/projects/{project_id}/sources").status_code == 404
        assert (
            analyst.post(
                f"/api/projects/{project_id}/candidate-proposals",
                json={
                    "source_id": source["id"],
                    "expected_source_sha256": source["content_sha256"],
                    "source_locator": fragment["locator"],
                    "subject": "Revoked attempt",
                    "statement": "Must not be stored",
                },
                headers=analyst_csrf,
            ).status_code
            == 404
        )
        assert client.post("/api/auth/logout", headers=headers).status_code == 204
        assert client.get("/api/projects").status_code == 401
        assert admin.id != reviewer_id


def test_postgres_browser_factory_requires_explicit_setup_and_bootstrap(
    empty_test_database, tmp_path: Path, monkeypatch
):
    dsn = empty_test_database
    monkeypatch.delenv("PROJECT_WATCH_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="database, object root and HTTPS origin"):
        create_postgres_team_app_from_env()
    monkeypatch.setenv("PROJECT_WATCH_DATABASE_URL", dsn)
    monkeypatch.setenv("PROJECT_WATCH_OBJECT_ROOT", str(tmp_path / "objects"))
    monkeypatch.setenv("PROJECT_WATCH_TEAM_ORIGIN", "https://testserver")
    with pytest.raises(RuntimeError, match="pinned parser image"):
        create_postgres_team_app_from_env()
    monkeypatch.setenv("PROJECT_WATCH_PARSER_IMAGE", "sha256:" + "a" * 64)
    monkeypatch.setenv(
        "PROJECT_WATCH_PARSER_DOCKER_HOST",
        "npipe:////./pipe/dockerDesktopLinuxEngine"
        if os.name == "nt"
        else f"unix:///run/user/{os.getuid()}/docker.sock",
    )
    monkeypatch.setattr(
        "project_watch.postgres_team_api.validate_parser_runtime", lambda _image, _host: None
    )
    with pytest.raises(postgres_migrations.MigrationError, match="checksum/version mismatch"):
        create_postgres_team_app_from_env()
    postgres_migrations.bootstrap_postgres(dsn)
    with pytest.raises(RuntimeError, match="bootstrap a team administrator"):
        create_postgres_team_app_from_env()
    monkeypatch.setattr(
        sys, "argv", ["postgres_team_auth", "bootstrap-admin", "--username", "test-admin"]
    )
    passwords = iter(["long-synthetic-password", "long-synthetic-password"])
    monkeypatch.setattr(postgres_team_auth.getpass, "getpass", lambda _prompt: next(passwords))
    postgres_team_auth.main()
    app = create_postgres_team_app_from_env()
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/projects").status_code == 401
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "test-admin", "password": "long-synthetic-password"},
            ).status_code
            == 200
        )
        assert client.get("/api/projects").json() == []


def test_postgres_browser_project_list_pages_without_duplicates(
    empty_test_database, tmp_path: Path
):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth, PostgresCoreRepository(dsn) as repo:
        admin = auth.bootstrap_admin("page-admin", "long-synthetic-password")
        created = [
            repo.create_project(f"Synthetic Project {index:03}", verified_actor_id=admin.id)
            for index in range(52)
        ]
    app = create_postgres_team_app(
        dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
    )
    with TestClient(app, base_url="https://testserver") as client:
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "page-admin", "password": "long-synthetic-password"},
            ).status_code
            == 200
        )
        first = client.get("/api/projects", params={"limit": 50}).json()
        assert len(first) == 50
        second = client.get(
            "/api/projects",
            params={
                "limit": 50,
                "after_created_at": first[-1]["created_at"],
                "after_id": first[-1]["id"],
            },
        ).json()
        assert len(second) == 2
        assert [row["id"] for row in first + second] == [row.id for row in created]
        assert client.get("/api/projects", params={"after_id": first[-1]["id"]}).status_code == 422


def test_postgres_mcp_bearer_scope_and_revocation(empty_test_database):
    httpx2 = pytest.importorskip("httpx2")
    pytest.importorskip("mcp.server.mcpserver")
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.transport_security import TransportSecuritySettings

    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth, PostgresCoreRepository(dsn) as repo:
        admin = auth.bootstrap_admin("mcp-admin", "long-synthetic-password")
        project = repo.create_project("MCP PG Synthetic", verified_actor_id=admin.id)
        other = repo.create_project("Other MCP PG", verified_actor_id=admin.id)
        source = repo.add_source(
            project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64
        )
        foreign = repo.add_source(
            other.id, kind="document", reference="foreign.txt", content_sha256="b" * 64
        )
        repo.enqueue_intake(project.id, source.id)
        claimed = repo.claim_next_intake()
        repo.complete_intake(
            project.id,
            claimed.id,
            [
                FragmentInput(ordinal=1, locator="line 1", text="Synthetic evidence"),
                FragmentInput(ordinal=2, locator="line 2", text="C" * 1800),
                FragmentInput(ordinal=3, locator="line 3", text="2025 revenue 1,200.5 万元"),
            ],
            expected_attempt=claimed.attempts,
        )
        read_token, _ = auth.issue_executor_token(
            project.id, admin.id, label="reader", permission="read"
        )
        propose_token, grant = auth.issue_executor_token(
            project.id, admin.id, label="proposer", permission="propose"
        )
        foreign_token, _ = auth.issue_executor_token(
            other.id, admin.id, label="foreign", permission="read"
        )
        url = "https://localhost:8768/mcp"
        server = build_postgres_mcp_server(
            repo, auth, project.id, resource_url=url, issuer_url="https://localhost:8767/"
        )
        app = server.streamable_http_app(
            transport_security=TransportSecuritySettings(
                allowed_hosts=["localhost:8768"], allowed_origins=["https://localhost:8768"]
            )
        )

        async def exercise() -> None:
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="https://localhost:8768"
            ) as anonymous:
                assert (await anonymous.post("/mcp", json={})).status_code == 401
                assert (
                    await anonymous.post(
                        "/mcp",
                        json={},
                        headers={"Authorization": f"Bearer {foreign_token}"},
                    )
                ).status_code == 401
            async with server.session_manager.run():
                async with (
                    httpx2.AsyncClient(
                        transport=transport,
                        base_url=url,
                        headers={"Authorization": f"Bearer {read_token}"},
                    ) as http_client,
                    Client(streamable_http_client(url, http_client=http_client)) as client,
                ):
                    tools = await client.list_tools()
                    assert {tool.name for tool in tools.tools} == {
                        "project_overview",
                        "source_excerpts",
                        "monitoring_status",
                        "propose_candidate",
                    }
                    overview = await client.call_tool("project_overview", {})
                    assert overview.structured_content["project_id"] == project.id
                    assert (
                        await client.call_tool("source_excerpts", {"source_id": foreign.id})
                    ).is_error
                    first = await client.call_tool(
                        "source_excerpts", {"source_id": source.id, "limit": 2}
                    )
                    assert first.structured_content["next_cursor"] is not None
                    second = await client.call_tool(
                        "source_excerpts",
                        {
                            "source_id": source.id,
                            "limit": 2,
                            "cursor": first.structured_content["next_cursor"],
                        },
                    )
                    assert second.structured_content["excerpts"][0]["text"] == "C" * 300
                    assert second.structured_content["next_cursor"] is None
                    assert (
                        await client.call_tool(
                            "propose_candidate",
                            {
                                "source_id": source.id,
                                "expected_source_sha256": source.content_sha256,
                                "source_locator": "line 1",
                                "subject": "Synthetic",
                                "statement": "Read-only token cannot propose",
                            },
                        )
                    ).is_error
                async with (
                    httpx2.AsyncClient(
                        transport=transport,
                        base_url=url,
                        headers={"Authorization": f"Bearer {propose_token}"},
                    ) as http_client,
                    Client(streamable_http_client(url, http_client=http_client)) as client,
                ):
                    proposal = await client.call_tool(
                        "propose_candidate",
                        {
                            "source_id": source.id,
                            "expected_source_sha256": source.content_sha256,
                            "source_locator": "line 1",
                            "subject": "Synthetic",
                            "statement": "Candidate needing human review",
                        },
                    )
                    assert proposal.structured_content["status"] == "candidate"
                    numeric = await client.call_tool(
                        "propose_candidate",
                        {
                            "source_id": source.id,
                            "expected_source_sha256": source.content_sha256,
                            "source_locator": "line 3",
                            "subject": "财务表现",
                            "statement": "营业收入",
                            "number": "1200.5",
                            "unit": "万元",
                            "period": "2025",
                            "scope": "合并",
                        },
                    )
                    assert numeric.structured_content["number"] == "1200.5"
                    assert numeric.structured_content["status"] == "candidate"
                    assert (
                        "metrics"
                        not in (await client.call_tool("monitoring_status", {})).structured_content
                    )
                    assert (
                        await client.call_tool("monitoring_status", {"include_metrics": True})
                    ).structured_content["metrics"]["series"] == []
                    repo.confirm_observation(
                        project.id,
                        numeric.structured_content["id"],
                        verified_reviewer_id=admin.id,
                        expected_version=1,
                    )
                    baseline = await client.call_tool(
                        "monitoring_status", {"include_metrics": True}
                    )
                    assert (
                        baseline.structured_content["metrics"]["series"][0]["points"][0]["number"]
                        == "1200.5"
                    )
                auth.revoke_executor_token(project.id, grant.id, actor_user_id=admin.id)
                async with httpx2.AsyncClient(
                    transport=transport,
                    base_url="https://localhost:8768",
                    headers={"Authorization": f"Bearer {propose_token}"},
                ) as revoked:
                    assert (await revoked.post("/mcp", json={})).status_code == 401

        asyncio.run(exercise())
        confirmed_rows = repo.list_observations(project.id, status="confirmed")
        assert len(confirmed_rows) == 1
        assert len(repo.list_observations(project.id, status="candidate")) == 1
        event = next(
            item
            for item in repo.list_audit_events(project.id)
            if item.action == "candidate.created" and item.subject_id == confirmed_rows[0].id
        )
        assert event.actor_kind == "verified_executor"
        assert event.actor_ref == grant.id
        assert event.channel == "team_mcp"


def test_postgres_monitoring_claims_are_atomic_and_stale_workers_cannot_finish(
    empty_test_database,
):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Queue Synthetic", verified_actor_id="user-a")
        other = repo.create_project("Other Synthetic", verified_actor_id="user-a")
        plan = repo.create_monitoring_plan(
            project.id, kind="rss", target="https://example.org/feed.xml", cadence_minutes=60
        )
        assert repo.get_monitoring_plan(other.id, plan.id) is None

        def request(_index):
            return repo.request_monitoring_run(project.id, plan.id, idempotency_key="same-key")

        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(request, range(16)))
        assert len({job.id for job in jobs}) == 1
        repo.request_monitoring_run(project.id, plan.id, idempotency_key="second-key")

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: repo.claim_next_monitoring_job(), range(2)))
        assert len({job.id for job in claimed if job is not None}) == 2
        assert all(job.status == "processing" and job.attempts == 1 for job in claimed)
        assert repo.claim_next_monitoring_job() is None

        leases = [job.lease_until for job in claimed if job is not None]
        assert len(leases) == 2 and all(lease is not None for lease in leases)
        reclaimed = repo.claim_next_monitoring_job(now=max(leases) + timedelta(seconds=1))
        assert reclaimed.id in {job.id for job in claimed} and reclaimed.attempts == 2
        with pytest.raises(IntakeConflictError, match="stale"):
            repo.fail_monitoring_job(
                project.id, reclaimed.id, expected_attempt=1, error_code="synthetic_failure"
            )
        failed = repo.fail_monitoring_job(
            project.id, reclaimed.id, expected_attempt=2, error_code="synthetic_failure"
        )
        assert failed.status == "failed"
        with pytest.raises(NotFoundError):
            repo.request_monitoring_run(other.id, plan.id, idempotency_key="cross-project")


@pytest.mark.skipif(
    not os.environ.get("PROJECT_WATCH_PARSER_TEST_IMAGE"),
    reason="set PROJECT_WATCH_PARSER_TEST_IMAGE to a locally built synthetic parser image ID",
)
def test_postgres_team_worker_uses_container_for_synthetic_intake(
    empty_test_database, tmp_path: Path
):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    objects = BlobStore(tmp_path / "objects")
    blob = objects.put_stream("synthetic.txt", io.BytesIO(b"Only synthetic container text"))
    with PostgresTeamAuthStore(dsn) as auth, PostgresCoreRepository(dsn) as repo:
        admin = auth.bootstrap_admin("parser-admin", "synthetic-admin-password")
        project = repo.create_project("Container Synthetic", verified_actor_id=admin.id)
        source = repo.add_source(
            project.id,
            kind="document",
            reference="synthetic.txt",
            content_sha256=blob.sha256,
        )
        repo.enqueue_intake(project.id, source.id)
        app = create_postgres_team_app(
            dsn,
            object_root=objects.root,
            team_origin="https://testserver",
            parser_image=os.environ["PROJECT_WATCH_PARSER_TEST_IMAGE"],
            parser_docker_host=os.environ["PROJECT_WATCH_PARSER_DOCKER_HOST"],
        )
        with TestClient(app, base_url="https://testserver"):
            assert app.state.intake_worker.process_one().status == "completed"
            assert [fragment.text for fragment in repo.list_fragments(project.id, source.id)] == [
                "Only synthetic container text"
            ]


def test_postgres_team_identity_roles_lockout_and_executor_revocation(empty_test_database):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth, PostgresCoreRepository(dsn) as repo:

        def bootstrap(_index):
            try:
                return auth.bootstrap_admin("admin", "long-synthetic-password")
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            admins = list(pool.map(bootstrap, range(2)))
        assert sum(admin is not None for admin in admins) == 1
        admin = next(admin for admin in admins if admin is not None)
        project = repo.create_project("Team Synthetic", verified_actor_id=admin.id)
        other = repo.create_project("Private Synthetic", verified_actor_id=admin.id)
        analyst = auth.create_user("analyst", "another-synthetic-password", actor_user_id=admin.id)
        with pytest.raises(AuthorizationError):
            auth.create_user("intruder", "intruder-synthetic-password", actor_user_id=analyst.id)

        auth.grant_project_role(project.id, analyst.id, "analyst", actor_user_id=admin.id)
        auth.require(analyst.id, project.id, "read")
        auth.require(analyst.id, project.id, "propose")
        with pytest.raises(AuthorizationError):
            auth.require(analyst.id, project.id, "review")
        with pytest.raises(AuthorizationError):
            auth.require(analyst.id, other.id, "read")

        second_admin = auth.create_user(
            "project-admin", "project-admin-password", actor_user_id=admin.id
        )
        auth.grant_project_role(project.id, second_admin.id, "admin", actor_user_id=admin.id)

        def same_role(actor_id):
            auth.grant_project_role(project.id, analyst.id, "reviewer", actor_user_id=actor_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(same_role, (admin.id, second_admin.id)))
        assert (
            sum(
                event.action == "grant_project_role"
                and event.target_user_id == analyst.id
                and event.new_role == "reviewer"
                for event in auth.list_security_events()
            )
            == 1
        )
        auth.grant_project_role(project.id, analyst.id, "analyst", actor_user_id=admin.id)

        token, csrf = auth.login("analyst", "another-synthetic-password")
        session = auth.session(token)
        assert session.user_id == analyst.id
        assert auth.session(token, now=session.expires_at) is None
        assert auth.validate_csrf(token, csrf)
        assert not auth.validate_csrf(token, "wrong")
        auth.logout(token)
        assert auth.session(token) is None

        for _ in range(5):
            with pytest.raises(AuthenticationError):
                auth.login("analyst", "wrong-password")
        with pytest.raises(AuthenticationError):
            auth.login("analyst", "another-synthetic-password")
        auth.unlock_user(analyst.id, actor_user_id=admin.id)
        token, _ = auth.login("analyst", "another-synthetic-password")

        secret, grant = auth.issue_executor_token(
            project.id, admin.id, label="Synthetic connector", permission="propose"
        )
        assert auth.executor_grant(secret, permission="propose").id == grant.id
        assert auth.executor_grant("wrong", permission="read") is None
        assert auth.revoke_executor_token(project.id, grant.id, actor_user_id=admin.id)
        assert auth.executor_grant(secret) is None
        assert not auth.revoke_executor_token(project.id, grant.id, actor_user_id=admin.id)

        auth.grant_project_role(project.id, analyst.id, "admin", actor_user_id=admin.id)
        scoped_secret, scoped_grant = auth.issue_executor_token(
            project.id, analyst.id, label="Read-only connector", permission="read"
        )
        assert auth.executor_grant(scoped_secret, permission="read").id == scoped_grant.id
        assert auth.executor_grant(scoped_secret, permission="propose") is None

        auth.revoke_project_role(project.id, analyst.id, actor_user_id=admin.id)
        assert auth.executor_grant(scoped_secret) is None
        with pytest.raises(AuthorizationError):
            auth.require(analyst.id, project.id, "read")
        auth.deactivate_user(analyst.id, actor_user_id=admin.id)
        with pytest.raises(ValueError, match="bootstrap administrator"):
            auth.deactivate_user(admin.id, actor_user_id=admin.id)
        assert auth.session(token) is None
        assert auth.get_user(analyst.id).active is False
        events = auth.list_security_events()
        assert {
            "bootstrap_admin",
            "create_user",
            "grant_project_role",
            "issue_executor_token",
            "revoke_executor_token",
            "deactivate_user",
        } <= {event.action for event in events}

    with PostgresTeamAuthStore(dsn) as reopened:
        assert reopened.is_bootstrapped()
        assert reopened.get_user(analyst.id).active is False
        assert reopened.executor_grant(secret) is None


def test_postgres_password_reset_and_offline_recovery(empty_test_database, tmp_path: Path):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth, PostgresCoreRepository(dsn) as repo:
        admin = auth.bootstrap_admin("admin", "synthetic-admin-password")
        member = auth.create_user("member", "synthetic-member-password", actor_user_id=admin.id)
        project = repo.create_project("Synthetic Recovery", verified_actor_id=admin.id)
        auth.grant_project_role(project.id, member.id, "admin", actor_user_id=admin.id)
        member_session, _ = auth.login(member.username, "synthetic-member-password")
        secret, _ = auth.issue_executor_token(
            project.id, member.id, label="Synthetic reset", permission="read"
        )
        with pytest.raises(AuthorizationError):
            auth.reset_password(admin.id, "synthetic-new-admin", actor_user_id=member.id)
        with pytest.raises(ValueError):
            auth.reset_password("missing", "synthetic-new-member", actor_user_id=admin.id)
        auth.reset_password(member.id, "synthetic-new-member", actor_user_id=admin.id)
        assert auth.session(member_session) is None
        assert auth.executor_grant(secret) is None
        with pytest.raises(AuthenticationError):
            auth.login(member.username, "synthetic-member-password")
        assert auth.login(member.username, "synthetic-new-member")

        app = create_postgres_team_app(
            dsn, object_root=tmp_path / "objects", team_origin="https://testserver"
        )
        with TestClient(app, base_url="https://testserver") as client:
            login = client.post(
                "/api/auth/login",
                json={"username": admin.username, "password": "synthetic-admin-password"},
            )
            assert login.status_code == 200
            csrf = login.json()["csrf_token"]
            url = f"/api/team/users/{member.id}/reset-password"
            assert client.post(url, json={"password": "synthetic-third-member"}).status_code == 403
            result = client.post(
                url,
                json={"password": "synthetic-third-member"},
                headers={"X-CSRF-Token": csrf},
            )
            assert result.status_code == 204
            assert result.headers["Cache-Control"] == "no-store"

        admin_session, _ = auth.login(admin.username, "synthetic-admin-password")
        admin_secret, _ = auth.issue_executor_token(
            project.id, admin.id, label="Synthetic admin", permission="read"
        )
        with pytest.raises(ValueError):
            auth.recover_admin_offline(member.username, "synthetic-offline-password")
        auth.recover_admin_offline(admin.username, "synthetic-recovered-admin")
        assert auth.session(admin_session) is None
        assert auth.executor_grant(admin_secret) is None
        with pytest.raises(AuthenticationError):
            auth.login(admin.username, "synthetic-admin-password")
        assert auth.login(admin.username, "synthetic-recovered-admin")
        events = auth.list_security_events()
        recovery = next(event for event in events if event.action == "recover_admin_offline")
        assert recovery.actor_user_id is None and recovery.target_user_id == admin.id
        assert sum(event.action == "reset_password" for event in events) == 2


def test_postgres_session_security_audit(empty_test_database):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresTeamAuthStore(dsn) as auth:
        admin = auth.bootstrap_admin("admin", "synthetic-admin-password")
        initial_events = len(auth.list_security_events())
        with pytest.raises(AuthenticationError):
            auth.login("missing", "synthetic-unknown-password")
        assert len(auth.list_security_events()) == initial_events
        for _ in range(5):
            with pytest.raises(AuthenticationError):
                auth.login(admin.username, "synthetic-wrong-password")
        events = auth.list_security_events()
        assert sum(event.action == "login_failed" for event in events) == 5
        assert sum(event.action == "account_locked" for event in events) == 1
        with pytest.raises(AuthenticationError):
            auth.login(admin.username, "synthetic-admin-password")
        assert len(auth.list_security_events()) == len(events)
        auth.unlock_user(admin.id, actor_user_id=admin.id)
        token, _ = auth.login(admin.username, "synthetic-admin-password")
        assert auth.list_security_events()[0].action == "login_success"
        auth.logout(token)
        assert auth.list_security_events()[0].action == "logout"
        audit_count = len(auth.list_security_events())
        auth.logout(token)
        assert len(auth.list_security_events()) == audit_count
        assert all(
            secret not in str(event)
            for event in auth.list_security_events()
            for secret in (token, "synthetic-admin-password", "synthetic-wrong-password")
        )


def test_postgres_intake_queue_recovery_and_project_isolation(empty_test_database):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Intake Synthetic", verified_actor_id="admin")
        other = repo.create_project("Other Intake", verified_actor_id="admin")
        sources = [
            repo.add_source(
                project.id,
                kind="document",
                reference=f"synthetic-{i}.txt",
                content_sha256=f"{i + 1:064x}",
            )
            for i in range(2)
        ]

        def enqueue(_index):
            return repo.enqueue_intake(project.id, sources[0].id)

        with ThreadPoolExecutor(max_workers=8) as pool:
            duplicate_jobs = list(pool.map(enqueue, range(16)))
        assert len({job.id for job in duplicate_jobs}) == 1
        repo.enqueue_intake(project.id, sources[1].id)
        assert repo.get_intake_job(other.id, duplicate_jobs[0].id) is None
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: repo.claim_next_intake(), range(2)))
        assert len({job.id for job in claimed if job is not None}) == 2
        assert repo.claim_next_intake() is None
        first, second = claimed
        assert first is not None and second is not None
        completed = repo.complete_intake(
            project.id,
            first.id,
            [FragmentInput(ordinal=1, locator="line 1", text="Synthetic evidence")],
            expected_attempt=1,
        )
        assert completed.status == "completed"
        assert len(repo.list_fragments(project.id, first.source_id)) == 1
        with pytest.raises(NotFoundError):
            repo.list_fragments(other.id, first.source_id)

        with pytest.raises(ValueError, match="workers must be stopped"):
            repo.recover_interrupted_intake_jobs()
        assert repo.recover_interrupted_intake_jobs(workers_stopped=True) == 1
        with pytest.raises(IntakeConflictError):
            repo.complete_intake(
                project.id,
                second.id,
                [FragmentInput(ordinal=1, locator="line 1", text="Stale worker")],
                expected_attempt=1,
            )
        reclaimed = repo.claim_next_intake()
        assert reclaimed.id == second.id and reclaimed.attempts == 2
        assert (
            repo.fail_intake(
                project.id, second.id, expected_attempt=2, error_code="synthetic_failure"
            ).status
            == "failed"
        )
        assert repo.retry_failed_intake(project.id, second.id).status == "queued"
        third = repo.claim_next_intake()
        assert third.id == second.id and third.attempts == 3
        repo.complete_intake(
            project.id,
            second.id,
            [FragmentInput(ordinal=1, locator="line 1", text="Fresh evidence")],
            expected_attempt=3,
        )
        assert repo.list_fragments(project.id, second.source_id)[0].text == "Fresh evidence"

    with PostgresCoreRepository(dsn) as reopened:
        assert len(reopened.list_intake(project.id)) == 2
        assert reopened.get_intake_job(project.id, second.id).status == "completed"


def test_postgres_monitor_receipts_are_atomic_and_unreachable_is_not_no_change(
    empty_test_database,
):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Monitor Synthetic", verified_actor_id="admin")
        other = repo.create_project("Private Monitor", verified_actor_id="admin")
        plan = repo.create_monitoring_plan(
            project.id, kind="rss", target="https://example.org/feed.xml", cadence_minutes=60
        )
        assert repo.set_monitoring_plan_enabled(project.id, plan.id, True).enabled

        unreachable = repo.request_monitoring_run(
            project.id, plan.id, idempotency_key="fetch-unreachable"
        )
        claimed = repo.claim_next_monitoring_job()
        assert claimed.id == unreachable.id
        finished, receipt = repo.finish_monitoring_job(
            project.id,
            claimed.id,
            expected_attempt=claimed.attempts,
            status="unreachable",
            error_code="fetch_failed",
        )
        assert finished.status == "done"
        assert receipt.status == "unreachable" and receipt.error_code == "fetch_failed"
        assert repo.get_monitoring_plan(project.id, plan.id).last_run_error_code == "fetch_failed"
        assert receipt.new_count == 0
        assert repo.list_signals(project.id) == []
        with pytest.raises(IntakeConflictError):
            repo.finish_monitoring_job(
                project.id,
                claimed.id,
                expected_attempt=claimed.attempts,
                status="no_results",
            )

        signal = SignalInput(
            external_id="synthetic-1",
            title="Synthetic announcement",
            url="https://example.org/item/1",
            summary="Synthetic only",
            published_at=None,
            freshness="current",
        )
        for key, expected_new in (("first-signal", 1), ("same-signal-again", 0)):
            repo.request_monitoring_run(project.id, plan.id, idempotency_key=key)
            job = repo.claim_next_monitoring_job()
            _, result = repo.finish_monitoring_job(
                project.id,
                job.id,
                expected_attempt=job.attempts,
                status="completed",
                signals=(signal,),
            )
            assert result.status == "completed" and result.new_count == expected_new
        assert len(repo.list_signals(project.id)) == 1
        assert repo.list_signals(other.id) == []
        assert len(repo.list_source_runs(project.id, plan.id)) == 3
        assert repo.list_latest_source_runs(project.id) == [result]
        assert repo.list_latest_source_runs(other.id) == []
        with pytest.raises(NotFoundError):
            repo.list_source_runs(other.id, plan.id)

        repo.request_monitoring_run(project.id, plan.id, idempotency_key="stale-lease")
        old = repo.claim_next_monitoring_job()
        assert old.lease_until is not None
        new = repo.claim_next_monitoring_job(now=old.lease_until + timedelta(seconds=1))
        assert new.id == old.id and new.attempts == old.attempts + 1
        with pytest.raises(IntakeConflictError):
            repo.finish_monitoring_job(
                project.id,
                old.id,
                expected_attempt=old.attempts,
                status="no_results",
            )
        assert len(repo.list_source_runs(project.id, plan.id)) == 3
        _, final_receipt = repo.finish_monitoring_job(
            project.id, new.id, expected_attempt=new.attempts, status="no_results"
        )
        assert final_receipt.status == "no_results"
        assert final_receipt.finished_at >= final_receipt.started_at
        assert len(repo.list_source_runs(project.id, plan.id)) == 4


def test_postgres_due_scheduler_is_idempotent_and_respects_cadence(empty_test_database):
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    with PostgresCoreRepository(dsn) as repo:
        project = repo.create_project("Scheduled Synthetic", verified_actor_id="admin")
        plan = repo.create_monitoring_plan(
            project.id, kind="rss", target="https://example.org/monitor.xml", cadence_minutes=60
        )
        now = datetime.now(UTC) + timedelta(seconds=2)
        assert repo.schedule_due_monitoring(now=now) == []
        repo.set_monitoring_plan_enabled(project.id, plan.id, True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            scheduled = list(pool.map(lambda _: repo.schedule_due_monitoring(now=now), range(2)))
        jobs = [job for batch in scheduled for job in batch]
        assert len(jobs) == 1
        assert jobs[0].trigger == "scheduled"
        assert repo.schedule_due_monitoring(now=now + timedelta(hours=2)) == []
        assert len(repo.list_monitoring_jobs(project.id)) == 1

        claimed = repo.claim_next_monitoring_job()
        assert claimed.id == jobs[0].id
        _, receipt = repo.finish_monitoring_job(
            project.id, claimed.id, expected_attempt=claimed.attempts, status="no_results"
        )
        assert repo.schedule_due_monitoring(now=receipt.finished_at + timedelta(minutes=59)) == []
        later = repo.schedule_due_monitoring(now=receipt.finished_at + timedelta(minutes=61))
        assert len(later) == 1 and later[0].id != jobs[0].id
        assert repo.schedule_due_monitoring(now=receipt.finished_at + timedelta(hours=3)) == []
        repo.set_monitoring_plan_enabled(project.id, plan.id, False)
        assert repo.schedule_due_monitoring(now=receipt.finished_at + timedelta(days=1)) == []


def test_postgres_full_backup_restore_with_objects_and_revoked_credentials(
    empty_test_database, tmp_path: Path, monkeypatch
):
    container = os.environ.get("PROJECT_WATCH_PG_DOCKER_CONTAINER")
    if not container:
        pytest.skip("set PROJECT_WATCH_PG_DOCKER_CONTAINER for a PostgreSQL client drill")
    assert container in {
        "project-watch-pg-auth-test-20260926",
        "project-watch-pg-team-api-test-20260926",
        "project-watch-pg-team-http-test-20260926",
        "project-watch-pg-browser-test-20260926",
        "project-watch-pg-rate-test-20260926",
    }
    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    original = b"synthetic source only"
    digest = hashlib.sha256(original).hexdigest()
    objects = tmp_path / "source-objects"
    path = objects / digest[:2] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    with PostgresTeamAuthStore(dsn) as auth, PostgresCoreRepository(dsn) as repo:
        admin = auth.bootstrap_admin("restore-admin", "long-synthetic-password")
        project = repo.create_project("Restore Synthetic", verified_actor_id=admin.id)
        source = repo.add_source(
            project.id, kind="document", reference="synthetic.txt", content_sha256=digest
        )
        candidate = repo.add_observation(
            project.id,
            source_id=source.id,
            subject="Synthetic",
            statement="Test only",
            source_locator="page 1",
            verified_actor_id=admin.id,
        )
        repo.confirm_observation(
            project.id, candidate.id, verified_reviewer_id=admin.id, expected_version=1
        )
        plan = repo.create_monitoring_plan(
            project.id, kind="rss", target="https://example.org/restore.xml", cadence_minutes=60
        )
        job = repo.request_monitoring_run(project.id, plan.id, idempotency_key="restore-run")
        claimed = repo.claim_next_monitoring_job()
        assert claimed.id == job.id
        repo.finish_monitoring_job(
            project.id, job.id, expected_attempt=claimed.attempts, status="no_results"
        )
        session_token, _csrf = auth.login("restore-admin", "long-synthetic-password")
        executor_token, _grant = auth.issue_executor_token(
            project.id, admin.id, label="synthetic restore", permission="read"
        )

    restore_name = f"project_watch_restore_{uuid.uuid4().hex[:10]}_test"
    target_dsn = dsn.rsplit("/", 1)[0] + "/" + restore_name
    archive_in_container = f"/tmp/project-watch-{uuid.uuid4().hex}.dump"

    def docker(*args: str, environment: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["docker", *args], env=environment, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def docker_client(command: list[str], environment: dict[str, str]) -> str:
        current_env = {**environment, "PGHOST": "127.0.0.1", "PGPORT": "5432"}
        if command[0] == "pg_dump":
            local_archive = Path(command[-1])
            docker(
                "exec",
                "-e",
                "PGHOST",
                "-e",
                "PGPORT",
                "-e",
                "PGUSER",
                "-e",
                "PGPASSWORD",
                "-e",
                "PGDATABASE",
                container,
                *command[:-1],
                archive_in_container,
                environment=current_env,
            )
            docker("cp", f"{container}:{archive_in_container}", str(local_archive))
            return ""
        if command[0] == "pg_restore" and "--list" in command:
            return docker("exec", container, "pg_restore", "--list", archive_in_container)
        return docker(
            "exec",
            "-e",
            "PGHOST",
            "-e",
            "PGPORT",
            "-e",
            "PGUSER",
            "-e",
            "PGPASSWORD",
            "-e",
            "PGDATABASE",
            container,
            *command[:-1],
            archive_in_container,
            environment=current_env,
        )

    monkeypatch.setattr(postgres_backup, "_run_client", docker_client)
    backup_dir = tmp_path / "pg-backup"
    try:
        path.unlink()
        with pytest.raises(BackupError, match="missing"):
            postgres_backup.create_postgres_backup(dsn, objects, backup_dir)
        assert not backup_dir.exists()
        path.write_bytes(original)
        manifest = postgres_backup.create_postgres_backup(dsn, objects, backup_dir)
        assert manifest["source_hashes"] == [digest]
        assert postgres_backup.verify_postgres_backup(backup_dir) == manifest
        backup_object = backup_dir / "objects" / digest[:2] / digest
        backup_object.write_bytes(b"tampered")
        with pytest.raises(BackupError, match="hash mismatch"):
            postgres_backup.verify_postgres_backup(backup_dir)
        backup_object.write_bytes(original)
        assert postgres_backup.verify_postgres_backup(backup_dir) == manifest
        docker(
            "exec",
            container,
            "psql",
            "-U",
            "project_watch_test",
            "-d",
            "postgres",
            "-c",
            f"CREATE DATABASE {restore_name} OWNER project_watch_test",
        )
        postgres_backup.restore_postgres_backup(
            backup_dir, target_dsn, tmp_path / "restored-objects"
        )
        with (
            PostgresTeamAuthStore(target_dsn) as restored_auth,
            PostgresCoreRepository(target_dsn) as restored_repo,
        ):
            assert restored_repo.list_sources(project.id)[0].content_sha256 == digest
            assert restored_repo.get_observation(project.id, candidate.id).status == "confirmed"
            assert len(restored_repo.list_source_runs(project.id, plan.id)) == 1
            assert restored_auth.session(session_token) is None
            assert restored_auth.executor_grant(executor_token) is None
            assert restored_auth.login("restore-admin", "long-synthetic-password")[0]
        assert (tmp_path / "restored-objects" / digest[:2] / digest).read_bytes() == original
    finally:
        docker("exec", container, "rm", "-f", archive_in_container)
        with psycopg.connect(dsn) as connection:
            existing = connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (restore_name,)
            ).fetchone()
        if existing:
            docker("exec", container, "dropdb", "-U", "project_watch_test", restore_name)


def test_postgres_native_client_backup_restore_drill(empty_test_database, tmp_path: Path) -> None:
    """Opt-in drill with real host pg_dump/pg_restore, not the Docker client shim."""
    if os.environ.get("PROJECT_WATCH_NATIVE_PG_DRILL") != "1":
        pytest.skip("set PROJECT_WATCH_NATIVE_PG_DRILL=1 for the host-client drill")
    for executable in ("pg_dump", "pg_restore"):
        if shutil.which(executable) is None:
            pytest.fail(f"{executable} is required for the native PostgreSQL drill")

    dsn = empty_test_database
    postgres_migrations.bootstrap_postgres(dsn)
    original = b"synthetic native backup drill only"
    digest = hashlib.sha256(original).hexdigest()
    objects = tmp_path / "source-objects"
    original_path = objects / digest[:2] / digest
    original_path.parent.mkdir(parents=True)
    original_path.write_bytes(original)

    with PostgresTeamAuthStore(dsn) as auth, PostgresCoreRepository(dsn) as repo:
        admin = auth.bootstrap_admin("native-drill-admin", "long-synthetic-password")
        project = repo.create_project("Native Backup Synthetic", verified_actor_id=admin.id)
        source = repo.add_source(
            project.id, kind="document", reference="synthetic.txt", content_sha256=digest
        )
        candidate = repo.add_observation(
            project.id,
            source_id=source.id,
            subject="Synthetic",
            statement="Test only",
            source_locator="page 1",
            verified_actor_id=admin.id,
        )
        repo.confirm_observation(
            project.id, candidate.id, verified_reviewer_id=admin.id, expected_version=1
        )
        plan = repo.create_monitoring_plan(
            project.id,
            kind="rss",
            target="https://example.org/native-drill.xml",
            cadence_minutes=60,
        )
        job = repo.request_monitoring_run(project.id, plan.id, idempotency_key="native-drill")
        claimed = repo.claim_next_monitoring_job()
        assert claimed.id == job.id
        repo.finish_monitoring_job(
            project.id, job.id, expected_attempt=claimed.attempts, status="no_results"
        )
        session_token, _csrf = auth.login("native-drill-admin", "long-synthetic-password")
        executor_token, _grant = auth.issue_executor_token(
            project.id, admin.id, label="native drill", permission="read"
        )

    backup_dir = tmp_path / "backup"
    target_name = f"project_watch_native_restore_{uuid.uuid4().hex[:10]}_test"
    target_dsn = psycopg.conninfo.make_conninfo(dsn, dbname=target_name)
    admin_dsn = psycopg.conninfo.make_conninfo(dsn, dbname="postgres")
    created_target = False
    try:
        started = time.perf_counter()
        manifest = postgres_backup.create_postgres_backup(dsn, objects, backup_dir)
        backup_seconds = time.perf_counter() - started
        assert manifest["source_hashes"] == [digest]
        assert postgres_backup.verify_postgres_backup(backup_dir) == manifest

        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                psycopg.sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    psycopg.sql.Identifier(target_name),
                    psycopg.sql.Identifier("project_watch_test"),
                )
            )
        created_target = True
        started = time.perf_counter()
        restored_objects = tmp_path / "restored-objects"
        postgres_backup.restore_postgres_backup(backup_dir, target_dsn, restored_objects)
        restore_seconds = time.perf_counter() - started

        with (
            PostgresTeamAuthStore(target_dsn) as restored_auth,
            PostgresCoreRepository(target_dsn) as restored_repo,
        ):
            assert restored_repo.list_sources(project.id)[0].content_sha256 == digest
            assert restored_repo.get_observation(project.id, candidate.id).status == "confirmed"
            assert len(restored_repo.list_source_runs(project.id, plan.id)) == 1
            assert restored_auth.session(session_token) is None
            assert restored_auth.executor_grant(executor_token) is None
            assert restored_auth.login("native-drill-admin", "long-synthetic-password")[0]
        assert (restored_objects / digest[:2] / digest).read_bytes() == original
        print(
            f"native PostgreSQL drill: backup={backup_seconds:.3f}s "
            f"restore={restore_seconds:.3f}s tables={len(manifest['table_counts'])} "
            f"files={len(manifest['files'])}"
        )
    finally:
        if created_target:
            with psycopg.connect(admin_dsn, autocommit=True) as connection:
                connection.execute(
                    psycopg.sql.SQL("DROP DATABASE {}").format(psycopg.sql.Identifier(target_name))
                )
