"""Official MCP client exercises a project-bound bridge without network access."""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")

from mcp import Client

from project_watch.domain import FragmentInput
from project_watch.mcp_server import build_mcp_server
from project_watch.repository import ProjectRepository


def test_mcp_tools_are_bounded_and_cannot_confirm_or_cross_project(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(path)
    project = repo.create_project("Synthetic MCP")
    other = repo.create_project("Other Synthetic MCP")
    source = repo.add_source(
        project.id, kind="document", reference="bp.txt", content_sha256="a" * 64
    )
    foreign = repo.add_source(
        other.id, kind="document", reference="foreign.txt", content_sha256="b" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    long_text = "Synthetic revenue " + "A" * 3100
    repo.complete_intake(
        claimed.id,
        [
            FragmentInput(ordinal=1, locator="line 1", text=long_text),
            FragmentInput(ordinal=2, locator="line 2", text="Later evidence"),
        ],
    )
    server = build_mcp_server(path, project.id)

    async def exercise() -> None:
        async with Client(server) as client:
            listing = await client.list_tools()
            names = {tool.name for tool in listing.tools}
            assert names == {
                "project_overview",
                "source_excerpts",
                "monitoring_status",
                "run_coverage",
                "propose_candidate",
            }
            overview = await client.call_tool("project_overview", {})
            assert overview.structured_content["project_id"] == project.id
            excerpts = await client.call_tool("source_excerpts", {"source_id": source.id})
            assert excerpts.structured_content["excerpts"][0]["locator"] == "line 1"
            first = await client.call_tool("source_excerpts", {"source_id": source.id, "limit": 1})
            page = first.structured_content
            collected = list(page["excerpts"])
            while page["next_cursor"] is not None:
                next_page = await client.call_tool(
                    "source_excerpts",
                    {"source_id": source.id, "limit": 1, "cursor": page["next_cursor"]},
                )
                page = next_page.structured_content
                collected.extend(page["excerpts"])
            assert "".join(item["text"] for item in collected if item["ordinal"] == 1) == long_text
            assert collected[-1]["text"] == "Later evidence"
            assert page["truncated"] is False
            assert (
                await client.call_tool(
                    "source_excerpts",
                    {"source_id": source.id, "cursor": "b" * 64 + ":1:0"},
                )
            ).is_error
            denied = await client.call_tool("source_excerpts", {"source_id": foreign.id})
            assert denied.is_error is True
            proposal = await client.call_tool(
                "propose_candidate",
                {
                    "source_id": source.id,
                    "expected_source_sha256": source.content_sha256,
                    "source_locator": "line 1",
                    "subject": "经营情况",
                    "statement": "资料提到收入，但口径待核对。",
                },
            )
            assert proposal.structured_content["status"] == "candidate"

    asyncio.run(exercise())
    assert repo.list_observations(project.id, status="confirmed") == []
