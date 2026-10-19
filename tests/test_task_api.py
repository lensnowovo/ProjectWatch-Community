"""HTTP task and candidate-proposal contracts use local synthetic evidence only."""

from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.domain import FragmentInput
from project_watch.repository import ProjectRepository


def test_coverage_task_api_idempotent_and_project_scoped(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    client = TestClient(create_app(path))
    project_id = client.post("/api/projects", json={"name": "Synthetic Tasks"}).json()["id"]
    other_id = client.post("/api/projects", json={"name": "Synthetic Other"}).json()["id"]
    payload = {
        "contract_version": 1,
        "kind": "coverage",
        "source_ids": [],
        "idempotency_key": "coverage-one",
    }

    first = client.post(f"/api/projects/{project_id}/tasks", json=payload)
    repeated = client.post(f"/api/projects/{project_id}/tasks", json=payload)

    assert first.status_code == 201
    assert first.json() == repeated.json()
    assert first.json()["outcome"]["confirmed_count"] == 0
    task_id = first.json()["id"]
    assert client.get(f"/api/projects/{project_id}/tasks/{task_id}").status_code == 200
    assert [item["id"] for item in client.get(f"/api/projects/{project_id}/tasks").json()] == [
        task_id
    ]
    assert client.get(f"/api/projects/{other_id}/tasks/{task_id}").status_code == 404
    assert client.get(f"/api/projects/{other_id}/tasks").json() == []
    assert (
        client.post(
            f"/api/projects/{project_id}/tasks", json={**payload, "contract_version": 2}
        ).status_code
        == 422
    )


def test_proposal_requires_current_source_and_real_locator(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    client = TestClient(create_app(path))
    repo = ProjectRepository(path)
    project = repo.create_project("Synthetic Proposal")
    other = repo.create_project("Other Proposal")
    source = repo.add_source(
        project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    repo.complete_intake(
        claimed.id, [FragmentInput(ordinal=1, locator="line 1", text="Revenue increased")]
    )
    payload = {
        "source_id": source.id,
        "expected_source_sha256": source.content_sha256,
        "source_locator": "line 1",
        "subject": "经营情况",
        "statement": "资料称收入增长，具体口径待核对。",
    }

    wrong_hash = client.post(
        f"/api/projects/{project.id}/candidate-proposals",
        json={**payload, "expected_source_sha256": "b" * 64},
    )
    bad_locator = client.post(
        f"/api/projects/{project.id}/candidate-proposals",
        json={**payload, "source_locator": "invented page"},
    )
    foreign = client.post(f"/api/projects/{other.id}/candidate-proposals", json=payload)
    accepted = client.post(f"/api/projects/{project.id}/candidate-proposals", json=payload)

    assert wrong_hash.status_code == 409
    assert bad_locator.status_code == 422
    assert foreign.status_code == 422
    assert accepted.status_code == 201
    assert accepted.json()["status"] == "candidate"
    assert client.get(f"/api/projects/{project.id}/summary").json()["confirmed_count"] == 0

    repeated = client.post(f"/api/projects/{project.id}/candidate-proposals", json=payload)
    assert repeated.json()["id"] == accepted.json()["id"]
    events = client.get(f"/api/projects/{project.id}/audit").json()
    proposal_events = [item for item in events if item["action"] == "candidate.created"]
    assert len(proposal_events) == 1
    assert proposal_events[0]["subject_id"] == accepted.json()["id"]
    assert proposal_events[0]["actor_kind"] == "unverified_executor"
    assert proposal_events[0]["channel"] == "http"
    assert proposal_events[0]["source_id"] == source.id
    assert proposal_events[0]["source_sha256"] == source.content_sha256
    assert proposal_events[0]["source_locator"] == "line 1"
    assert "Revenue increased" not in str(proposal_events)
    assert "资料称" not in str(proposal_events)
    assert client.get(f"/api/projects/{other.id}/audit").json() == []
    assert client.get(f"/api/projects/{project.id}/audit").json() == events


def test_numeric_executor_proposal_stays_candidate_until_review(tmp_path: Path) -> None:
    path = tmp_path / "metric-watch.sqlite3"
    client = TestClient(create_app(path))
    repo = ProjectRepository(path)
    project = repo.create_project("Synthetic Metric Executor")
    source = repo.add_source(
        project.id, kind="document", reference="synthetic-finance.txt", content_sha256="f" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    repo.complete_intake(
        claimed.id,
        [FragmentInput(ordinal=1, locator="line 2", text="2025 年营业收入 1,200.50 万元。")],
    )
    route = f"/api/projects/{project.id}"
    payload = {
        "source_id": source.id,
        "expected_source_sha256": source.content_sha256,
        "source_locator": "line 2",
        "subject": "财务表现",
        "statement": "营业收入",
        "number": "1200.50",
        "unit": "万元",
        "period": "2025",
        "scope": "合并报表",
    }
    proposed = client.post(f"{route}/candidate-proposals", json=payload)
    assert proposed.status_code == 201
    assert proposed.json()["status"] == "candidate"
    assert proposed.json()["number"] == "1200.50"
    assert (
        client.post(f"{route}/candidate-proposals", json=payload).json()["id"]
        == proposed.json()["id"]
    )
    assert client.get(f"{route}/metric-series").json()["series"] == []
    invalid = client.post(f"{route}/candidate-proposals", json={**payload, "number": "999.00"})
    assert invalid.status_code == 422
    assert len(repo.list_observations(project.id)) == 1
    confirmed = client.post(
        f"{route}/observations/{proposed.json()['id']}/confirm",
        json={"reviewer": "synthetic-reviewer", "expected_version": 1},
    )
    assert confirmed.status_code == 200
    point = client.get(f"{route}/metric-series").json()["series"][0]["points"][0]
    assert point["number"] == "1200.50"
    assert point["source_locator"] == "line 2"
