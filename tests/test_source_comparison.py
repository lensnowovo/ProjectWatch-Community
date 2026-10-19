"""Located, bounded text comparisons are review aids, not fact updates."""

from project_watch.domain import DocumentFragment
from project_watch.source_comparison import compare_fragments


def fragment(locator: str, text: str) -> DocumentFragment:
    return DocumentFragment(
        id=locator,
        project_id="synthetic-project",
        source_id="synthetic-source",
        job_id="synthetic-job",
        ordinal=1,
        locator=locator,
        text=text,
    )


def test_compare_located_changes_without_promoting_facts() -> None:
    result = compare_fragments(
        [fragment("page 1", "same"), fragment("page 2", "old"), fragment("page 3", "gone")],
        [fragment("page 1", "same"), fragment("page 2", "new"), fragment("page 4", "added")],
    )

    assert result["counts"] == {"added": 1, "changed": 1, "removed": 1}
    assert [(item["locator"], item["kind"]) for item in result["changes"]] == [
        ("page 2", "changed"),
        ("page 3", "removed"),
        ("page 4", "added"),
    ]
    assert result["changes"][0]["before"] == "old"
    assert result["changes"][0]["after"] == "new"
    assert result["truncated"] is False


def test_comparison_bounds_excerpts_and_visible_changes() -> None:
    old = [fragment(f"line {i:04}", "old " + "x" * 2000) for i in range(120)]
    new = [fragment(f"line {i:04}", "new " + "y" * 2000) for i in range(120)]

    result = compare_fragments(old, new)

    assert result["counts"]["changed"] == 120
    assert len(result["changes"]) == 100
    assert result["truncated"] is True
    assert result["excerpt_truncated"] is True
    assert len(result["changes"][0]["before"]) <= 500
    assert len(result["changes"][0]["after"]) <= 500


def test_comparison_does_not_call_a_rewriter_for_no_changes() -> None:
    result = compare_fragments([fragment("line 1", "same")], [fragment("line 1", "same")])

    assert result["counts"] == {"added": 0, "changed": 0, "removed": 0}
    assert result["changes"] == []
    assert result["truncated"] is False
    assert result["excerpt_truncated"] is False


def test_comparison_discloses_uncompared_locations() -> None:
    before = [fragment(f"line {index:04}", "same") for index in range(1001)]
    after = [*before[:-1], fragment("line 1000", "changed beyond comparison limit")]

    result = compare_fragments(before, after)

    assert result["counts"] == {"added": 0, "changed": 0, "removed": 0}
    assert result["compared_locations"] == 1000
    assert result["total_locations"] == 1001
    assert result["truncated"] is True
