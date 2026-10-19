"""Synthetic bearer-gated MCP exercises the HTTP boundary, not in-memory bypass."""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")
pytest.importorskip("httpx2")

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings

from project_watch.domain import FragmentInput
from project_watch.mcp_server import build_mcp_server, create_team_mcp_app
from project_watch.repository import ProjectRepository
from project_watch.team_auth import TeamAuthStore


def test_team_mcp_bearer_scope_revoke_and_evidence(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(path)
    auth = TeamAuthStore(path)
    admin = auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    project = repo.create_project("Synthetic MCP Team")
    other = repo.create_project("Synthetic MCP Other")
    source = repo.add_source(
        project.id, kind="document", reference="synthetic-bp.txt", content_sha256="a" * 64
    )
    foreign = repo.add_source(
        other.id, kind="document", reference="foreign.txt", content_sha256="b" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    repo.complete_intake(
        claimed.id,
        [
            FragmentInput(ordinal=1, locator="line 1", text="Synthetic revenue"),
            FragmentInput(ordinal=2, locator="line 2", text="B" * 1800),
            FragmentInput(ordinal=3, locator="line 3", text="2025 revenue 1,200.5 万元"),
        ],
    )
    read_token, _ = auth.issue_executor_token(
        project.id, admin.id, label="read only", permission="read"
    )
    propose_token, grant = auth.issue_executor_token(
        project.id, admin.id, label="synthetic agent", permission="propose"
    )
    foreign_token, _ = auth.issue_executor_token(
        other.id, admin.id, label="other project", permission="read"
    )
    url = "https://localhost:8768/mcp"
    server = build_mcp_server(
        path,
        project.id,
        team_auth=auth,
        resource_url=url,
        issuer_url="https://localhost:8767/",
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
                    "/mcp", json={}, headers={"Authorization": f"Bearer {foreign_token}"}
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
                assert second.structured_content["excerpts"][0]["text"] == "B" * 300
                assert second.structured_content["next_cursor"] is None
                denied = await client.call_tool(
                    "propose_candidate",
                    {
                        "source_id": source.id,
                        "expected_source_sha256": source.content_sha256,
                        "source_locator": "line 1",
                        "subject": "经营情况",
                        "statement": "Synthetic proposal denied for read token",
                    },
                )
                assert denied.is_error
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
                        "subject": "经营情况",
                        "statement": "Synthetic candidate; human review needed.",
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
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="https://localhost:8768",
                headers={"Authorization": f"Bearer {propose_token}"},
            ) as revoked_client:
                auth.revoke_executor_token(project.id, grant.id, actor_user_id=admin.id)
                assert (await revoked_client.post("/mcp", json={})).status_code == 401

    asyncio.run(exercise())
    assert repo.list_observations(project.id, status="confirmed") == []
    assert len(repo.list_observations(project.id, status="candidate")) == 2
    event = repo.list_audit_events(project.id)[-1]
    assert event.actor_kind == "verified_executor"
    assert event.actor_ref == grant.id
    assert event.channel == "team_mcp"


def test_team_mcp_factory_rejects_http_and_unconfigured_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(path)
    project = repo.create_project("Synthetic Factory")
    TeamAuthStore(path).bootstrap_admin("admin@example.test", "synthetic-admin-password")
    monkeypatch.setenv("PROJECT_WATCH_DB_PATH", str(path))
    monkeypatch.setenv("PROJECT_WATCH_PROJECT_ID", project.id)
    monkeypatch.delenv("PROJECT_WATCH_MCP_RESOURCE_URL", raising=False)
    monkeypatch.setenv("PROJECT_WATCH_MCP_ISSUER_URL", "https://localhost:8767/")
    with pytest.raises(RuntimeError, match="resource URL"):
        create_team_mcp_app()
    monkeypatch.setenv("PROJECT_WATCH_MCP_RESOURCE_URL", "http://localhost:8768/mcp")
    with pytest.raises(ValueError, match="HTTPS"):
        create_team_mcp_app()
    monkeypatch.setenv("PROJECT_WATCH_MCP_RESOURCE_URL", "https://localhost:8768/mcp")
    app = create_team_mcp_app()

    async def probe() -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8768"
        ) as client:
            assert (await client.post("/mcp", json={})).status_code == 400
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="https://localhost:8768"
        ) as client:
            assert (await client.post("/mcp", json={})).status_code == 401

    asyncio.run(probe())
