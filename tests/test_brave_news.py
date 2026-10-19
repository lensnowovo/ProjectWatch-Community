"""The paid news path is entirely synthetic in tests: no API key or network use."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.brave_news import BraveNewsAdapter, brave_news_target, is_brave_news_target
from project_watch.repository import ProjectRepository


def test_brave_target_is_fixed_and_does_not_contain_credentials() -> None:
    target = brave_news_target("合成测试科技")
    assert target.startswith("https://api.search.brave.com/res/v1/news/search?")
    assert "q=%22%E5%90%88%E6%88%90" in target
    assert "freshness=pw" in target
    assert "search_lang=zh-hans" in target
    assert is_brave_news_target(target)
    assert not is_brave_news_target(target + "&extra=1")
    assert not is_brave_news_target(target.replace("freshness=pw", "freshness=py"))
    assert not is_brave_news_target(target.replace("search_lang=zh-hans", "search_lang=en"))


def test_brave_language_preference_preserves_older_chinese_plans() -> None:
    chinese_target = brave_news_target("合成测试科技")
    assert is_brave_news_target(chinese_target.replace("&search_lang=zh-hans", ""))
    english_target = brave_news_target("Synthetic Company")
    assert "search_lang=" not in english_target
    assert is_brave_news_target(english_target)


@pytest.mark.parametrize("name", ["A", 'Fake "Name"', "x\\y", " "])
def test_brave_target_rejects_ambiguous_names(name: str) -> None:
    with pytest.raises(ValueError):
        brave_news_target(name)


def test_brave_adapter_has_atomic_monthly_budget_and_candidate_only_result(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    calls = []

    def transport(url: str, key: str):
        calls.append((url, key))
        return 200, json.dumps(
            {
                "type": "news",
                "results": [
                    {
                        "title": "合成测试科技发布新产品",
                        "url": "https://example.org/news",
                        "description": "公开线索",
                        "page_age": "2026-09-30T10:00:00Z",
                    },
                    {"title": "Unsafe", "url": "http://127.0.0.1/private", "description": "skip"},
                ],
            }
        ).encode()

    adapter = BraveNewsAdapter(
        "synthetic-key",
        monthly_limit=1,
        reserve=repo.reserve_brave_news_request,
        transport=transport,
    )
    target = brave_news_target("合成测试科技")
    first = adapter.fetch(target)
    assert len(first.items) == 1
    assert first.items[0].url == "https://example.org/news"
    assert calls == [(target, "synthetic-key")]
    assert adapter.fetch(target).unavailable_error == "quota_exhausted"
    assert len(calls) == 1
    assert repo.brave_news_usage() == 1


def test_brave_adapter_fails_closed_without_key_or_budget(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    target = brave_news_target("Synthetic Company")
    calls = []
    adapter = BraveNewsAdapter(
        None,
        monthly_limit=100,
        reserve=repo.reserve_brave_news_request,
        transport=lambda *_: calls.append(1),
    )
    assert adapter.fetch(target).unavailable_error == "credential_missing"
    adapter = BraveNewsAdapter(
        "synthetic-key",
        monthly_limit=0,
        reserve=repo.reserve_brave_news_request,
        transport=lambda *_: calls.append(1),
    )
    assert adapter.fetch(target).unavailable_error == "quota_exhausted"
    assert calls == []
    assert repo.brave_news_usage() == 0


def test_brave_proxy_must_be_explicit_loopback(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    with pytest.raises(ValueError):
        BraveNewsAdapter(
            "synthetic-key",
            monthly_limit=1,
            reserve=repo.reserve_brave_news_request,
            proxy_url="http://example.org:17891",
        )
    adapter = BraveNewsAdapter(
        "synthetic-key",
        monthly_limit=1,
        reserve=repo.reserve_brave_news_request,
        proxy_url="http://127.0.0.1:17891",
        transport=lambda *_: (200, b'{"results":[]}'),
    )
    assert adapter.fetch(brave_news_target("Synthetic Company")).items == ()


def test_invalid_brave_configuration_fails_before_opening_database(tmp_path: Path) -> None:
    db_path = tmp_path / "watch.sqlite3"
    with pytest.raises(ValueError):
        create_app(
            db_path,
            brave_api_key="synthetic-key",
            brave_monthly_limit=1,
            brave_proxy_url="http://example.org:17891",
        )
    assert not db_path.exists()


def test_brave_adapter_records_provider_rate_limit_without_leaking_body(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    adapter = BraveNewsAdapter(
        "synthetic-key",
        monthly_limit=5,
        reserve=repo.reserve_brave_news_request,
        transport=lambda *_: (429, b'{"error":{"code":"QUOTA_LIMITED","detail":"secret"}}'),
    )
    result = adapter.fetch(brave_news_target("Synthetic Company"))
    assert result.unavailable_error == "rate_limited"
    assert not result.items
    limited_on_403 = BraveNewsAdapter(
        "synthetic-key",
        monthly_limit=5,
        reserve=repo.reserve_brave_news_request,
        transport=lambda *_: (403, b'{"error":{"code":"QUOTA_LIMITED","detail":"secret"}}'),
    )
    assert (
        limited_on_403.fetch(brave_news_target("Synthetic Company")).unavailable_error
        == "rate_limited"
    )


def test_paid_provider_requires_server_configuration_and_explicit_project_consent(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    project = client.post("/api/projects", json={"name": "Synthetic Company"}).json()
    path = f"/api/projects/{project['id']}/monitoring/public-search"
    preview = client.get(path, params={"provider": "brave"}).json()
    assert preview["eligible"] is False
    assert preview["monthly_limit"] == 0
    assert client.post(path, json={"provider": "brave"}).status_code == 409
    assert client.get(f"/api/projects/{project['id']}/monitoring/plans").json() == []

    configured = TestClient(
        create_app(tmp_path / "watch.sqlite3", brave_api_key="synthetic-key", brave_monthly_limit=1)
    )
    preview = configured.get(path, params={"provider": "brave"}).json()
    assert preview["eligible"] is True
    assert preview["target"] == brave_news_target("Synthetic Company")
    assert preview["monthly_used"] == 0
    started = configured.post(path, json={"provider": "brave"})
    assert started.status_code == 202
    assert started.json()["plan"]["kind"] == "brave_news"
    assert started.json()["job"]["status"] == "queued"
    assert configured.post(path, json={"provider": "brave"}).json() == started.json()


def test_brave_full_local_run_keeps_results_unverified_and_stops_at_budget(
    tmp_path: Path,
) -> None:
    database = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(database)
    calls = []

    def transport(url: str, key: str):
        calls.append(url)
        return 200, json.dumps(
            {
                "results": [
                    {
                        "title": "Synthetic Company product update",
                        "url": "https://example.org/one",
                        "page_age": datetime.now(UTC).isoformat(),
                    },
                    {"title": "Unrelated item", "url": "https://example.org/two"},
                ]
            }
        ).encode()

    adapter = BraveNewsAdapter(
        "synthetic-key",
        monthly_limit=1,
        reserve=repo.reserve_brave_news_request,
        transport=transport,
    )
    app = create_app(
        database,
        brave_api_key="synthetic-key",
        brave_monthly_limit=1,
        monitoring_adapters={"brave_news": adapter},
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={"name": "Synthetic Company"}).json()
    base = f"/api/projects/{project['id']}/monitoring"
    started = client.post(f"{base}/public-search", json={"provider": "brave"}).json()
    plan_id = started["plan"]["id"]
    assert app.state.monitoring_scheduler.process_one().status == "done"
    signals = client.get(f"{base}/signals").json()
    assert len(signals) == 1 and signals[0]["status"] == "candidate"
    assert client.get(f"/api/projects/{project['id']}/summary").json()["confirmed_count"] == 0
    assert client.get(f"{base}/public-search?provider=brave").json()["eligible"] is False
    assert repo.brave_news_usage() == 1

    client.post(f"{base}/plans/{plan_id}/runs", json={"idempotency_key": "second"})
    assert app.state.monitoring_scheduler.process_one().status == "done"
    receipts = client.get(f"{base}/plans/{plan_id}/runs").json()
    assert receipts[-1]["status"] == "rate_limited"
    assert receipts[-1]["error_code"] == "quota_exhausted"
    assert len(calls) == 1


def test_brave_two_cycles_only_add_new_candidates(tmp_path: Path) -> None:
    database = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(database)
    calls = []
    published_at = datetime.now(UTC).isoformat()

    def transport(url: str, key: str):
        calls.append((url, key))
        results = [
            {
                "title": "Synthetic Company opens a new line",
                "url": "https://example.org/one",
                "page_age": published_at,
            },
        ]
        if len(calls) == 2:
            results.append(
                {
                    "title": "Synthetic Company signs a supply agreement",
                    "url": "https://example.org/two",
                    "page_age": published_at,
                }
            )
        return 200, json.dumps({"type": "news", "results": results}).encode()

    adapter = BraveNewsAdapter(
        "synthetic-key",
        monthly_limit=2,
        reserve=repo.reserve_brave_news_request,
        transport=transport,
    )
    app = create_app(
        database,
        brave_api_key="synthetic-key",
        brave_monthly_limit=2,
        monitoring_adapters={"brave_news": adapter},
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={"name": "Synthetic Company"}).json()
    base = f"/api/projects/{project['id']}/monitoring"
    started = client.post(f"{base}/public-search", json={"provider": "brave"}).json()
    plan_id = started["plan"]["id"]

    assert app.state.monitoring_scheduler.process_one().status == "done"
    first = client.get(f"{base}/plans/{plan_id}/runs").json()[-1]
    assert (first["status"], first["item_count"], first["matched_count"], first["new_count"]) == (
        "completed",
        1,
        1,
        1,
    )

    queued = client.post(f"{base}/plans/{plan_id}/runs", json={"idempotency_key": "second-cycle"})
    assert queued.status_code == 202
    assert app.state.monitoring_scheduler.process_one().status == "done"
    second = client.get(f"{base}/plans/{plan_id}/runs").json()[-1]
    assert (
        second["status"],
        second["item_count"],
        second["matched_count"],
        second["new_count"],
    ) == (
        "completed",
        2,
        2,
        1,
    )
    assert len(client.get(f"{base}/signals").json()) == 2
    assert client.get(f"/api/projects/{project['id']}/summary").json()["confirmed_count"] == 0
    assert repo.brave_news_usage() == 2
    assert len(calls) == 2
