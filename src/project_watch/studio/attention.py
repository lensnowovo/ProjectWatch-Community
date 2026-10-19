"""Portfolio view of what needs a look: open cross-checks, silence signals, watch-point hits and
notable official-site changes, across every monitored project. Rules only — no model calls.

Each entry says which project, why, and which tab of that project holds the evidence, so the
portfolio pages can link straight to it.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from pydantic import BaseModel, Field

from project_watch.studio import signals as sig

# Ordering inside the list: how urgent a kind of signal is by default.
LEVEL_RANK = {"high": 0, "watch": 1, "medium": 1, "info": 2, "low": 2}
KIND_LABEL = {"crosscheck": "内外对照", "silence": "沉默信号", "watch": "关注点命中", "site": "官网变化"}
KIND_TAB = {"crosscheck": "overview", "silence": "overview", "watch": "external", "site": "documents"}
WEIGHT = {"high": 3, "watch": 2, "medium": 2, "info": 1, "low": 1}


def _recent_watch_hits(monitor, project_id: str, days: int) -> list[dict]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with monitor._db() as db:
        rows = db.execute("""SELECT w.label, i.title, i.summary, i.url, i.event_id, h.created_at FROM studio_watch_hits h
                             JOIN studio_watchpoints w ON w.id=h.watch_id JOIN studio_items i ON i.id=h.item_id
                             LEFT JOIN studio_events e ON e.id=i.event_id
                             WHERE w.project_id=? AND h.created_at >= ? AND i.relevant=1
                             AND i.status!='dismissed' AND COALESCE(e.status,'new')!='dismissed'
                             ORDER BY h.created_at DESC""", (project_id, since)).fetchall()
    return [dict(r) for r in rows]


def project_attention(store, monitor, signals, project_id: str, documents: list[dict] | None, *, days: int = 7, base_hours=24) -> list[dict]:
    """Entries for one project, most urgent first."""
    out: list[dict] = []
    for c in signals.crosschecks(project_id):
        out.append({"kind": "crosscheck", "level": "info" if c["severity"] == "unrated" else c["severity"], "title": f"{c['kind_label']}：{c['topic']}",
                    "detail": f"公开：{c['public_says']}；内部：{c['internal_says']}", "question": c.get("question") or "",
                    "at": c["updated_at"], "ref": c["id"]})
    for s in sig.silence(store, monitor, project_id, documents, base_hours=base_hours):
        out.append({"kind": "silence", "level": s["level"], "title": s["title"], "detail": s["detail"], "at": None,
                    "ref": s["kind"]})
    seen: set[str] = set()
    for h in _recent_watch_hits(monitor, project_id, days):
        key = h["event_id"] or h["url"]
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": "watch", "level": "watch", "title": f"命中关注点“{h['label']}”",
                    "detail": h["summary"] or h["title"], "url": h["url"], "at": h["created_at"], "ref": h["event_id"]})
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    for c in monitor.changes(project_id, 20):
        if c["detected_at"] >= since and c.get("importance") in ("major", "notable"):
            level = "high" if c["importance"] == "major" else "info"
            head = (c["added"] or c["removed"] or [""])[0]
            out.append({"kind": "site", "level": level, "title": f"官网“{c['label']}”有变化",
                        "detail": c.get("impact") or head, "url": c["url"], "at": c["detected_at"], "ref": c["id"]})
    out.sort(key=lambda e: (LEVEL_RANK.get(e["level"], 2), -(datetime.fromisoformat(e["at"]).timestamp() if e.get("at") else 0)))
    return out


def portfolio(store, monitor, signals, documents_for, *, days: int = 7, base_hours=24) -> dict:
    """All active projects with at least one entry, most in need of attention first."""
    projects, totals = [], {k: 0 for k in KIND_LABEL}
    for p in store.list_projects():
        if p.get("status") == "paused" or not p.get("activated", True):
            continue
        entries = project_attention(store, monitor, signals, p["id"], documents_for(p["id"]), days=days, base_hours=base_hours)
        if not entries:
            continue
        for e in entries:
            totals[e["kind"]] += 1
            e["tab"] = KIND_TAB[e["kind"]]
        projects.append({"id": p["id"], "name": p.get("short_name") or p["name"],
                         "score": sum(WEIGHT.get(e["level"], 1) for e in entries),
                         "counts": {k: sum(1 for e in entries if e["kind"] == k) for k in KIND_LABEL},
                         "entries": entries})
    projects.sort(key=lambda x: (-x["score"], x["name"]))
    return {"days": days, "projects": projects, "totals": totals, "labels": KIND_LABEL}


def markdown(data: dict, *, compact: bool = False) -> str:
    """A section for the weekly digest. Empty string when nothing needs a look."""
    if not data["projects"]:
        return ""
    lines = ["## 需要核对与异常信号", ""]
    for p in data["projects"][: 5 if compact else None]:
        entries = p["entries"][: 2 if compact else 6]
        name = " ".join(p["name"].split())[:40] if compact else p["name"]
        lines.append(f"- **{name}**")
        for e in entries:
            title = " ".join(e["title"].split())[:60] if compact else e["title"]
            lines.append(f"  - {KIND_LABEL[e['kind']]} · {title}" + ("" if compact else f"：{e['detail']}"))
            if e.get("question") and not compact:
                lines.append(f"    - 可以问：{e['question']}")
    if compact and len(data["projects"]) > 5:
        lines.append(f"另有 {len(data['projects']) - 5} 个项目需要查看，完整列表请在 ProjectWatch 中查看。")
    lines.append("")
    return "\n".join(lines)


def documents_lookup(pipeline):
    """project_id → its uploaded documents (only creation time is needed for silence signals)."""
    def documents_for(project_id: str) -> list[dict]:
        return [{"created_at": s.created_at.isoformat()} for s in pipeline.repository.list_sources(project_id)
                if s.kind == "document"]
    return documents_for


def for_pipeline(pipeline, days: int = 7) -> dict:
    return portfolio(pipeline.store, pipeline.monitor, pipeline.signals, documents_lookup(pipeline), days=days,
                     base_hours=pipeline.settings_store.load().refresh_hours)


class ReadAll(BaseModel):
    project_id: str | None = None
    days: int | None = Field(default=None, ge=1, le=365)


def mark_all_read(store, project_id: str | None = None, days: int | None = None) -> int:
    """New → read for the events currently in view (optionally one project / a time window). Starred stay starred."""
    where, args = ["status='new'"], []
    if project_id:
        where.append("project_id=?")
        args.append(project_id)
    if days:
        where.append("COALESCE(event_at, created_at) >= ?")
        args.append((datetime.now(UTC) - timedelta(days=days)).isoformat())
    with closing(store._connect()) as db, db:
        return db.execute(f"UPDATE studio_events SET status='read' WHERE {' AND '.join(where)}", args).rowcount


def register_attention_routes(app: FastAPI, pipeline) -> None:
    @app.get("/api/studio/attention")
    def attention(days: int = 7):
        return for_pipeline(pipeline, max(1, min(int(days), 30)))

    @app.post("/api/studio/events/read-all")
    def read_all(body: ReadAll):
        return {"updated": mark_all_read(pipeline.store, body.project_id, body.days)}
