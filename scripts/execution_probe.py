"""Offline synthetic comparison of native, MCP and DSH-shaped client paths.

No DSH runtime, model, credentials, network transport or real material is used.
Timing covers this local fixture only; it is not a product performance claim.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.domain import FragmentInput
from project_watch.mcp_server import build_mcp_server
from project_watch.repository import ProjectRepository


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 3)


def run_probe(db_path: Path) -> dict[str, object]:
    """Exercise three client shapes against one fresh, synthetic repository."""
    from mcp import Client

    if db_path.exists():
        raise ValueError("probe requires a new database path")
    repository = ProjectRepository(db_path)
    project = repository.create_project("Synthetic Project Watch Conformance")
    other = repository.create_project("Other Synthetic Project")
    source = repository.add_source(
        project.id,
        kind="document",
        reference="synthetic-probe.txt",
        content_sha256="a" * 64,
    )
    foreign = repository.add_source(
        other.id,
        kind="document",
        reference="other-synthetic.txt",
        content_sha256="b" * 64,
    )
    repository.enqueue_intake(project.id, source.id)
    job = repository.claim_next_intake()
    if job is None:
        raise RuntimeError("synthetic intake was not claimed")
    expected_text = {
        1: "Synthetic revenue needs review",
        2: ("Synthetic later-page context " * 70).strip(),
    }
    repository.complete_intake(
        job.id,
        [
            FragmentInput(ordinal=ordinal, locator=f"line {ordinal}", text=value)
            for ordinal, value in expected_text.items()
        ],
    )

    http = TestClient(create_app(db_path))
    started = perf_counter()
    native_response = http.post(
        f"/api/projects/{project.id}/tasks",
        json={
            "contract_version": 1,
            "kind": "coverage",
            "source_ids": [source.id],
            "idempotency_key": "synthetic-conformance-task",
        },
    )
    native_duration = _elapsed_ms(started)
    if native_response.status_code != 201:
        raise RuntimeError("native synthetic task did not complete")
    native_task = native_response.json()

    async def exercise_mcp() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        async with Client(build_mcp_server(db_path, project.id)) as client:
            started = perf_counter()
            same_task = await client.call_tool(
                "run_coverage",
                {
                    "source_ids": [source.id],
                    "idempotency_key": "synthetic-conformance-task",
                },
            )
            mcp_duration = _elapsed_ms(started)

            # This paged MCP sequence mimics a DSH client, not DSH inference.
            started = perf_counter()
            cursor = None
            read_calls = 0
            reconstructed = {1: "", 2: ""}
            excerpt = None
            while True:
                arguments = {"source_id": source.id, "limit": 1}
                if cursor is not None:
                    arguments["cursor"] = cursor
                evidence = await client.call_tool("source_excerpts", arguments)
                read_calls += 1
                if (
                    evidence.is_error
                    or evidence.structured_content["content_sha256"] != source.content_sha256
                    or not evidence.structured_content["excerpts"]
                ):
                    raise RuntimeError("synthetic source excerpt was unavailable")
                item = evidence.structured_content["excerpts"][0]
                if excerpt is None:
                    excerpt = item
                ordinal = item["ordinal"]
                if item["start_char"] != len(reconstructed[ordinal]):
                    raise RuntimeError("synthetic excerpt span was not contiguous")
                reconstructed[ordinal] += item["text"]
                next_cursor = evidence.structured_content["next_cursor"]
                if next_cursor is None:
                    break
                if next_cursor == cursor or read_calls >= 4:
                    raise RuntimeError("synthetic excerpt cursor did not advance")
                cursor = next_cursor
            if excerpt is None:
                raise RuntimeError("synthetic source excerpt was unavailable")
            proposal = await client.call_tool(
                "propose_candidate",
                {
                    "source_id": source.id,
                    "expected_source_sha256": evidence.structured_content["content_sha256"],
                    "source_locator": excerpt["locator"],
                    "subject": "经营情况",
                    "statement": "合成资料提到收入；需人工核验。",
                },
            )
            dsh_duration = _elapsed_ms(started)

            foreign_read = await client.call_tool("source_excerpts", {"source_id": foreign.id})
            stale_proposal = await client.call_tool(
                "propose_candidate",
                {
                    "source_id": source.id,
                    "expected_source_sha256": "c" * 64,
                    "source_locator": excerpt["locator"],
                    "subject": "经营情况",
                    "statement": "该版本不应写入。",
                },
            )
            checks = {
                "same_task_record": not same_task.is_error
                and same_task.structured_content["id"] == native_task["id"]
                and same_task.structured_content["outcome"] == native_task["outcome"],
                "cross_project_denied": foreign_read.is_error is True,
                "stale_hash_denied": stale_proposal.is_error is True,
                "proposal_candidate": not proposal.is_error
                and proposal.structured_content["status"] == "candidate",
                "full_source_read": reconstructed == expected_text,
                "proposal_id": proposal.structured_content.get("id")
                if not proposal.is_error
                else None,
            }
            routes = {
                "mcp": {"duration_ms": mcp_duration, "tool_calls": 1},
                "dsh_shaped_fake": {
                    "duration_ms": dsh_duration,
                    "tool_calls": read_calls + 1,
                },
            }
            return checks, routes, {"source_locator": excerpt["locator"]}

    mcp_checks, mcp_routes, evidence = asyncio.run(exercise_mcp())
    candidates = repository.list_observations(project.id, status="candidate")
    audit = repository.list_audit_events(project.id)
    proposal_audit = [event for event in audit if event.action == "candidate.created"]
    checks = {
        "same_task_record": mcp_checks["same_task_record"],
        "evidence_hash_and_locator_preserved": len(proposal_audit) == 1
        and proposal_audit[0].source_id == source.id
        and proposal_audit[0].source_sha256 == source.content_sha256
        and proposal_audit[0].source_locator == evidence["source_locator"],
        "candidate_unconfirmed": mcp_checks["proposal_candidate"]
        and len(candidates) == 1
        and candidates[0].id == mcp_checks["proposal_id"]
        and not repository.list_observations(project.id, status="confirmed"),
        "cross_project_denied": mcp_checks["cross_project_denied"],
        "stale_hash_denied": mcp_checks["stale_hash_denied"],
        "full_source_read": mcp_checks["full_source_read"],
    }
    return {
        "corpus": "synthetic_only",
        "real_model_or_dsh_used": False,
        "model_quality": "not_measured",
        "model_cost_usd": "not_measured",
        "timing_scope": "single_local_run_not_a_performance_benchmark",
        "routes": {
            "native_http": {"duration_ms": native_duration, "tool_calls": 1},
            **mcp_routes,
        },
        "checks": checks,
        "expected_denials": sum(
            checks[key] for key in ("cross_project_denied", "stale_hash_denied")
        ),
        "unexpected_failures": sum(not value for value in checks.values()),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="project-watch-synthetic-probe-") as temporary:
        report = run_probe(Path(temporary) / "synthetic-probe.sqlite3")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["unexpected_failures"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
