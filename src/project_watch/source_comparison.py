"""Read-only comparison of located extraction text; not a semantic fact diff."""

from __future__ import annotations

from collections.abc import Iterable

from project_watch.domain import DocumentFragment

MAX_LOCATIONS = 1000
MAX_VISIBLE_CHANGES = 100
MAX_EXCERPT_CHARS = 500


def _by_locator(fragments: Iterable[DocumentFragment]) -> dict[str, str]:
    located: dict[str, str] = {}
    for fragment in fragments:
        # A format adapter may yield several bounded fragments at one position.
        current = located.get(fragment.locator)
        located[fragment.locator] = (
            f"{current}\n{fragment.text}" if current is not None else fragment.text
        )
    return located


def compare_fragments(
    before: Iterable[DocumentFragment], after: Iterable[DocumentFragment]
) -> dict[str, object]:
    """Return bounded excerpts and explicit partial-result metadata."""
    old, new = _by_locator(before), _by_locator(after)
    locators = list(old) + [locator for locator in new if locator not in old]
    counts = {"added": 0, "changed": 0, "removed": 0}
    changes: list[dict[str, str | None]] = []
    excerpt_truncated = False
    for locator in locators[:MAX_LOCATIONS]:
        old_text, new_text = old.get(locator), new.get(locator)
        if old_text == new_text:
            continue
        kind = "added" if old_text is None else "removed" if new_text is None else "changed"
        counts[kind] += 1
        if len(changes) < MAX_VISIBLE_CHANGES:
            excerpt_truncated |= any(
                text is not None and len(text) > MAX_EXCERPT_CHARS for text in (old_text, new_text)
            )
            changes.append(
                {
                    "locator": locator,
                    "kind": kind,
                    "before": old_text[:MAX_EXCERPT_CHARS] if old_text is not None else None,
                    "after": new_text[:MAX_EXCERPT_CHARS] if new_text is not None else None,
                }
            )
    return {
        "counts": counts,
        "changes": changes,
        "total_locations": len(locators),
        "compared_locations": min(len(locators), MAX_LOCATIONS),
        "truncated": len(locators) > MAX_LOCATIONS or sum(counts.values()) > len(changes),
        "excerpt_truncated": excerpt_truncated,
    }
