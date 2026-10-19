"""End-to-end local preview for a reviewed peer set and evidence-bound comparison."""

from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app


def _fact(client: TestClient, project_id: str, value: str, source_hash: str) -> None:
    root = f"/api/projects/{project_id}"
    source = client.post(
        f"{root}/sources",
        json={
            "kind": "document",
            "reference": f"{source_hash[0]}.pdf",
            "content_sha256": source_hash,
        },
    ).json()
    observation = client.post(
        f"{root}/observations",
        json={
            "source_id": source["id"],
            "subject": "财务表现",
            "statement": "营业收入",
            "number": value,
            "unit": "万元",
            "period": "2025",
            "scope": "合并",
            "basis": "actual",
            "source_locator": "利润表:B3",
        },
    ).json()
    assert (
        client.post(
            f"/api/observations/{observation['id']}/confirm",
            json={"reviewer": "local-reviewer", "expected_version": 1},
        ).status_code
        == 200
    )


def test_reviewed_peer_set_compares_only_linked_confirmed_sourced_metrics(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    subject = client.post("/api/projects", json={"name": "Synthetic Subject"}).json()
    peer_project = client.post("/api/projects", json={"name": "Synthetic Peer"}).json()
    root = f"/api/projects/{subject['id']}"
    _fact(client, subject["id"], "120", "a" * 64)
    _fact(client, peer_project["id"], "100", "b" * 64)

    proposed = client.post(
        f"{root}/competitive-peers",
        json={"name": "Synthetic Peer", "linked_project_id": peer_project["id"]},
    )
    assert proposed.status_code == 201
    candidate = proposed.json()
    assert candidate["status"] == "candidate"
    assert (
        client.get(f"{root}/competitive-position").json()["comparison"]["status"]
        == "missing_peer_set"
    )
    assert (
        client.post(
            f"{root}/competitive-peers/{candidate['id']}/review",
            json={"action": "confirm", "expected_version": 1, "reviewer": "local-reviewer"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{root}/competitive-peers/{candidate['id']}/review",
            json={"action": "confirm", "expected_version": 1, "reviewer": "local-reviewer"},
        ).status_code
        == 409
    )

    comparison = client.get(f"{root}/competitive-position").json()
    assert comparison["peer_access"][candidate["id"]] == "readable"
    assert comparison["comparison"]["status"] == "comparable"
    point = comparison["comparison"]["comparisons"][0]
    assert point["subject"]["value"] == "120"
    assert point["peers"][0]["value"] == "100"
    assert point["peers"][0]["source_sha256"] == "b" * 64
    assert comparison["comparison"]["rank"] is None


def test_name_only_peer_can_later_link_but_must_be_reviewed_again(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    subject = client.post("/api/projects", json={"name": "Synthetic Subject"}).json()
    other = client.post("/api/projects", json={"name": "Synthetic Other"}).json()
    root = f"/api/projects/{subject['id']}"
    _fact(client, subject["id"], "120", "c" * 64)
    created = client.post(f"{root}/competitive-peers", json={"name": "Name Only"}).json()
    assert (
        client.post(
            f"{root}/competitive-peers/{created['id']}/review",
            json={"action": "confirm", "expected_version": 1, "reviewer": "local-reviewer"},
        ).status_code
        == 200
    )
    response = client.get(f"{root}/competitive-position").json()
    assert response["peer_access"][created["id"]] == "no_linked_project"
    assert response["comparison"]["status"] == "insufficient_evidence"
    assert response["comparison"]["gaps"][0]["reason"] == "missing_metric"
    linked = client.post(
        f"{root}/competitive-peers",
        json={"name": "Name Only", "linked_project_id": other["id"]},
    )
    assert linked.status_code == 201
    assert linked.json()["id"] == created["id"]
    assert linked.json()["status"] == "candidate"
    assert linked.json()["version"] == 3
    assert (
        client.get(f"{root}/competitive-position").json()["comparison"]["status"]
        == "missing_peer_set"
    )
    assert (
        client.post(
            f"{root}/competitive-peers",
            json={"name": "Own Project", "linked_project_id": subject["id"]},
        ).status_code
        == 422
    )
