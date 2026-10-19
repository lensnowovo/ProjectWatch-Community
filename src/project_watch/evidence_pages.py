"""Bounded cursor pages over immutable, located extracted text."""

from __future__ import annotations

import re
from collections.abc import Sequence

from project_watch.domain import DocumentFragment

MAX_PAGE_EXCERPTS = 8
MAX_EXCERPT_CHARS = 1500
_CURSOR = re.compile(r"^([0-9a-f]{64}):([1-9][0-9]*):([0-9]+)$")


def page_excerpts(
    fragments: Sequence[DocumentFragment],
    *,
    source_sha256: str,
    cursor: str | None = None,
    limit: int = MAX_PAGE_EXCERPTS,
) -> dict[str, object]:
    """Return up to `limit` chunks, never silently dropping a fragment tail."""
    if not 1 <= limit <= MAX_PAGE_EXCERPTS:
        raise ValueError("limit must be between 1 and 8")
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("invalid source hash")
    ordered = sorted(fragments, key=lambda item: item.ordinal)
    if len({fragment.ordinal for fragment in ordered}) != len(ordered) or any(
        fragment.ordinal < 1 or not fragment.text for fragment in ordered
    ):
        raise ValueError("invalid extracted fragments")

    index, offset = 0, 0
    if cursor is not None:
        match = _CURSOR.fullmatch(cursor) if len(cursor) <= 96 else None
        if match is None or match.group(1) != source_sha256:
            raise ValueError("invalid source cursor")
        ordinal, offset = int(match.group(2)), int(match.group(3))
        index = next((i for i, item in enumerate(ordered) if item.ordinal == ordinal), -1)
        if index < 0 or offset >= len(ordered[index].text):
            raise ValueError("invalid source cursor")

    excerpts: list[dict[str, object]] = []
    while index < len(ordered) and len(excerpts) < limit:
        fragment = ordered[index]
        end = min(offset + MAX_EXCERPT_CHARS, len(fragment.text))
        excerpts.append(
            {
                "locator": fragment.locator,
                "ordinal": fragment.ordinal,
                "start_char": offset,
                "end_char": end,
                "text": fragment.text[offset:end],
            }
        )
        if end < len(fragment.text):
            offset = end
        else:
            index += 1
            offset = 0

    next_cursor = (
        f"{source_sha256}:{ordered[index].ordinal}:{offset}" if index < len(ordered) else None
    )
    return {
        "excerpts": excerpts,
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
    }
