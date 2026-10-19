"""Why one event matters: its source excerpts, the monitoring-plan points it hit, the question assessments
that used it, and the AI judgment points that cite it. Read-only; no provider calls."""
from __future__ import annotations

import json
from contextlib import closing

from project_watch.studio.monitoring_plan import AXIS_LABEL, RING_OF, RINGS, SECTION_OF
from project_watch.studio.questions import STATUSES

RING_LABEL = dict(RINGS)
# Judgment parts and which point lists count as supporting (+) or opposing (−) it.
JUDGMENT_PARTS = (
    ("operations", "公司本体", (("for", "+"), ("against", "−"))),
    ("position", "竞争位置", (("for", "+"), ("against", "−"))),
    ("chain", "产业链", (("for", "+"), ("against", "−"))),
    ("industry", "行业环境", (("drivers", "+"), ("headwinds", "−"))),
    ("technology", "技术路线", (("academic", "+"), ("industry", "+"), ("risks", "−"))),
)
EXCERPT_CHARS = 600


def _hits(db, event_id: str) -> list[str]:
    return [r[0] for r in db.execute(
        "SELECT DISTINCT h.watch_id FROM studio_watch_hits h JOIN studio_items i ON i.id=h.item_id WHERE i.event_id=?",
        (event_id,))]


def _assessments(db, project_id: str, event_id: str) -> list[dict]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_question_assessments'").fetchone():
        return []  # no question has been assessed in this database yet
    rows = db.execute(
        "SELECT watch_id, status, note, actor, created_at, event_ids FROM studio_question_assessments "
        "WHERE project_id=? AND event_ids LIKE ? ORDER BY created_at DESC, rowid DESC LIMIT 20",
        (project_id, f'%"{event_id}"%')).fetchall()
    out = []
    for r in rows:
        try:
            ids = json.loads(r["event_ids"] or "[]")
        except ValueError:
            continue
        if event_id in ids:  # LIKE only narrows; the stored list decides
            out.append({"watch_id": r["watch_id"], "status": r["status"], "note": r["note"], "actor": r["actor"],
                        "created_at": r["created_at"]})
    return out


def _excerpts(db, event_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT title, url, source, published_at, collected_at, snippet, summary FROM studio_items WHERE event_id=? "
        "ORDER BY COALESCE(published_at, collected_at)", (event_id,)).fetchall()
    return [{"title": r["title"], "url": r["url"], "source": r["source"], "date": (r["published_at"] or r["collected_at"] or "")[:10],
             "text": (r["snippet"] or r["summary"] or "")[:EXCERPT_CHARS]} for r in rows]


def _watch(w: dict, by_id: dict[str, dict]) -> dict:
    section = SECTION_OF.get(w.get("axis") or "", w.get("axis") or "")
    ring = RING_OF.get(section, "")
    parent = by_id.get(w.get("parent_id") or "")
    return {"id": w["id"], "text": w.get("text") or w.get("label") or "", "section": section,
            "section_label": AXIS_LABEL.get(section, ""), "ring": ring, "ring_label": RING_LABEL.get(ring, ""),
            "objects": w.get("objects") or [], "reason": w.get("reason") or "", "hop": str(w.get("hop") or "0"),
            "parent": (parent.get("label") or parent.get("text") or "") if parent else "", "status": w.get("status") or ""}


def _judgment_points(record: dict | None, urls: set[str], labels: dict[str, dict[str, str]]) -> list[dict]:
    """Points of the AI cross-analysis whose cited sources include this event's URLs."""
    analysis = (record or {}).get("analysis") or {}
    out = []
    for key, label, parts in JUDGMENT_PARTS:
        part = analysis.get(key) or {}
        verdict = part.get("verdict") or part.get("maturity") or ""
        for field, sign in parts:
            for point in part.get(field) or []:
                refs = point.get("refs") or []
                if any(isinstance(r, dict) and r.get("url") and r["url"] in urls for r in refs):
                    out.append({"part": key, "label": label, "verdict": verdict,
                                "verdict_label": labels.get("maturity" if key == "technology" else key, {}).get(verdict, ""),
                                "sign": sign, "text": point.get("text") or ""})
    return out


def build(store, monitor, project_id: str, event_id: str, record: dict | None, labels: dict[str, dict[str, str]]) -> dict | None:
    events = store.list_events(project_id=project_id, event_ids=[event_id], include_dismissed=True, limit=1)
    if not events:
        return None
    event = events[0]
    with closing(store._connect()) as db:
        hit_ids = _hits(db, event_id)
        assessments = _assessments(db, project_id, event_id)
        excerpts = _excerpts(db, event_id)
    watches = monitor.watchpoints(project_id)
    by_id = {w["id"]: w for w in watches}
    for a in assessments:
        w = by_id.get(a["watch_id"])
        a["question"] = (w.get("text") or w.get("label") or "") if w else ""
        a["status_label"] = STATUSES.get(a["status"], a["status"])
    urls = {x["url"] for x in excerpts if x["url"]}
    return {
        "event": {k: event.get(k) for k in ("id", "title", "summary", "impact", "category", "importance", "event_at", "created_at")},
        "excerpts": excerpts,
        "hits": [_watch(by_id[i], by_id) for i in hit_ids if i in by_id and by_id[i].get("status") != "retired"],
        "assessments": [a for a in assessments if a["question"]],
        "judgments": _judgment_points(record, urls, labels),
    }
