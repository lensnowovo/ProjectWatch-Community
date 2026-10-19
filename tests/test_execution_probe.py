"""Synthetic executor comparison cannot be mistaken for a model benchmark."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")

from scripts.execution_probe import run_probe


def test_probe_compares_native_mcp_and_dsh_shaped_paths(tmp_path: Path) -> None:
    report = run_probe(tmp_path / "synthetic-probe.sqlite3")

    assert report["corpus"] == "synthetic_only"
    assert report["real_model_or_dsh_used"] is False
    assert report["model_quality"] == "not_measured"
    assert report["model_cost_usd"] == "not_measured"
    assert report["timing_scope"] == "single_local_run_not_a_performance_benchmark"
    assert set(report["routes"]) == {"native_http", "mcp", "dsh_shaped_fake"}
    assert all(route["duration_ms"] >= 0 for route in report["routes"].values())
    assert report["routes"]["dsh_shaped_fake"]["tool_calls"] == 4
    assert report["checks"] == {
        "same_task_record": True,
        "evidence_hash_and_locator_preserved": True,
        "candidate_unconfirmed": True,
        "full_source_read": True,
        "cross_project_denied": True,
        "stale_hash_denied": True,
    }
    assert report["expected_denials"] == 2
    assert report["unexpected_failures"] == 0


def test_probe_module_cli_emits_only_synthetic_summary() -> None:
    product_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "scripts.execution_probe"],
        cwd=product_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["corpus"] == "synthetic_only"
    assert report["checks"]["candidate_unconfirmed"] is True
    assert "Synthetic revenue" not in result.stdout
