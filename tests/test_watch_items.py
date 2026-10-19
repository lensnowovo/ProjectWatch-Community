"""Reviewed, source-bound project watch items (synthetic local preview)."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app


def _confirmed_observation(client: TestClient, project_id: str, marker: str) -> dict:
    root = f"/api/projects/{project_id}"
    source = client.post(
        f"{root}/sources",
        json={"kind": "document", "reference": f"{marker}.txt", "content_sha256": marker * 64},
    ).json()
    observation = client.post(
        f"{root}/observations",
        json={
            "source_id": source["id"],
            "subject": "风险与合规",
            "statement": "主要客户续约条件尚待核对",
            "source_locator": "line 3",
        },
    ).json()
    confirmed = client.post(
        f"/api/observations/{observation['id']}/confirm",
        json={"reviewer": "synthetic-reviewer", "expected_version": 1},
    )
    assert confirmed.status_code == 200
    return confirmed.json()


def test_watch_item_requires_review_before_report_and_keeps_evidence(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    project = client.post("/api/projects", json={"name": "Synthetic Watch Company"}).json()
    root = f"/api/projects/{project['id']}"
    observation = _confirmed_observation(client, project["id"], "a")
    body = {
        "observation_id": observation["id"],
        "title": "核对客户续约",
        "question": "取得协议并核对续约期限和收入占比",
        "owner": "项目组",
        "due_on": "2026-12-31",
    }
    proposed = client.post(f"{root}/watch-items", json=body)
    assert proposed.status_code == 201, proposed.text
    item = proposed.json()
    assert item["status"] == "candidate"
    assert item["source_locator"] == "line 3"
    assert item["source_sha256"] == "a" * 64
    assert client.post(f"{root}/watch-items", json=body).json()["id"] == item["id"]
    changed_duplicate = client.post(
        f"{root}/watch-items", json={**body, "question": "另一项核对要求"}
    )
    assert changed_duplicate.status_code == 409
    assert client.get(f"{root}/report/current").json()["watch_items"] == []
    reviewed = client.post(
        f"{root}/watch-items/{item['id']}/review",
        json={
            "action": "require_action",
            "expected_version": 1,
            "reviewer": "synthetic-reviewer",
            "note": "需在下次访谈前核对",
        },
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "action_required"
    assert reviewed.json()["version"] == 2
    assert (
        client.post(
            f"{root}/watch-items/{item['id']}/review",
            json={"action": "reject", "expected_version": 1, "reviewer": "synthetic-reviewer"},
        ).status_code
        == 409
    )
    report_item = client.get(f"{root}/report/current").json()["watch_items"][0]
    assert report_item["status"] == "action_required"
    assert report_item["owner"] == "项目组"
    assert report_item["due_on"] == "2026-12-31"
    assert report_item["source_locator"] == "line 3"
    with sqlite3.connect(tmp_path / "watch.sqlite3") as connection:
        events = connection.execute(
            "SELECT version, to_status FROM watch_item_events WHERE watch_item_id = ? ORDER BY version",
            (item["id"],),
        ).fetchall()
    assert events == [(1, "candidate"), (2, "action_required")]


def test_watch_item_cannot_borrow_another_projects_observation(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    first = client.post("/api/projects", json={"name": "Synthetic First"}).json()
    second = client.post("/api/projects", json={"name": "Synthetic Second"}).json()
    observation = _confirmed_observation(client, first["id"], "b")
    response = client.post(
        f"/api/projects/{second['id']}/watch-items",
        json={"observation_id": observation["id"], "title": "不允许跨项目", "question": "核对"},
    )
    assert response.status_code in {404, 422}


def test_watch_item_change_requires_review_and_can_resolve_then_reopen(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    project = client.post("/api/projects", json={"name": "Synthetic Lifecycle"}).json()
    root = f"/api/projects/{project['id']}"
    original = _confirmed_observation(client, project["id"], "c")
    follow_up = _confirmed_observation(client, project["id"], "d")
    item = client.post(
        f"{root}/watch-items",
        json={
            "observation_id": original["id"],
            "title": "核对客户续约",
            "question": "取得书面协议",
        },
    ).json()
    accepted = client.post(
        f"{root}/watch-items/{item['id']}/review",
        json={"action": "require_action", "expected_version": 1, "reviewer": "local-reviewer"},
    ).json()
    assert accepted["version"] == 2

    change_body = {
        "expected_version": 2,
        "target_status": "resolved",
        "question": "核对续约价格与期限",
        "owner": "投资组",
        "due_on": "2026-12-31",
        "evidence_observation_id": follow_up["id"],
        "reason": "已取得新的续约材料，申请结束本轮核对",
    }
    proposed = client.post(f"{root}/watch-items/{item['id']}/changes", json=change_body)
    assert proposed.status_code == 201
    change = proposed.json()
    assert change["status"] == "candidate"
    assert (
        client.post(f"{root}/watch-items/{item['id']}/changes", json=change_body).json()["id"]
        == change["id"]
    )
    assert (
        client.get(f"{root}/report/current").json()["watch_items"][0]["status"] == "action_required"
    )
    conflicting = client.post(
        f"{root}/watch-items/{item['id']}/changes",
        json={**change_body, "reason": "另一项变更"},
    )
    assert conflicting.status_code == 409
    reviewed = client.post(
        f"{root}/watch-items/{item['id']}/changes/{change['id']}/review",
        json={"action": "accept", "expected_version": 2, "reviewer": "local-reviewer"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "resolved"
    assert reviewed.json()["version"] == 3
    assert reviewed.json()["current_observation_id"] == follow_up["id"]
    assert reviewed.json()["source_sha256"] == "d" * 64
    report = client.get(f"{root}/report/current").json()
    assert report["watch_items"] == []
    assert report["resolved_watch_items"][0]["id"] == item["id"]
    assert report["resolved_watch_items"][0]["source_sha256"] == "d" * 64
    assert report["resolved_watch_items"][0]["resolution_reason"] == change_body["reason"]
    assert (
        client.post(
            f"{root}/watch-items/{item['id']}/changes/{change['id']}/review",
            json={"action": "accept", "expected_version": 2, "reviewer": "local-reviewer"},
        ).status_code
        == 409
    )

    reopen = client.post(
        f"{root}/watch-items/{item['id']}/changes",
        json={
            "expected_version": 3,
            "target_status": "watching",
            "question": "复核续约实际履行情况",
            "owner": "投后组",
            "due_on": None,
            "reason": "合同履行情况尚待再次确认",
        },
    ).json()
    assert reopen["status"] == "candidate"
    assert client.get(f"{root}/report/current").json()["watch_items"] == []
    reopened = client.post(
        f"{root}/watch-items/{item['id']}/changes/{reopen['id']}/review",
        json={"action": "accept", "expected_version": 3, "reviewer": "local-reviewer"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "watching"
    assert reopened.json()["version"] == 4
    assert reopened.json()["owner"] == "投后组"
    assert reopened.json()["due_on"] is None
    assert client.get(f"{root}/report/current").json()["watch_items"][0]["id"] == item["id"]


def test_watch_item_change_rejection_and_cross_project_evidence(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    first = client.post("/api/projects", json={"name": "Synthetic First"}).json()
    second = client.post("/api/projects", json={"name": "Synthetic Second"}).json()
    root = f"/api/projects/{first['id']}"
    original = _confirmed_observation(client, first["id"], "e")
    foreign = _confirmed_observation(client, second["id"], "f")
    item = client.post(
        f"{root}/watch-items",
        json={"observation_id": original["id"], "title": "核对收入", "question": "取得明细"},
    ).json()
    client.post(
        f"{root}/watch-items/{item['id']}/review",
        json={"action": "watch", "expected_version": 1, "reviewer": "local-reviewer"},
    )
    body = {
        "expected_version": 2,
        "target_status": "action_required",
        "question": "取得收入与客户明细",
        "owner": None,
        "due_on": None,
        "reason": "需要补充核对范围",
        "evidence_observation_id": foreign["id"],
    }
    assert client.post(f"{root}/watch-items/{item['id']}/changes", json=body).status_code in {
        404,
        422,
    }
    body["evidence_observation_id"] = None
    change = client.post(f"{root}/watch-items/{item['id']}/changes", json=body).json()
    rejected = client.post(
        f"{root}/watch-items/{item['id']}/changes/{change['id']}/review",
        json={"action": "reject", "expected_version": 2, "reviewer": "local-reviewer"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "watching"
    assert rejected.json()["version"] == 2
    assert client.get(f"{root}/report/current").json()["watch_items"][0]["question"] == "取得明细"
