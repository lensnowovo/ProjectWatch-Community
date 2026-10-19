"""Synthetic end-to-end proof for the local holding review chain."""

from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.domain import FragmentInput
from project_watch.repository import ProjectRepository


def _setup(tmp_path: Path):
    path = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(path)
    project = repo.create_project("Synthetic Portfolio Company")
    foreign = repo.create_project("Synthetic Other Company")
    source = repo.add_source(
        project.id, kind="document", reference="holdings.txt", content_sha256="a" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    repo.complete_intake(
        claimed.id,
        [FragmentInput(1, "line 1", "甲基金 2026-06-30 成本 1,100 万元，持股 8%，价值 1,430 万元")],
    )
    return TestClient(create_app(path)), project.id, foreign.id, source.id


def test_holding_candidate_requires_review_before_appearing_in_monitor_and_brief(
    tmp_path: Path,
) -> None:
    client, project_id, foreign_id, source_id = _setup(tmp_path)
    root = f"/api/projects/{project_id}"
    body = {
        "fund_name": "甲基金",
        "as_of": "2026-06-30",
        "source_id": source_id,
        "source_locator": "line 1",
        "invested_cost_wan": "1100",
        "ownership_pct": "8",
        "current_value_wan": "1430",
    }
    assert (
        client.post(f"/api/projects/{foreign_id}/holding-snapshots", json=body).status_code == 404
    )
    assert (
        client.post(f"{root}/holding-snapshots", json={**body, "ownership_pct": "9"}).status_code
        == 422
    )
    proposed = client.post(f"{root}/holding-snapshots", json=body)
    assert proposed.status_code == 201, proposed.text
    candidate = proposed.json()
    assert candidate["status"] == "candidate"
    assert client.get(f"{root}/holding-snapshots").json()[0]["id"] == candidate["id"]
    capital = next(
        row
        for row in client.get(f"{root}/monitoring/domains").json()["domains"]
        if row["key"] == "capital"
    )
    assert capital["holdings"]["positions"] == []
    assert client.get(f"{root}/report/current").json()["domains"][0]["holdings"]["positions"] == []
    reviewed = client.post(
        f"{root}/holding-snapshots/{candidate['id']}/review",
        json={"action": "confirm", "expected_version": 1, "reviewer": "local-reviewer"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "confirmed"
    assert (
        client.post(
            f"{root}/holding-snapshots/{candidate['id']}/review",
            json={"action": "reject", "expected_version": 1, "reviewer": "local-reviewer"},
        ).status_code
        == 409
    )
    capital = next(
        row
        for row in client.get(f"{root}/monitoring/domains").json()["domains"]
        if row["key"] == "capital"
    )
    assert capital["status"] == "evidence_available"
    assert capital["holdings"]["positions"][0]["fund_name"] == "甲基金"
    assert capital["holdings"]["positions"][0]["source_sha256"] == "a" * 64
    assert (
        client.get(f"{root}/report/current").json()["domains"][0]["holdings"]["positions"][0]["moc"]
        == "1.3"
    )
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    repo.add_source(project_id, kind="document", reference="later.txt", content_sha256="b" * 64)
    position = next(
        row for row in client.get(f"{root}/monitoring/domains").json()["domains"]
        if row["key"] == "capital"
    )["holdings"]["positions"][0]
    assert position["new_material_count_since_review"] == 1
    assert position["invested_cost_wan"] == "1100"
    brief_position = client.get(f"{root}/report/current").json()["holdings"]["positions"][0]
    assert brief_position["new_material_count_since_review"] == 1
