"""A scoped executor can page through all extracted text without a bulk dump."""

import pytest

from project_watch.domain import DocumentFragment
from project_watch.evidence_pages import page_excerpts

HASH = "a" * 64


def fragment(ordinal: int, text: str) -> DocumentFragment:
    return DocumentFragment(
        id=f"fragment-{ordinal}",
        project_id="synthetic-project",
        source_id="synthetic-source",
        job_id="synthetic-job",
        ordinal=ordinal,
        locator=f"page {ordinal}",
        text=text,
    )


def test_empty_source_has_no_next_page() -> None:
    result = page_excerpts([], source_sha256=HASH)

    assert result == {"excerpts": [], "next_cursor": None, "truncated": False}


def test_short_source_has_exact_location_and_no_next_page() -> None:
    result = page_excerpts([fragment(1, "short")], source_sha256=HASH)

    assert result == {
        "excerpts": [
            {
                "locator": "page 1",
                "ordinal": 1,
                "start_char": 0,
                "end_char": 5,
                "text": "short",
            }
        ],
        "next_cursor": None,
        "truncated": False,
    }


def test_long_fragment_and_later_fragments_reconstruct_without_missing_text() -> None:
    texts = ["A" * 1500, "B" * 3101, "C" * 20]
    fragments = [fragment(index, text) for index, text in enumerate(texts, start=1)]
    cursor = None
    chunks = []
    seen = set()
    while True:
        page = page_excerpts(fragments, source_sha256=HASH, cursor=cursor, limit=2)
        chunks.extend(page["excerpts"])
        cursor = page["next_cursor"]
        assert page["truncated"] == (cursor is not None)
        if cursor is None:
            break
        assert cursor not in seen
        seen.add(cursor)

    assert len(chunks) == 5
    for ordinal, expected in enumerate(texts, start=1):
        rows = [row for row in chunks if row["ordinal"] == ordinal]
        assert "".join(row["text"] for row in rows) == expected
        assert rows[0]["start_char"] == 0
        assert rows[-1]["end_char"] == len(expected)
    assert len(chunks[0]["text"]) == 1500
    assert chunks[1]["locator"] == "page 2"


def test_fragment_count_is_bounded_per_call() -> None:
    fragments = [fragment(index, str(index)) for index in range(1, 12)]

    first = page_excerpts(fragments, source_sha256=HASH)
    second = page_excerpts(fragments, source_sha256=HASH, cursor=first["next_cursor"])

    assert len(first["excerpts"]) == 8
    assert [item["ordinal"] for item in second["excerpts"]] == [9, 10, 11]
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "0:0",
        "1:0",
        "other:1:0",
        "b" * 64 + ":1:0",
        HASH + ":0:0",
        HASH + ":2:0",
        HASH + ":1:4",
        HASH + ":1:-1",
    ],
)
def test_invalid_or_foreign_version_cursor_fails(cursor: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        page_excerpts([fragment(1, "abc")], source_sha256=HASH, cursor=cursor)


@pytest.mark.parametrize("limit", [0, 9])
def test_limit_remains_bounded(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        page_excerpts([fragment(1, "abc")], source_sha256=HASH, limit=limit)


def test_invalid_extracted_fragment_is_rejected() -> None:
    with pytest.raises(ValueError, match="fragments"):
        page_excerpts([fragment(0, "abc")], source_sha256=HASH)
