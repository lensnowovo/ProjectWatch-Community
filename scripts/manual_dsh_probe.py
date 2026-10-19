"""Explicit real-model acceptance through the local manual API, synthetic only."""

import hashlib
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.domain import FragmentInput
from project_watch.dsh_executor import configured_local_model
from project_watch.repository import ProjectRepository


def main():
    model = configured_local_model()
    if model is None:
        raise SystemExit("Explicit model opt-in required")
    with tempfile.TemporaryDirectory(prefix="watch-manual-synthetic-") as root:
        path = Path(root) / "synthetic.sqlite3"
        repo = ProjectRepository(path)
        project = repo.create_project("Synthetic model UI acceptance")
        text = "2026年预测收入42万元，仅为合成测试数据，并非任何真实公司的信息。"
        source = repo.add_source(
            project.id,
            kind="document",
            reference="synthetic.txt",
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
        repo.enqueue_intake(project.id, source.id)
        job = repo.claim_next_intake()
        repo.complete_intake(job.id, [FragmentInput(ordinal=1, locator="line 1", text=text)])
        with TestClient(
            create_app(path, proposal_model=model), base_url="http://127.0.0.1"
        ) as client:
            route = f"/api/projects/{project.id}/ai-extraction"
            preview = client.get(route, params={"source_id": source.id, "source_locator": "line 1"})
            assert preview.status_code == 200
            response = client.post(
                route,
                json={"consent": True, "ticket": preview.json()["ticket"]},
                headers={"Origin": "http://127.0.0.1"},
            )
            observations = repo.list_observations(project.id)
            checks = {
                "api_completed": response.status_code == 200,
                "one_pending_candidate": len(observations) == 1
                and observations[0].status == "candidate",
                "metric_preserved": len(observations) == 1
                and str(observations[0].number) == "42"
                and observations[0].period == "2026"
                and observations[0].basis == "forecast",
                "none_confirmed": not repo.list_observations(project.id, status="confirmed"),
                "single_model_call": model.receipt.get("requests_admitted") == 1,
            }
            print(
                json.dumps(
                    {
                        "corpus": "synthetic_only",
                        "model": model.config.model,
                        "checks": checks,
                        "usage": model.receipt,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if all(checks.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
