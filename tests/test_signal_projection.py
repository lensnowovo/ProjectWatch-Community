"""Public lead deduplication must not erase raw source evidence."""

from datetime import UTC, datetime, timedelta

from project_watch.domain import Signal
from project_watch.signal_projection import deduplicate_project_signals

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def signal(
    id: str,
    plan_id: str,
    url: str,
    *,
    project_id: str = "project-1",
    content_hash: str = "a" * 64,
    seen_at: datetime = NOW,
    title: str = "Synthetic Company announcement",
) -> Signal:
    return Signal(
        id=id,
        project_id=project_id,
        plan_id=plan_id,
        run_id=f"run-{id}",
        external_id=id,
        title=title,
        url=url,
        summary="Unverified public lead",
        published_at=NOW,
        first_seen_at=seen_at,
        content_sha256=content_hash,
        freshness="current",
        status="candidate",
    )


def test_same_article_across_sources_is_one_visible_lead() -> None:
    rows = [
        signal("new", "source-b", "https://NEWS.example.org/story?id=7&utm_source=feed#top"),
        signal(
            "old",
            "source-a",
            "https://news.example.org/story?id=7&utm_medium=rss",
            seen_at=NOW - timedelta(hours=1),
        ),
    ]

    assert [row.id for row in deduplicate_project_signals(rows)] == ["new"]
    assert len(rows) == 2  # The raw source/run records remain available for audit.


def test_distinct_article_query_and_project_are_not_merged() -> None:
    rows = [
        signal("one", "source-a", "https://news.example.org/story?id=7"),
        signal("another-article", "source-b", "https://news.example.org/story?id=8"),
        signal(
            "different-headline",
            "source-d",
            "https://news.example.org/story?id=7",
            title="Synthetic Company unrelated update",
        ),
        signal(
            "another-project",
            "source-c",
            "https://news.example.org/story?id=7",
            project_id="project-2",
        ),
    ]

    assert [row.id for row in deduplicate_project_signals(rows)] == [
        "one",
        "another-article",
        "different-headline",
        "another-project",
    ]


def test_revised_article_in_same_source_stays_visible_for_review() -> None:
    rows = [
        signal("revised", "source-a", "https://news.example.org/story/7", content_hash="b" * 64),
        signal("original", "source-a", "https://news.example.org/story/7"),
        signal("syndicated", "source-b", "https://news.example.org/story/7"),
    ]

    assert [row.id for row in deduplicate_project_signals(rows)] == ["revised", "original"]
