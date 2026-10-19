"""Project-scoped display projection for unverified public leads.

Raw signals and source-run receipts are intentionally not changed by this module.
Only a shared article URL and headline can collapse cross-source sightings;
matching titles alone can describe different business events, while some feeds
link distinct items to the same generic page.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from project_watch.domain import Signal

_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def _article_url_key(url: str) -> str:
    parsed = urlsplit(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    kept = [
        (key, value)
        for key, value in params
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMETERS
    ]
    query = parsed.query if len(kept) == len(params) else urlencode(kept)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, ""))


def deduplicate_project_signals(signals: list[Signal]) -> list[Signal]:
    """Keep input order and same-source revisions; collapse cross-source article sightings.

    The caller supplies newest-first records. Deduplication is scoped by project so
    a shared public URL never hides a candidate in another project's timeline.
    """
    visible: list[Signal] = []
    owners: dict[tuple[str, str, str], str] = {}
    seen_revisions: set[tuple[str, str, str, str, str]] = set()
    for signal in signals:
        headline = " ".join(signal.title.split()).casefold()
        key = (signal.project_id, _article_url_key(signal.url), headline)
        owner = owners.setdefault(key, signal.plan_id)
        revision = (*key, signal.plan_id, signal.content_sha256)
        if owner != signal.plan_id or revision in seen_revisions:
            continue
        visible.append(signal)
        seen_revisions.add(revision)
    return visible
