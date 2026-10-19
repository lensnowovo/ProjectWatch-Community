"""Native HTTP and MCP adapters preserve one evidence and review contract."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("mcp.server.mcpserver")

from mcp import Client

from project_watch.api import create_app
from project_watch.domain import FragmentInput
from project_watch.mcp_server import build_mcp_server
from project_watch.repository import ProjectRepository


def test_native_http_and_mcp_share_task_and_candidate_state(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(path)
    project = repo.create_project("Synthetic Conformance")
    source = repo.add_source(
        project.id, kind="document", reference="bp.txt", content_sha256="a" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    repo.complete_intake(
        claimed.id, [FragmentInput(ordinal=1, locator="line 1", text="Synthetic revenue")]
    )
    http = TestClient(create_app(path))
    native = http.post(
        f"/api/projects/{project.id}/tasks",
        json={
            "contract_version": 1,
            "kind": "coverage",
            "source_ids": [source.id],
            "idempotency_key": "same-task",
        },
    ).json()

    async def mcp_checks() -> None:
        async with Client(build_mcp_server(path, project.id)) as mcp:
            same = await mcp.call_tool(
                "run_coverage",
                {"source_ids": [source.id], "idempotency_key": "same-task"},
            )
            assert same.structured_content["id"] == native["id"]
            assert same.structured_content["outcome"] == native["outcome"]
            proposed = await mcp.call_tool(
                "propose_candidate",
                {
                    "source_id": source.id,
                    "expected_source_sha256": source.content_sha256,
                    "source_locator": "line 1",
                    "subject": "经营情况",
                    "statement": "资料称收入情况，需人工核对。",
                },
            )
            assert proposed.structured_content["status"] == "candidate"

    asyncio.run(mcp_checks())
    summary = http.get(f"/api/projects/{project.id}/summary").json()
    assert summary["candidate_count"] == 1
    assert summary["confirmed_count"] == 0


def test_mcp_can_propose_literal_metric_and_read_confirmed_baseline(tmp_path: Path) -> None:
    path = tmp_path / "numeric-mcp.sqlite3"
    repo = ProjectRepository(path)
    project = repo.create_project("Synthetic MCP Metric")
    source = repo.add_source(
        project.id, kind="document", reference="finance.txt", content_sha256="f" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    repo.complete_intake(
        claimed.id,
        [FragmentInput(ordinal=1, locator="line 2", text="2025 年营业收入 1,200.5 万元")],
    )

    async def exercise() -> None:
        async with Client(build_mcp_server(path, project.id)) as mcp:
            default = await mcp.call_tool("monitoring_status", {})
            assert "metrics" not in default.structured_content
            payload = {
                "source_id": source.id,
                "expected_source_sha256": source.content_sha256,
                "source_locator": "line 2",
                "subject": "财务表现",
                "statement": "营业收入",
                "number": "1200.5",
                "unit": "万元",
                "period": "2025",
                "scope": "合并报表",
            }
            invalid = await mcp.call_tool("propose_candidate", {**payload, "number": "999"})
            assert invalid.is_error
            result = await mcp.call_tool("propose_candidate", payload)
            assert result.structured_content["status"] == "candidate"
            assert result.structured_content["number"] == "1200.5"
            assert (
                await mcp.call_tool("monitoring_status", {"include_metrics": True})
            ).structured_content["metrics"]["series"] == []
            repo.confirm_observation(
                result.structured_content["id"], reviewer="synthetic-human", expected_version=1
            )
            metrics = await mcp.call_tool("monitoring_status", {"include_metrics": True})
            assert (
                metrics.structured_content["metrics"]["series"][0]["points"][0]["source_locator"]
                == "line 2"
            )

    asyncio.run(exercise())
