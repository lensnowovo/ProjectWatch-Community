import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from project_watch.monitoring import (
    AdapterResult,
    FeedItem,
    MonitoringRunner,
    select_project_items,
)
from project_watch.repository import ProjectRepository


class StaticAdapter:
    def __init__(self, result: AdapterResult) -> None:
        self.result = result
        self.calls = 0

    def fetch(self, target: str) -> AdapterResult:
        assert target == "https://example.org/feed.xml"
        self.calls += 1
        return self.result


def setup(tmp_path: Path) -> tuple[ProjectRepository, str]:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Company")
    plan = repository.create_monitoring_plan(
        project.id, kind="rss", target="https://example.org/feed.xml", cadence_minutes=60
    )
    return repository, plan.id


def item(published_at: datetime) -> FeedItem:
    return FeedItem(
        external_id="announcement-1",
        title="Synthetic Company product announcement",
        url="https://example.org/news/1",
        summary="This is a test item, not a business fact.",
        published_at=published_at,
    )


def test_project_name_screen_is_conservative_and_preserves_feed_order() -> None:
    now = datetime.now(UTC)
    items = (
        FeedItem("1", "Alphabet update", "https://example.org/1", "not the project", now),
        FeedItem("2", "Alpha raises funding", "https://example.org/2", "", now),
        FeedItem("3", "Other", "https://example.org/3", "天籁通讯完成交付", now),
        FeedItem("4", "Unrelated", "https://example.org/4", "", now),
    )
    assert [row.external_id for row in select_project_items(items, "Alpha")] == ["2"]
    assert [row.external_id for row in select_project_items(items, "天籁通讯")] == ["3"]
    assert select_project_items(items, "A") == ()


def test_reachable_feed_with_unrelated_items_has_zero_matches(tmp_path: Path) -> None:
    repository, plan_id = setup(tmp_path)
    unrelated = FeedItem(
        external_id="other",
        title="Another company's news",
        url="https://example.org/other",
        summary="No project mention",
        published_at=datetime.now(UTC),
    )
    receipt = MonitoringRunner(
        repository, {"rss": StaticAdapter(AdapterResult(items=(unrelated,)))}
    ).run(plan_id, idempotency_key="unrelated")

    assert receipt.status == "completed"
    assert receipt.item_count == 1
    assert receipt.matched_count == 0
    assert receipt.new_count == 0
    assert repository.list_signals(repository.get_monitoring_plan(plan_id).project_id) == []


def test_existing_sqlite_receipts_keep_historical_counts_on_upgrade(tmp_path: Path) -> None:
    repository, plan_id = setup(tmp_path)
    repository.record_source_run(
        plan_id,
        status="completed",
        error_code=None,
        signals=(),
        fetched_count=3,
    )
    database = tmp_path / "watch.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE source_runs DROP COLUMN matched_count")

    reopened = ProjectRepository(database)
    old = reopened.list_source_runs(plan_id)[0]
    assert old.item_count == 3
    assert old.matched_count is None


def test_new_item_is_candidate_signal_and_rerun_is_not_new(tmp_path: Path) -> None:
    repository, plan_id = setup(tmp_path)
    adapter = StaticAdapter(AdapterResult(items=(item(datetime.now(UTC)),)))
    runner = MonitoringRunner(repository, {"rss": adapter})

    first = runner.run(plan_id, idempotency_key="run-1")
    repeated_request = runner.run(plan_id, idempotency_key="run-1")
    second_scan = runner.run(plan_id, idempotency_key="run-2")

    assert first.status == "completed" and first.item_count == 1 and first.new_count == 1
    assert repeated_request.id == first.id
    assert second_scan.status == "completed" and second_scan.new_count == 0
    assert adapter.calls == 2
    signals = repository.list_signals(repository.get_monitoring_plan(plan_id).project_id)
    assert len(signals) == 1
    assert signals[0].freshness == "current"
    assert signals[0].status == "candidate"
    assert signals[0].run_id == first.id
    assert (
        ProjectRepository(tmp_path / "watch.sqlite3").list_signals(signals[0].project_id) == signals
    )


def test_empty_and_unreachable_are_distinct(tmp_path: Path) -> None:
    repository, plan_id = setup(tmp_path)
    empty = MonitoringRunner(repository, {"rss": StaticAdapter(AdapterResult(items=()))}).run(
        plan_id, idempotency_key="empty"
    )
    unavailable = MonitoringRunner(
        repository, {"rss": StaticAdapter(AdapterResult(unavailable_error="timeout"))}
    ).run(plan_id, idempotency_key="unavailable")

    assert empty.status == "no_results" and empty.error_code is None
    assert unavailable.status == "unreachable" and unavailable.error_code == "timeout"
    assert repository.list_signals(repository.get_monitoring_plan(plan_id).project_id) == []


def test_rate_limited_is_distinct_from_unreachable_and_no_results(tmp_path: Path) -> None:
    repository, plan_id = setup(tmp_path)
    run = MonitoringRunner(
        repository, {"rss": StaticAdapter(AdapterResult(unavailable_error="rate_limited"))}
    ).run(plan_id, idempotency_key="rate-limited")

    assert run.status == "rate_limited"
    assert run.error_code == "rate_limited"
    assert run.item_count == 0
    assert repository.get_monitoring_plan(plan_id).last_run_status == "rate_limited"


def test_old_dated_first_seen_item_is_historical_not_new_change(tmp_path: Path) -> None:
    repository, plan_id = setup(tmp_path)
    old = item(datetime.now(UTC) - timedelta(days=400))

    receipt = MonitoringRunner(repository, {"rss": StaticAdapter(AdapterResult(items=(old,)))}).run(
        plan_id, idempotency_key="old"
    )

    assert receipt.status == "completed" and receipt.item_count == 1
    assert receipt.new_count == 0
    signal = repository.list_signals(repository.get_monitoring_plan(plan_id).project_id)[0]
    assert signal.freshness == "historical"


def test_duplicate_items_in_one_feed_are_atomic_and_deduplicated(tmp_path: Path) -> None:
    repository, plan_id = setup(tmp_path)
    now = datetime.now(UTC)
    adapter = StaticAdapter(AdapterResult(items=(item(now), item(now))))

    receipt = MonitoringRunner(repository, {"rss": adapter}).run(
        plan_id, idempotency_key="duplicates"
    )

    assert receipt.item_count == 2 and receipt.new_count == 1
    assert len(repository.list_signals(repository.get_monitoring_plan(plan_id).project_id)) == 1


def test_updated_content_of_known_item_is_a_separate_candidate(tmp_path: Path) -> None:
    repository, plan_id = setup(tmp_path)
    original = item(datetime.now(UTC))
    revised = FeedItem(
        external_id=original.external_id,
        title=original.title,
        url=original.url,
        summary="Revised test wording.",
        published_at=original.published_at,
    )

    MonitoringRunner(repository, {"rss": StaticAdapter(AdapterResult(items=(original,)))}).run(
        plan_id, idempotency_key="original"
    )
    update = MonitoringRunner(
        repository, {"rss": StaticAdapter(AdapterResult(items=(revised,)))}
    ).run(plan_id, idempotency_key="revision")

    assert update.new_count == 1
    signals = repository.list_signals(repository.get_monitoring_plan(plan_id).project_id)
    assert len(signals) == 2
    assert all(signal.status == "candidate" for signal in signals)
