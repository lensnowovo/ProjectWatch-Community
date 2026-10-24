"""Read-only evidence projection for the relation tree; never schedules work."""
from datetime import UTC, datetime, timedelta


def _date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def evidence_signal(rows):
    """Deduplicate events; a repost/config edit must not refresh an old event."""
    clock = datetime.now(UTC)
    unique = {}
    for row in rows:
        key = row["id"]
        before = unique.get(key)
        date = _date(row.get("recorded_at"))
        previous = _date(before.get("recorded_at")) if before else None
        if before is None or (date is not None and (previous is None or date < previous)):
            unique[key] = row
    dated = [r for r in unique.values() if (d := _date(r.get("recorded_at"))) and d <= clock]
    newest = max(dated, key=lambda r: _date(r["recorded_at"]), default=None)
    return {"count": len(unique), "recorded_at": newest["recorded_at"] if newest else "",
            "fresh": bool(newest and _date(newest["recorded_at"]) >= clock - timedelta(days=7)),
            "title": newest.get("title", "") if newest else "",
            "url": newest.get("url", "") if newest else "",
            "basis": newest.get("basis", "") if newest else ""}


def tree_signals(plan, project_id, items):
    from project_watch.studio.questions import QuestionLedger

    project = plan.monitor.store.get_project(project_id) or {}
    with plan.monitor._db() as db:
        public = QuestionLedger.public_event_rows(db, project_id)
        clocks = {r["id"]: r["collected_at"] for r in db.execute(
            "SELECT id,collected_at FROM studio_items WHERE project_id=?", (project_id,))}
        routed = {}
        for row in db.execute("""SELECT h.watch_id,h.item_id FROM studio_watch_hits h
            JOIN studio_watchpoints w ON w.id=h.watch_id
            JOIN studio_items i ON i.id=h.item_id
            WHERE w.project_id=? AND i.project_id=?""", (project_id, project_id)):
            routed.setdefault(row["watch_id"], set()).add(row["item_id"])
        result = {}
        for item in items:
            receipts = []
            if item.get("scope_kind") in ("relation", "question") and item.get("visibility") == "public":
                for row in public:
                    if item["scope_kind"] == "question" and row["id"] not in routed.get(item["id"], set()):
                        continue
                    match = QuestionLedger.public_row_match(item, project, row)
                    if match:
                        receipts.append({"id": row["event_id"], "title": row["event_title"], "url": row["url"],
                                         "recorded_at": clocks.get(row["id"], ""), "basis": match["basis"]})
            else:
                # Legacy/internal items retain their own accepted hit semantics, not inferred graph roles.
                for hit in plan._focused(project_id, item, plan._hits(db, item["id"], limit=10000)):
                    receipts.append({"id": hit.get("event_id") or hit["id"], "title": hit["title"],
                                     "url": hit["url"], "recorded_at": clocks.get(hit["id"], ""), "basis": "accepted_hit"})
            result[item["id"]] = evidence_signal(receipts)
    return result
