from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "watch.sqlite3"))


def test_quality_hint_is_read_only_and_never_rejects_existing_candidate(tmp_path: Path):
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Review"}).json()
    source = client.post(f"/api/projects/{project['id']}/sources", json={
        "kind": "document", "reference": "synthetic.pdf", "content_sha256": "f" * 64,
    }).json()
    path = f"/api/projects/{project['id']}/observations"
    candidate = client.post(path, json={"source_id": source["id"], "subject": "财务表现", "statement": "资产负债表 3-4", "source_locator": "page 1"}).json()
    assert candidate["quality_hint"] == "目录项，不是业务陈述"
    for _ in range(2):
        row = client.get(path).json()[0]
        assert row["id"] == candidate["id"] and row["status"] == "candidate" and row["version"] == 1
    assert client.get(f"/api/observations/{candidate['id']}/reviews").json() == []


def test_local_preview_can_read_but_not_attest_project_identity(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Entity"}).json()
    path = f"/api/projects/{project['id']}/identity"
    assert client.get(path).json() == {"status": "unverified", "history": []}
    assert client.post(path, json={}).status_code == 405


def test_name_to_sourced_review_flow(tmp_path: Path) -> None:
    client = _client(tmp_path)

    created = client.post("/api/projects", json={"name": " Synthetic Company "})
    assert created.status_code == 201
    project = created.json()
    assert project["identity_status"] == "unverified"
    assert client.get(f"/api/projects/{project['id']}").json() == project

    source_response = client.post(
        f"/api/projects/{project['id']}/sources",
        json={
            "kind": "document",
            "reference": "synthetic.pdf",
            "content_sha256": "a" * 64,
        },
    )
    assert source_response.status_code == 201
    source = source_response.json()

    candidate_response = client.post(
        f"/api/projects/{project['id']}/observations",
        json={
            "source_id": source["id"],
            "subject": "revenue",
            "statement": "Synthetic reported revenue",
            "number": "120.50",
            "unit": "CNY million",
            "period": "2025 FY",
            "scope": "company",
            "source_locator": "p. 3, table 2",
        },
    )
    assert candidate_response.status_code == 201
    candidate = candidate_response.json()
    assert candidate["status"] == "candidate"
    assert candidate["number"] == "120.50"
    assert candidate["basis"] == "unknown"

    pending_summary = client.get(f"/api/projects/{project['id']}/summary").json()
    assert pending_summary["candidate_count"] == 1
    assert pending_summary["confirmed_count"] == 0

    confirmed_response = client.post(
        f"/api/observations/{candidate['id']}/confirm",
        json={"reviewer": "analyst-1", "expected_version": 1},
    )
    assert confirmed_response.status_code == 200
    assert confirmed_response.json()["status"] == "confirmed"
    assert client.get(f"/api/projects/{project['id']}/summary").json()["confirmed_count"] == 1
    reviews = client.get(f"/api/observations/{candidate['id']}/reviews")
    assert reviews.status_code == 200
    assert [(row["reviewer"], row["action"]) for row in reviews.json()] == [
        ("analyst-1", "confirm")
    ]

    stale_response = client.post(
        f"/api/observations/{candidate['id']}/confirm",
        json={"reviewer": "analyst-2", "expected_version": 1},
    )
    assert stale_response.status_code == 409


def test_reject_candidate_retains_audit_without_monitoring_fact(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Review"}).json()
    route = f"/api/projects/{project['id']}"
    source = client.post(
        f"{route}/sources",
        json={"kind": "document", "reference": "synthetic.xlsx", "content_sha256": "b" * 64},
    ).json()
    candidate = client.post(
        f"{route}/observations",
        json={
            "source_id": source["id"],
            "subject": "财务表现",
            "statement": "营业收入",
            "number": "120",
            "unit": "万元",
            "period": "2025",
            "scope": "合并",
            "basis": "actual",
            "source_locator": "利润表!B4",
        },
    ).json()

    rejected = client.post(
        f"{route}/observations/{candidate['id']}/reject",
        json={"reviewer": "analyst-1", "expected_version": 1},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["version"] == 2
    assert [
        row["id"]
        for row in client.get(f"{route}/observations", params={"status_filter": "rejected"}).json()
    ] == [candidate["id"]]
    summary = client.get(f"{route}/summary").json()
    assert summary["candidate_count"] == summary["confirmed_count"] == 0
    report = client.get(f"{route}/report/current").json()
    assert report["candidate_count"] == report["confirmed_count"] == 0
    assert report["metric_trends"] == report["confirmed_facts"] == []
    domains = {
        row["key"]: row for row in client.get(f"{route}/monitoring/domains").json()["domains"]
    }
    assert domains["financials"]["status"] == "missing"
    assert domains["financials"]["source_count"] == 0
    assert domains["financials"]["last_recorded_at"] is None
    coverage = client.post(
        f"{route}/tasks",
        json={
            "contract_version": 1,
            "kind": "coverage",
            "source_ids": [source["id"]],
            "idempotency_key": "rejected-coverage",
        },
    ).json()["outcome"]
    assert coverage["covered_topics"] == []
    assert "财务表现" in coverage["missing_topics"]
    assert (
        client.get(f"/api/observations/{candidate['id']}/reviews").json()[0]["action"] == "reject"
    )
    assert any(row["action"] == "candidate.rejected" for row in client.get(f"{route}/audit").json())
    assert (
        client.post(
            f"{route}/observations/{candidate['id']}/confirm",
            json={"reviewer": "analyst-1", "expected_version": 1},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{route}/observations/{candidate['id']}/reject",
            json={"reviewer": "analyst-1", "expected_version": 2},
        ).status_code
        == 409
    )


def test_api_rejects_unbacked_observation(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Company"}).json()

    response = client.post(
        f"/api/projects/{project['id']}/observations",
        json={
            "source_id": "missing",
            "subject": "finance",
            "statement": "Unsupported claim",
            "source_locator": "unknown",
        },
    )

    assert response.status_code == 404
    summary = client.get(f"/api/projects/{project['id']}/summary").json()
    assert summary["candidate_count"] == 0
    assert summary["confirmed_count"] == 0


def test_financial_screen_reads_only_reviewed_actuals(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Screen"}).json()
    route = f"/api/projects/{project['id']}"
    source = client.post(
        f"{route}/sources",
        json={"kind": "document", "reference": "synthetic.xlsx", "content_sha256": "a" * 64},
    ).json()
    candidate = client.post(
        f"{route}/observations",
        json={
            "source_id": source["id"],
            "subject": "财务表现",
            "statement": "营业收入",
            "number": "120",
            "unit": "万元",
            "period": "2025",
            "scope": "合并",
            "basis": "actual",
            "source_locator": "利润表!B4",
        },
    ).json()
    path = f"{route}/financial-screen"
    before = client.get(path)
    assert before.status_code == 200
    assert before.json()["analysis"]["status"] == "insufficient_data"
    client.post(
        f"/api/observations/{candidate['id']}/confirm",
        json={"reviewer": "synthetic-reviewer", "expected_version": 1},
    )
    after = client.get(path)
    assert after.status_code == 200
    assert after.json()["accepted_count"] == 1
    assert after.json()["analysis"]["status"] == "partial"
    assert after.json()["evidence"][0]["source_locator"] == "利润表!B4"
    domains = {
        item["key"]: item for item in client.get(f"{route}/monitoring/domains").json()["domains"]
    }
    assert domains["financials"]["status"] == "evidence_available"
    assert domains["financials"]["source_count"] == 1
    assert domains["financials"]["last_recorded_at"]
    assert domains["financial_integrity"]["status"] == "needs_review"
    assert domains["financial_integrity"]["source_count"] == 1


def test_capital_domain_requires_review_and_returns_source_location(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Investment"}).json()
    route = f"/api/projects/{project['id']}"
    source = client.post(
        f"{route}/sources",
        json={"kind": "document", "reference": "synthetic-round.pdf", "content_sha256": "c" * 64},
    ).json()
    candidate = client.post(
        f"{route}/observations",
        json={
            "source_id": source["id"],
            "subject": "资本与持仓",
            "statement": "本机构持股比例",
            "number": "8.5",
            "unit": "%",
            "period": "2026-09",
            "scope": "本机构",
            "basis": "actual",
            "source_locator": "page 3",
        },
    ).json()

    def capital() -> dict:
        domains = client.get(f"{route}/monitoring/domains").json()["domains"]
        return next(item for item in domains if item["key"] == "capital")

    assert capital()["status"] == "needs_review"
    assert capital()["evidence"] == []
    confirmed = client.post(
        f"/api/observations/{candidate['id']}/confirm",
        json={"reviewer": "synthetic-reviewer", "expected_version": 1},
    )
    assert confirmed.status_code == 200
    assert capital()["status"] == "evidence_available"
    assert capital()["evidence"][0]["source_id"] == source["id"]
    assert capital()["evidence"][0]["source_locator"] == "page 3"
    assert capital()["evidence"][0]["source_sha256"] == "c" * 64


def test_activity_timeline_exposes_only_reviewed_located_records(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Activity"}).json()
    route = f"/api/projects/{project['id']}"
    source = client.post(
        f"{route}/sources",
        json={
            "kind": "document",
            "reference": "synthetic-progress.txt",
            "content_sha256": "d" * 64,
        },
    ).json()
    candidate = client.post(
        f"{route}/observations",
        json={
            "source_id": source["id"],
            "subject": "产品与技术",
            "statement": "合成测试产品记录",
            "period": "2026-09",
            "source_locator": "line 2",
        },
    ).json()
    path = f"{route}/monitoring/domains"
    before = client.get(path).json()["activity"]
    assert before["confirmed_count"] == 0
    assert before["pending_count"] == 1
    assert before["entries"] == []
    client.post(
        f"/api/observations/{candidate['id']}/confirm",
        json={"reviewer": "synthetic-reviewer", "expected_version": 1},
    )
    after = client.get(path).json()["activity"]
    assert after["confirmed_count"] == 1
    assert after["pending_count"] == 0
    assert after["entries"][0]["subject"] == "产品与技术"
    assert after["entries"][0]["source_sha256"] == "d" * 64
    assert after["entries"][0]["source_locator"] == "line 2"


def test_operating_metrics_api_requires_review_and_preserves_period_scope(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Operating Company"}).json()
    route = f"/api/projects/{project['id']}"
    source = client.post(
        f"{route}/sources",
        json={
            "kind": "document",
            "reference": "synthetic-capacity.xlsx",
            "content_sha256": "e" * 64,
        },
    ).json()
    candidate = client.post(
        f"{route}/observations",
        json={
            "source_id": source["id"],
            "subject": "经营情况",
            "statement": "实际产量",
            "number": "80",
            "unit": "台",
            "period": "2026-09",
            "scope": "A 产品线",
            "source_locator": "产量表!B4",
            "basis": "actual",
        },
    ).json()
    path = f"{route}/monitoring/domains"
    before = client.get(path).json()["operating_metrics"]
    assert before["actual_count"] == 0
    assert before["pending_count"] == 1
    client.post(
        f"/api/observations/{candidate['id']}/confirm",
        json={"reviewer": "synthetic-reviewer", "expected_version": 1},
    )
    after = client.get(path).json()["operating_metrics"]
    assert after["actual_count"] == 1
    assert after["evidence"][0]["value"] == "80"
    assert after["evidence"][0]["period"] == "2026-09"
    assert after["evidence"][0]["scope"] == "A 产品线"
    assert after["evidence"][0]["source_sha256"] == "e" * 64
    assert after["evidence"][0]["source_locator"] == "产量表!B4"


def test_metric_series_api_only_shows_confirmed_values_with_source_version(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Metric Company"}).json()
    other = client.post("/api/projects", json={"name": "Other Synthetic Company"}).json()
    source = client.post(
        f"/api/projects/{project['id']}/sources",
        json={"kind": "document", "reference": "synthetic.xlsx", "content_sha256": "b" * 64},
    ).json()
    route = f"/api/projects/{project['id']}"
    first = client.post(
        f"{route}/observations",
        json={
            "source_id": source["id"],
            "subject": "财务表现",
            "statement": "营业收入",
            "number": "0",
            "unit": "万元",
            "period": "2024",
            "scope": "合并",
            "source_locator": "利润表!B3",
        },
    ).json()
    client.post(
        f"{route}/observations",
        json={
            "source_id": source["id"],
            "subject": "财务表现",
            "statement": "营业收入",
            "number": "12.5",
            "unit": "万元",
            "period": "2025",
            "scope": "合并",
            "source_locator": "利润表!C3",
        },
    )
    assert client.get(f"{route}/metric-series").json()["series"] == []
    client.post(
        f"{route}/observations/{first['id']}/confirm",
        json={"reviewer": "synthetic-reviewer", "expected_version": 1},
    )
    result = client.get(f"{route}/metric-series")
    assert result.status_code == 200
    assert result.json()["total_series"] == 1
    point = result.json()["series"][0]["points"][0]
    assert point["number"] == "0"
    assert point["source_sha256"] == "b" * 64
    assert point["source_locator"] == "利润表!B3"
    assert client.get(f"/api/projects/{other['id']}/metric-series").json()["series"] == []


def test_api_never_invents_monitoring_values(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Synthetic Company"}).json()

    summary = client.get(f"/api/projects/{project['id']}/summary").json()

    assert summary["source_count"] == 0
    assert summary["candidate_count"] == 0
    assert summary["confirmed_count"] == 0
    assert "risk_score" not in summary
    assert "revenue" not in summary


def test_numeric_basis_is_explicit_and_part_of_candidate_identity(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Basis Example"}).json()
    route = f"/api/projects/{project['id']}"
    source = client.post(
        f"{route}/sources",
        json={"kind": "document", "reference": "synthetic.xls", "content_sha256": "c" * 64},
    ).json()
    claim = {
        "source_id": source["id"],
        "subject": "财务表现",
        "statement": "营业收入",
        "number": "120",
        "unit": "万元",
        "period": "2025",
        "source_locator": "sheet S row 2",
    }
    actual = client.post(f"{route}/observations", json={**claim, "basis": "actual"})
    forecast = client.post(f"{route}/observations", json={**claim, "basis": "forecast"})
    assert actual.status_code == forecast.status_code == 201
    assert actual.json()["id"] != forecast.json()["id"]
    assert {row["basis"] for row in client.get(f"{route}/observations").json()} == {
        "actual",
        "forecast",
    }
    assert (
        client.post(f"{route}/observations", json={**claim, "basis": "guessed"}).status_code == 422
    )


def test_api_validation_and_missing_project(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.post("/api/projects", json={"name": " "}).status_code == 422
    assert client.get("/api/projects/missing").status_code == 404
    assert client.get("/api/projects/missing/summary").status_code == 404
