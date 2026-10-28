"""SQLite persistence for Studio (shares the project database file)."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from project_watch.studio.normalize import normalize_metric

# Publication dates, not acquisition time, prioritize limited news reading.
# SQLite normalizes impossible calendar days with '+0 days'; a changed value
# remains unknown. Undated/future reports are retained between recent and old.
_NEWS_DAY = """CASE WHEN date(substr(published_at,1,10),'+0 days')=substr(published_at,1,10)
               AND length(substr(published_at,1,10))=10
               THEN date(substr(published_at,1,10)) END"""
_NEWS_ORDER = f"""CASE WHEN ({_NEWS_DAY}) BETWEEN date('now','-180 days') AND date('now') THEN 0
                     WHEN ({_NEWS_DAY}) IS NULL OR ({_NEWS_DAY})>date('now') THEN 1 ELSE 2 END,
                  CASE WHEN ({_NEWS_DAY})<=date('now') THEN ({_NEWS_DAY}) END DESC,
                  collected_at DESC,rowid DESC"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_source_permissions (
    source_id TEXT PRIMARY KEY REFERENCES sources(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    model_binding TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_document_snapshot_receipts (
    source_id TEXT PRIMARY KEY REFERENCES sources(id), intake_updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_projects (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    legal_name TEXT NOT NULL DEFAULT '',
    short_name TEXT NOT NULL DEFAULT '',
    credit_code TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    founded TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '[]',
    entity_source TEXT NOT NULL DEFAULT 'manual',
    stage TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    consent_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_internal_at TEXT,
    last_external_at TEXT
);
CREATE TABLE IF NOT EXISTS studio_profile (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_public_profile (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_landscape (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_brief (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_plan (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_metrics (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    source_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'financial',
    period TEXT NOT NULL,
    value REAL,
    unit TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL DEFAULT 'unknown',
    scope TEXT NOT NULL DEFAULT '',
    locator TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS studio_metrics_project ON studio_metrics(project_id, name, period);
CREATE TABLE IF NOT EXISTS studio_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    key TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    collected_at TEXT NOT NULL,
    triaged INTEGER NOT NULL DEFAULT 0,
    relevant INTEGER NOT NULL DEFAULT 1,
    importance TEXT NOT NULL DEFAULT 'routine',
    summary TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    extra TEXT NOT NULL DEFAULT '{}',
    UNIQUE (project_id, key)
);
CREATE INDEX IF NOT EXISTS studio_items_feed ON studio_items(relevant, published_at);
CREATE TABLE IF NOT EXISTS studio_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    importance TEXT NOT NULL DEFAULT 'routine',
    event_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS studio_events_feed ON studio_events(project_id, event_at);
CREATE TABLE IF NOT EXISTS studio_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_doc_changes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    source_ids TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS studio_doc_changes_project ON studio_doc_changes(project_id, created_at);
CREATE TABLE IF NOT EXISTS studio_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS studio_jobs_queue ON studio_jobs(status, created_at);
CREATE TABLE IF NOT EXISTS studio_analyzed_sources (
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    analyzed_at TEXT NOT NULL,
    PRIMARY KEY (project_id, source_id)
);
CREATE TABLE IF NOT EXISTS studio_usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);
"""

JOB_KINDS = ("internal", "external", "external_followup", "brief", "public_profile", "landscape", "business", "competition", "details", "commercial", "insight")
LANDSCAPE_DAYS = 7  # competition / industry pages are regenerated weekly
IMPORTANCE = ("major", "notable", "routine")
ITEM_STATUS = ("new", "read", "starred", "dismissed")


def now() -> str:
    return datetime.now(UTC).isoformat()


UNREAD_DAYS = 14


class FollowupSuperseded(ValueError):
    """The failed follow-up's inputs changed; recover with a fresh round, never by resuming it."""


class StudioStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._write_context = threading.local()
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)
            project_columns = {r[1] for r in db.execute("PRAGMA table_info(studio_projects)")}
            if 'execution_mode' not in project_columns:
                db.execute("ALTER TABLE studio_projects ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'api'")
            if "config_version" not in project_columns:
                db.execute("ALTER TABLE studio_projects ADD COLUMN config_version INTEGER NOT NULL DEFAULT 1")
            # Both legacy web mutations and application/Agent writes advance
            # the same configuration version. Monitoring timestamps do not.
            db.executescript("""
                CREATE TRIGGER IF NOT EXISTS studio_config_version
                AFTER UPDATE OF legal_name,short_name,credit_code,website,founded,location,
                    industry,description,aliases,entity_source,stage,note,consent_at,status
                ON studio_projects
                WHEN OLD.legal_name IS NOT NEW.legal_name OR OLD.short_name IS NOT NEW.short_name
                    OR OLD.credit_code IS NOT NEW.credit_code OR OLD.website IS NOT NEW.website
                    OR OLD.founded IS NOT NEW.founded OR OLD.location IS NOT NEW.location
                    OR OLD.industry IS NOT NEW.industry OR OLD.description IS NOT NEW.description
                    OR OLD.aliases IS NOT NEW.aliases OR OLD.entity_source IS NOT NEW.entity_source
                    OR OLD.stage IS NOT NEW.stage OR OLD.note IS NOT NEW.note
                    OR OLD.consent_at IS NOT NEW.consent_at OR OLD.status IS NOT NEW.status
                BEGIN
                    UPDATE studio_projects SET config_version=OLD.config_version+1
                    WHERE project_id=NEW.project_id;
                END;
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(studio_items)")}
            for name, ddl in (("body", "TEXT NOT NULL DEFAULT ''"),
                              ("body_status", "TEXT NOT NULL DEFAULT ''"),
                              ("event_id", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE studio_items ADD COLUMN {name} {ddl}")
            db.execute("CREATE INDEX IF NOT EXISTS studio_items_event ON studio_items(event_id)")
            metric_columns = {r[1] for r in db.execute("PRAGMA table_info(studio_metrics)")}
            for name, ddl in (("std_name", "TEXT NOT NULL DEFAULT ''"), ("period_key", "TEXT NOT NULL DEFAULT ''"),
                              ("value_norm", "REAL"), ("norm_unit", "TEXT NOT NULL DEFAULT ''"),
                              ("currency", "TEXT NOT NULL DEFAULT ''")):
                if name not in metric_columns:
                    db.execute(f"ALTER TABLE studio_metrics ADD COLUMN {name} {ddl}")
            if "event_id" not in columns:
                self._migrate_events(db)
            event_columns = {r[1] for r in db.execute("PRAGMA table_info(studio_events)")}
            if "notified_at" not in event_columns:
                db.execute("ALTER TABLE studio_events ADD COLUMN notified_at TEXT")
                # Events that existed before push was available are never pushed retroactively.
                db.execute("UPDATE studio_events SET notified_at=?", (now(),))
            job_columns = {r[1] for r in db.execute("PRAGMA table_info(studio_jobs)")}
            for name, ddl in (("lease_token", "TEXT NOT NULL DEFAULT ''"), ("lease_until", "TEXT"),
                              ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("retry_after", "TEXT"),
                              ("input_version", "TEXT NOT NULL DEFAULT ''"),
                              ("progress_at", "TEXT"), ("heartbeat_at", "TEXT"), ("stalled_at", "TEXT"),
                              ("yield_count", "INTEGER NOT NULL DEFAULT 0"), ("queue_at", "TEXT")):
                if name not in job_columns:
                    db.execute(f"ALTER TABLE studio_jobs ADD COLUMN {name} {ddl}")
            from project_watch.studio.execution import SCHEMA as EXECUTION_SCHEMA

            db.executescript(EXECUTION_SCHEMA)
            from project_watch.studio.recovery import SCHEMA as RECOVERY_SCHEMA

            db.executescript(RECOVERY_SCHEMA)
            # Additive accounting migration; old daily aggregates stay labelled
            # legacy rather than being counted twice with their old HTTP rows.
            call_columns = {r[1] for r in db.execute('PRAGMA table_info(studio_job_calls)')}
            for name, ddl in (("usage_version", "INTEGER NOT NULL DEFAULT 0"),
                              ("input_tokens", "INTEGER"), ("output_tokens", "INTEGER"),
                              ("cache_read_tokens", "INTEGER"), ("cache_creation_tokens", "INTEGER"),
                              ("elapsed_ms", "INTEGER"), ("finished_at", "TEXT"),
                              ("request_limit", "INTEGER"), ("deadline_at", "TEXT")):
                if name not in call_columns:
                    db.execute(f'ALTER TABLE studio_job_calls ADD COLUMN {name} {ddl}')
            db.executescript('''CREATE TABLE IF NOT EXISTS studio_direct_model_calls (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, status TEXT NOT NULL,
                response_status INTEGER, created_at TEXT NOT NULL, finished_at TEXT,
                input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER, elapsed_ms INTEGER
            );''')
            from project_watch.studio.query_ledger import SCHEMA as QUERY_SCHEMA

            db.executescript(QUERY_SCHEMA)
            from project_watch.studio.direct_operations import SCHEMA as DIRECT_SCHEMA

            db.executescript(DIRECT_SCHEMA)
            # Opening a store (including read-only adapters) must not steal a
            # live worker. Recovery happens under claim's write transaction.
        from project_watch.studio.evidence_store import EvidenceStore

        self.evidence = EvidenceStore(self)

    def _connect(self) -> sqlite3.Connection:
        from project_watch.studio.write_fencing import FencedConnection

        db = sqlite3.connect(self.db_path, timeout=10, factory=FencedConnection)
        db.guard = getattr(self._write_context, 'guard', None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 10000")
        return db

    @contextmanager
    def write_scope(self, guard):
        previous = getattr(self._write_context, 'guard', None)
        self._write_context.guard = guard
        try:
            yield
        finally:
            self._write_context.guard = previous

    @staticmethod
    def _migrate_events(db: sqlite3.Connection) -> None:
        """Keep legacy triaged reports visible immediately, without model/network calls."""
        from project_watch.studio import events as ev

        rows = db.execute("SELECT * FROM studio_items WHERE triaged=1 AND relevant=1").fetchall()
        partitions: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            item = dict(row)
            partitions.setdefault((item["project_id"], item["status"]), []).append(item)
        for (project_id, status), items in partitions.items():
            groups, _ = ev.plan_groups(items, [])
            for group in groups:
                StudioStore._insert_event(db, project_id, ev.summarise(group),
                                          [i["id"] for i in group.items], status=status)

    # ---------- projects ----------
    def upsert_project(self, project_id: str, entity: dict, *, consent: bool, db=None) -> dict:
        ts = now()
        fields = {k: str(entity.get(k) or "")[:2000] for k in (
            "legal_name", "short_name", "credit_code", "website", "founded", "location",
            "industry", "description", "stage")}
        aliases = json.dumps([str(a)[:100] for a in (entity.get("aliases") or [])][:12],
                             ensure_ascii=False)
        source = entity.get("entity_source") or "manual"
        def write(connection):
            connection.execute(
                """INSERT INTO studio_projects (project_id, legal_name, short_name, credit_code,
                website, founded, location, industry, description, stage, aliases, entity_source,
                consent_at, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET legal_name=excluded.legal_name,
                short_name=excluded.short_name, credit_code=excluded.credit_code,
                website=excluded.website, founded=excluded.founded, location=excluded.location,
                industry=excluded.industry, description=excluded.description, stage=excluded.stage,
                aliases=excluded.aliases, entity_source=excluded.entity_source,
                consent_at=COALESCE(excluded.consent_at, studio_projects.consent_at),
                updated_at=excluded.updated_at""",
                (project_id, *[fields[k] for k in ("legal_name", "short_name", "credit_code",
                 "website", "founded", "location", "industry", "description", "stage")],
                 aliases, source, ts if consent else None, ts, ts))
            return self.get_project(project_id, db=connection)
        if db is not None:
            return write(db)
        with closing(self._connect()) as connection, connection:
            return write(connection)

    def get_project(self, project_id: str, *, db=None) -> dict | None:
        def read(connection):
            row = connection.execute(
                """SELECT p.id, p.name, s.* FROM projects p
                LEFT JOIN studio_projects s ON s.project_id = p.id WHERE p.id = ?""",
                (project_id,)).fetchone()
            return self._project_row(row) if row else None
        if db is not None:
            return read(db)
        with closing(self._connect()) as connection:
            return read(connection)

    def activity(self, project_id: str, days: int = 30) -> dict:
        """Recent event counts by importance (drives the header line field's motion)."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT importance, count(*) n FROM studio_events WHERE project_id=? AND status != 'dismissed'
                AND COALESCE(event_at, created_at) >= ? GROUP BY importance""", (project_id, since)).fetchall()
        counts = {r["importance"]: r["n"] for r in rows}
        return {"days": days, "major": counts.get("major", 0), "notable": counts.get("notable", 0),
                "routine": counts.get("routine", 0)}

    @staticmethod
    def _project_row(row) -> dict:
        data = dict(row)
        data["id"] = data.pop("id")
        data.pop("project_id", None)
        data["aliases"] = json.loads(data.get("aliases") or "[]")
        data["activated"] = bool(data.get("consent_at"))
        return data

    def list_projects(self, *, include_removed: bool = False) -> list[dict]:
        """Watched projects. Projects removed from the watchlist keep their data but leave every list and loop."""
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT p.id, p.name, s.*,
                (SELECT count(*) FROM studio_events e WHERE e.project_id=p.id
                   AND e.status='new' AND COALESCE(e.event_at, e.created_at) >= ?
                   AND (e.importance='major'
                        OR (e.importance='notable' AND e.category IN ('company','competitor')))) AS unread,
                (SELECT count(*) FROM studio_events e WHERE e.project_id=p.id
                   AND e.importance='major' AND e.status != 'dismissed'
                   AND COALESCE(e.event_at, e.created_at) >= ?) AS major_recent,
                (SELECT count(*) FROM sources src WHERE src.project_id=p.id) AS documents,
                -- A dated-ahead item (a paper's scheduled issue, a planned event) is never "latest": cap at when it was recorded.
                (SELECT max(min(COALESCE(e.event_at, e.created_at), e.created_at)) FROM studio_events e
                   WHERE e.project_id=p.id AND e.status != 'dismissed') AS last_event_at,
                (SELECT title FROM studio_events e WHERE e.project_id=p.id AND e.status != 'dismissed'
                   ORDER BY min(COALESCE(e.event_at, e.created_at), e.created_at) DESC LIMIT 1) AS last_event,
                (SELECT status FROM studio_jobs j WHERE j.project_id=p.id
                   ORDER BY created_at DESC LIMIT 1) AS job_status
                FROM projects p LEFT JOIN studio_projects s ON s.project_id = p.id
                WHERE ? OR COALESCE(s.status, 'active') != 'removed'
                ORDER BY COALESCE(s.updated_at, p.created_at) DESC""",
                # 未读 = what deserves a look: the last UNREAD_DAYS, major items, plus notable company/competitor news.
                # Backfilled history and routine industry/policy/paper items stay on their own tabs.
                ((datetime.now(UTC) - timedelta(days=UNREAD_DAYS)).isoformat(),
                 (datetime.now(UTC) - timedelta(days=30)).isoformat(), include_removed)).fetchall()
        return [self._project_row(r) for r in rows]

    def remove_project(self, project_id: str) -> None:
        """Take a project off the watchlist: hidden everywhere, no automatic work, queued jobs cancelled. Data stays."""
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT status FROM studio_projects WHERE project_id=?", (project_id,)).fetchone()
            if row and row[0] != "removed":  # Remember whether monitoring was paused, so restoring does not resume it.
                db.execute("INSERT INTO studio_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (f"removed_from:{project_id}", row[0]))
            db.execute("UPDATE studio_projects SET status='removed', updated_at=? WHERE project_id=?", (now(), project_id))
            db.execute("UPDATE studio_jobs SET status='failed', error='项目已移出关注', finished_at=? "
                       "WHERE project_id=? AND status='queued'", (now(), project_id))

    def restore_project(self, project_id: str) -> None:
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT value FROM studio_state WHERE key=?", (f"removed_from:{project_id}",)).fetchone()
            previous = row[0] if row and row[0] in ("active", "paused") else "active"
            db.execute("UPDATE studio_projects SET status=?, updated_at=? WHERE project_id=? AND status='removed'",
                       (previous, now(), project_id))

    def set_note(self, project_id: str, note: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE studio_projects SET note=?, updated_at=? WHERE project_id=?",
                       (note[:5000], now(), project_id))

    def nav_alerts(self, project_id: str) -> list[str]:
        """Read-only actionable signals; routine unread reports never light a red dot."""
        alerts = []
        with closing(self._connect()) as db:
            major = db.execute("""SELECT 1 FROM studio_events WHERE project_id=?
                                  AND importance='major' AND status='new' LIMIT 1""", (project_id,)).fetchone()
            if major:
                alerts.append("有新的重大事件")
            failed = db.execute("""SELECT DISTINCT j.kind FROM studio_jobs j
                WHERE j.project_id=? AND j.status='failed' AND NOT EXISTS (
                    SELECT 1 FROM studio_jobs recovered WHERE recovered.project_id=j.project_id
                    AND recovered.kind=j.kind AND recovered.status='done' AND recovered.rowid>j.rowid)
                ORDER BY j.kind""", (project_id,)).fetchall()
            labels = {"internal": "资料分析未完成", "external": "外部更新未完成", "external_followup": "外部信息补全未完成", "brief": "项目解读未完成",
                      "public_profile": "公开档案更新未完成", "landscape": "竞争与行业分析未完成", "insight": "深度分析未完成"}
            alerts.extend(labels.get(r[0], "项目更新未完成") for r in failed)
            # The optional signals store may not have been initialized yet. GET
            # must not create its tables or run a model to calculate this marker.
            if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_crosschecks'").fetchone()
                    and db.execute("""SELECT 1 FROM studio_crosschecks WHERE project_id=?
                                      AND severity='high' AND status='open' LIMIT 1""", (project_id,)).fetchone()):
                alerts.append("有重要信息差异，系统依据不足")
        return list(dict.fromkeys(alerts))

    def set_status(self, project_id: str, status: str) -> None:
        if status not in ("active", "paused"):
            raise ValueError("invalid status")
        with closing(self._connect()) as db, db:
            db.execute("UPDATE studio_projects SET status=?, updated_at=? WHERE project_id=?",
                       (status, now(), project_id))

    def touch(self, project_id: str, field: str) -> None:
        assert field in ("last_internal_at", "last_external_at")
        with closing(self._connect()) as db, db:
            db.execute(f"UPDATE studio_projects SET {field}=? WHERE project_id=?",
                       (now(), project_id))

    # ---------- documents ----------
    def mark_analyzed(self, project_id: str, source_ids: list[str]) -> None:
        with closing(self._connect()) as db, db:
            db.executemany(
                "INSERT OR REPLACE INTO studio_analyzed_sources VALUES (?,?,?)",
                [(project_id, s, now()) for s in source_ids])

    def analyzed_source_ids(self, project_id: str) -> set[str]:
        with closing(self._connect()) as db:
            return {r[0] for r in db.execute(
                "SELECT source_id FROM studio_analyzed_sources WHERE project_id=?", (project_id,))}

    def add_doc_change(self, project_id: str, source_ids: list[str], data: dict, *,
                       db: sqlite3.Connection | None = None) -> None:
        row = (uuid.uuid4().hex, project_id, json.dumps(source_ids), json.dumps(data, ensure_ascii=False), now())
        if db is not None:
            db.execute("INSERT INTO studio_doc_changes VALUES (?,?,?,?,?)", row)
            return
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT INTO studio_doc_changes VALUES (?,?,?,?,?)", row)

    def doc_changes(self, project_id: str, limit: int = 5) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM studio_doc_changes WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                              (project_id, limit)).fetchall()
        return [{"id": r["id"], "source_ids": json.loads(r["source_ids"]), "created_at": r["created_at"],
                 **json.loads(r["data"])} for r in rows]

    def reset_analyzed(self, project_id: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM studio_analyzed_sources WHERE project_id=?", (project_id,))

    def pending_documents(self, project_id: str) -> int:
        with closing(self._connect()) as db:
            return db.execute(
                """SELECT count(*) FROM intake_jobs j WHERE j.project_id=? AND j.status IN ('completed','needs_ocr')
                AND EXISTS (SELECT 1 FROM document_fragments f WHERE f.source_id=j.source_id)
                AND NOT EXISTS (SELECT 1 FROM studio_analyzed_sources a
                    WHERE a.project_id=j.project_id AND a.source_id=j.source_id AND a.analyzed_at>=j.updated_at)""",
                (project_id,)).fetchone()[0]

    def unanalyzed_ready_projects(self, *, model_binding: str = "") -> list[str]:
        """Activated projects with completed document intake not yet analysed."""
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT DISTINCT j.project_id FROM intake_jobs j
                JOIN studio_projects s ON s.project_id=j.project_id
                WHERE j.status IN ('completed','needs_ocr') AND s.consent_at IS NOT NULL AND s.status='active'
                AND EXISTS (SELECT 1 FROM document_fragments f WHERE f.source_id=j.source_id)
                AND NOT EXISTS (SELECT 1 FROM studio_source_permissions p WHERE p.source_id=j.source_id
                    AND (p.model_binding='' OR p.model_binding<>?))
                AND NOT EXISTS (SELECT 1 FROM studio_analyzed_sources a
                    WHERE a.project_id=j.project_id AND a.source_id=j.source_id AND a.analyzed_at>=j.updated_at)
                AND NOT EXISTS (SELECT 1 FROM studio_jobs q WHERE q.project_id=j.project_id
                    AND q.kind='internal' AND q.status IN ('queued','running'))
                AND NOT EXISTS (SELECT 1 FROM studio_jobs q WHERE q.project_id=j.project_id
                    AND q.kind='internal' AND q.status='failed' AND q.created_at>=j.updated_at)""", (model_binding,)).fetchall()
        return [r[0] for r in rows]

    def source_analysis_allowed(self, project_id: str, source_id: str, model_binding: str) -> bool:
        with closing(self._connect()) as db:
            row = db.execute("SELECT model_binding FROM studio_source_permissions WHERE source_id=? AND project_id=?",
                             (source_id, project_id)).fetchone()
        return row is None or bool(model_binding and row[0] == model_binding)

    def missing_public_profile_projects(self) -> list[str]:
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT project_id FROM studio_projects s
                WHERE consent_at IS NOT NULL AND status='active'
                AND NOT EXISTS (SELECT 1 FROM studio_public_profile p WHERE p.project_id=s.project_id)
                AND NOT EXISTS (SELECT 1 FROM studio_jobs j WHERE j.project_id=s.project_id
                    AND (j.kind='public_profile' OR (j.kind IN ('external','external_followup')
                        AND (j.status IN ('queued','running') OR (j.status='failed' AND j.retry_after>?
                            AND NOT EXISTS (SELECT 1 FROM studio_jobs recovered
                                WHERE recovered.project_id=j.project_id AND recovered.kind=j.kind
                                AND recovered.status='done' AND recovered.rowid>j.rowid))))))""", (now(),)).fetchall()
        return [r[0] for r in rows]

    def due_external_projects(self, hours: int) -> list[str]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT project_id FROM studio_projects s
                WHERE consent_at IS NOT NULL AND status='active'
                AND (last_external_at IS NULL OR last_external_at < ?)
                AND NOT EXISTS (SELECT 1 FROM studio_jobs q WHERE q.project_id=s.project_id
                    AND (q.kind='external' AND q.status IN ('queued','running')
                         OR q.kind='external_followup' AND (q.status IN ('queued','running')
                             OR (q.status='failed' AND q.retry_after>?
                                 AND NOT EXISTS (SELECT 1 FROM studio_jobs recovered
                                     WHERE recovered.project_id=q.project_id AND recovered.kind=q.kind
                                     AND recovered.status='done' AND recovered.rowid>q.rowid)))))""",
                (cutoff, now())).fetchall()
        return [r[0] for r in rows]

    def due_landscape_projects(self, days: int = LANDSCAPE_DAYS) -> list[str]:
        """Active projects with collected news whose competition/industry view is missing or a week old."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        retry = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT project_id FROM studio_projects s
                WHERE consent_at IS NOT NULL AND status='active' AND last_external_at IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM studio_landscape l WHERE l.project_id=s.project_id AND l.updated_at >= ?)
                AND NOT EXISTS (SELECT 1 FROM studio_jobs q WHERE q.project_id=s.project_id AND q.kind='landscape'
                    AND (q.status IN ('queued','running') OR (q.status='failed' AND q.created_at >= ?)))
                AND NOT EXISTS (SELECT 1 FROM studio_jobs q WHERE q.project_id=s.project_id
                    AND q.kind IN ('external','external_followup') AND q.status IN ('queued','running'))""",
                (cutoff, retry)).fetchall()
        return [r[0] for r in rows]

    def put_landscape(self, project_id: str, data: dict) -> None:
        self._put_blob("studio_landscape", project_id, data)

    def get_landscape(self, project_id: str) -> dict | None:
        return self._get_blob("studio_landscape", project_id)

    # ---------- json blobs ----------
    def _put_blob(self, table: str, project_id: str, data: dict) -> None:
        with closing(self._connect()) as db, db:
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)",
                       (project_id, json.dumps(data, ensure_ascii=False), now()))

    def _get_blob(self, table: str, project_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute(f"SELECT data, updated_at FROM {table} WHERE project_id=?",
                             (project_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(row["data"]), "updated_at": row["updated_at"]}

    def put_profile(self, project_id: str, data: dict) -> None:
        self._put_blob("studio_profile", project_id, data)

    def get_profile(self, project_id: str) -> dict | None:
        record = self._get_blob("studio_profile", project_id)
        if record:
            record["dependency"] = self.evidence.dependencies.get(project_id, "profile")
        return record

    def put_public_profile(self, project_id: str, data: dict) -> None:
        self._put_blob("studio_public_profile", project_id, data)

    def get_public_profile(self, project_id: str) -> dict | None:
        record = self._get_blob("studio_public_profile", project_id)
        if record:
            record["dependency"] = self.evidence.dependencies.get(project_id, "public_profile")
        return record

    def put_compiled_profile(self, project_id: str, data: dict, evidence_ids: list[str], *,
                             expected_generation: int, public: bool = False,
                             on_current: Callable[[sqlite3.Connection], None] | None = None) -> dict:
        kind = "public_profile" if public else "profile"
        table = "studio_public_profile" if public else "studio_profile"
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            dependency = self.evidence.dependencies.track(project_id, kind, kind, evidence_ids,
                                                          expected_generation=expected_generation, db=db)
            if dependency["status"] == "current":
                if on_current is not None:
                    on_current(db)
                db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)",
                           (project_id, json.dumps(data, ensure_ascii=False), now()))
            return dependency

    def put_plan(self, project_id: str, data: dict) -> None:
        self._put_blob("studio_plan", project_id, data)

    def get_plan(self, project_id: str) -> dict | None:
        return self._get_blob("studio_plan", project_id)

    def merge_public_plan(self, project_id: str, fields: dict, competitors: list[dict]) -> None:
        """Incremental research never replaces a competitor list with a subset.

        Merge against the latest row in one transaction. Existing names, reasons,
        manual fields and evidence survive an empty round or alias-only update.
        Explicit exclusions remain the relationship ledger's responsibility.
        """
        from project_watch.studio.peer_names import company_core

        if "competitors" in fields:
            raise ValueError("competitors must be merged, not replaced")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM studio_plan WHERE project_id=?", (project_id,)).fetchone()
            plan = json.loads(row["data"]) if row else {}
            peers = list(plan.get("competitors") or [])
            known = {company_core(p["name"]).casefold(): p for p in peers}
            for peer in competitors:
                key = company_core(peer["name"]).casefold()
                if key not in known:
                    peers.append(dict(peer))
                    known[key] = peers[-1]
                else:
                    current = known[key]
                    refs = list(current.get("evidence") or [])
                    for ref in peer.get("evidence") or []:
                        if ref not in refs:
                            refs.append(ref)
                    if refs:
                        current["evidence"] = refs
            data = {**plan, **fields, "competitors": peers}
            db.execute("INSERT OR REPLACE INTO studio_plan VALUES (?,?,?)",
                       (project_id, json.dumps(data, ensure_ascii=False), now()))

    def put_brief(self, project_id: str, data: dict) -> None:
        self._put_blob("studio_brief", project_id, data)

    def get_brief(self, project_id: str) -> dict | None:
        from project_watch.studio.public_copy import brief_view

        data = self._get_blob("studio_brief", project_id)
        if data is not None:
            data["dependency"] = self.evidence.dependencies.get(project_id, "brief")
        return brief_view(data)

    def put_compiled_brief(self, project_id, data, evidence_ids, *, expected_generation):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            dependency = self.evidence.dependencies.track(project_id, "brief", "brief", evidence_ids,
                expected_generation=expected_generation, db=db)
            if dependency["status"] == "current":
                db.execute("INSERT INTO studio_brief VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                           (project_id, json.dumps(data, ensure_ascii=False), now()))
            return dependency

    # ---------- metrics ----------
    def replace_metrics(self, project_id: str, source_ids: list[str], metrics: list[dict]) -> None:
        ts = now()
        with closing(self._connect()) as db, db:
            db.executemany("DELETE FROM studio_metrics WHERE project_id=? AND source_id=?",
                           [(project_id, s) for s in source_ids])
            db.executemany(
                """INSERT INTO studio_metrics (id, project_id, source_id, name, category, period,
                value, unit, basis, scope, locator, quote, created_at, std_name, period_key,
                value_norm, norm_unit, currency)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(uuid.uuid4().hex, project_id, m["source_id"], m["name"][:80],
                  m.get("category", "financial"), m["period"][:40], m.get("value"),
                  m.get("unit", "")[:30], m.get("basis", "unknown"), m.get("scope", "")[:40],
                  m.get("locator", "")[:200], m.get("quote", "")[:500], ts,
                  (m.get("std_name") or m["name"])[:80], (m.get("period_key") or m["period"])[:40],
                  m.get("value_norm"), (m.get("norm_unit") or "")[:30], (m.get("currency") or "")[:8])
                 for m in metrics])

    def list_metrics(self, project_id: str) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT m.*, s.reference AS source_name, s.created_at AS source_created_at FROM studio_metrics m
                LEFT JOIN sources s ON s.id = m.source_id
                WHERE m.project_id=? ORDER BY m.category, m.name, m.period""",
                (project_id,)).fetchall()
        # Rows written before normalisation existed get their columns computed on read.
        return [normalize_metric(dict(r)) for r in rows]

    # ---------- items ----------
    def add_items(self, project_id: str, category: str, items: list[dict]) -> int:
        ts = now()
        added = 0
        with closing(self._connect()) as db, db:
            for it in items:
                if not it.get("title"):
                    continue
                cur = db.execute(
                    """INSERT OR IGNORE INTO studio_items (id, project_id, key, category, title, url,
                    source, snippet, published_at, collected_at, extra)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, project_id, it["key"], category, it["title"], it["url"],
                     it["source"], it["snippet"], it["published_at"], ts,
                     json.dumps(it.get("extra") or {}, ensure_ascii=False)))
                added += cur.rowcount
        return added

    def untriaged(self, project_id: str, limit: int = 40, *,
                  item_ids: set[str] | None = None, category: str | None = None) -> list[dict]:
        where, args = ["project_id=?", "triaged=0"], [project_id]
        if item_ids is not None:
            if not item_ids:
                return []
            if len(item_ids) > 200:
                raise ValueError("triage selection exceeds bounds")
            where.append("id IN (" + ",".join("?" * len(item_ids)) + ")")
            args.extend(sorted(item_ids))
        if category is not None:
            where.append("category=?")
            args.append(category)
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM studio_items WHERE " + " AND ".join(where) +
                f" ORDER BY {_NEWS_ORDER} LIMIT ?",
                (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def save_triage(self, results: list[dict]) -> None:
        with closing(self._connect()) as db, db:
            db.executemany(
                """UPDATE studio_items SET triaged=1, relevant=?, importance=?, summary=?, impact=?,
                category=COALESCE(?, category) WHERE id=?""",
                [(1 if r.get("relevant", True) else 0,
                  r.get("importance") if r.get("importance") in IMPORTANCE else "routine",
                  str(r.get("summary", ""))[:600], str(r.get("impact", ""))[:600],
                  r.get("category"), r["id"]) for r in results])
            for r in results:
                if isinstance(r.get("triage_binding"), dict):
                    db.execute("UPDATE studio_items SET extra=json_set(CASE WHEN json_valid(extra) THEN extra ELSE '{}' END, "
                               "'$.triage_binding',json(?)) WHERE id=?",
                               (json.dumps(r["triage_binding"]), r["id"]))

    def list_items(self, *, project_id: str | None = None, category: str | None = None,
                   importance: str | None = None, include_dismissed: bool = False,
                   since_days: int | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        where = ["i.relevant=1", "i.triaged=1"]
        args: list = []
        if project_id:
            where.append("i.project_id=?")
            args.append(project_id)
        if category:
            where.append("i.category=?")
            args.append(category)
        if importance:
            where.append("i.importance=?")
            args.append(importance)
        if not include_dismissed:
            where.append("i.status != 'dismissed'")
        if since_days:
            where.append("COALESCE(i.published_at, i.collected_at) >= ?")
            args.append((datetime.now(UTC) - timedelta(days=since_days)).isoformat())
        sql = f"""SELECT i.*, p.name AS project_name FROM studio_items i
            JOIN projects p ON p.id=i.project_id WHERE {' AND '.join(where)}
            ORDER BY min(COALESCE(i.published_at, i.collected_at), i.collected_at) DESC LIMIT ? OFFSET ?"""
        with closing(self._connect()) as db:
            rows = db.execute(sql, (*args, limit, offset)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["extra"] = json.loads(d.get("extra") or "{}")
            out.append(d)
        return out

    def set_item_status(self, item_id: str, status: str) -> None:
        if status not in ITEM_STATUS:
            raise ValueError("invalid status")
        with closing(self._connect()) as db, db:
            if db.execute("UPDATE studio_items SET status=? WHERE id=?",
                          (status, item_id)).rowcount == 0:
                raise KeyError(item_id)

    def category_counts(self, project_id: str) -> dict[str, int]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT category, count(*) n FROM studio_events WHERE project_id=?
                AND status != 'dismissed' GROUP BY category""",
                (project_id,)).fetchall()
        return {r["category"]: r["n"] for r in rows}

    # ---------- article bodies ----------
    def item_by_key(self, project_id: str, key: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM studio_items WHERE project_id=? AND key=?", (project_id, key)).fetchone()
        return dict(row) if row else None

    def save_body_prefilter(self, decisions: list[dict], *, guard=None) -> None:
        """Store provisional reading choices, never change relevance or triaged state."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if guard:
                guard(db)
            for decision in decisions:
                row = db.execute("SELECT extra FROM studio_items WHERE id=?", (decision["id"],)).fetchone()
                if row is None:
                    raise KeyError("item not found")
                extra = json.loads(row["extra"] or "{}")
                extra["body_prefilter"] = {"version": "relevance-v1", "read": decision["read"]}
                db.execute("UPDATE studio_items SET extra=? WHERE id=?",
                           (json.dumps(extra, ensure_ascii=False), decision["id"]))

    def items_needing_body(self, project_id: str) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute(
                f"""SELECT * FROM studio_items WHERE project_id=? AND triaged=0 AND body_status=''
                AND category IN ('company', 'competitor', 'industry', 'policy')
                ORDER BY {_NEWS_ORDER}""",
                (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def save_body(self, item_id: str, status: str, body: str, *, donor: dict | None = None,
                  resolved_url: str | None = None, guard=None) -> None:
        from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart
        from project_watch.studio.provenance import provenance

        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if guard:
                guard(db)
            row = db.execute("SELECT * FROM studio_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError("item not found")
            project_id = row["project_id"]
            extra = json.loads(row['extra'] or '{}')
            if resolved_url and status in ('article_body', 'partial_body'):
                parsed = urlsplit(resolved_url)
                if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                        or len(resolved_url) > 2000):
                    raise ValueError('invalid resolved publisher URL')
                if resolved_url != row['url']:
                    extra.setdefault('original_url', row['url'])
                    extra['resolved_url'] = resolved_url
                    db.execute('UPDATE studio_items SET url=?,extra=? WHERE id=?',
                               (resolved_url, json.dumps(extra, ensure_ascii=False), item_id))
            version_id, basis = None, "own_text"
            readable = status in ("article_body", "partial_body", "filing_text", "search_summary", "paper_abstract")
            if status == "twin_body":
                if not donor:
                    raise ValueError("twin text requires donor identity")
                actual = db.execute("SELECT * FROM studio_items WHERE project_id=? AND id=?",
                                    (project_id, donor.get("id"))).fetchone()
                if (actual is None or actual["id"] == item_id or actual["body_status"] not in ("article_body", "partial_body")
                        or not body or body != actual["body"]):
                    raise ValueError("twin text must match the actual same-project donor body")
                pointer = db.execute("SELECT evidence_version_id FROM studio_item_evidence WHERE item_id=?", (actual["id"],)).fetchone()
                if not pointer or not pointer[0]:
                    # A pre-A3 donor may only have its old bounded body. Preserve
                    # that exact partial text; never pretend it was fully read.
                    record = self.evidence.save(EvidenceInput(project_id=project_id, source_key="item:" + actual["id"],
                        extraction_version="legacy-body-adapter-v1", source_type="public_page", visibility="public",
                        read_scope="partial", parts=[EvidencePart(locator_kind="stored_text", locator="stored body",
                                                                 text=actual["body"])]), db=db)
                    version_id = record["id"]
                else:
                    version_id = pointer[0]
                self.evidence.link_twin(project_id, "item:" + item_id, version_id, db=db)
                basis = "donor_text"
            elif readable and body:
                text = body[:2_000_000]
                scope = "summary" if status in ("search_summary", "paper_abstract") else "fragmented" if status == "filing_text" else "partial"
                website = db.execute("SELECT website FROM studio_projects WHERE project_id=?", (project_id,)).fetchone()
                metadata = provenance(resolved_url or row["url"], text, read_scope=scope, company_website=website[0] if website else "")
                record = self.evidence.save(EvidenceInput(project_id=project_id, source_key="item:" + item_id,
                    extraction_version="openalex-abstract-v1" if status == "paper_abstract" else "stored-article-v1",
                    source_type="search_summary" if status == "search_summary" else "public_page",
                    visibility="public", read_scope=scope, **metadata,
                    parts=[EvidencePart(locator_kind="stored_text", locator=f"stored chars {start}:{min(len(text),start+20000)}",
                                        text=text[start:start+20000]) for start in range(0, len(text), 20000)]), db=db)
                version_id = record["id"]
            db.execute("UPDATE studio_items SET body_status=?, body=? WHERE id=?",
                       (status[:40], body[:12000], item_id))
            db.execute("INSERT INTO studio_item_evidence VALUES (?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET "
                       "evidence_version_id=COALESCE(excluded.evidence_version_id,studio_item_evidence.evidence_version_id),"
                       "basis=CASE WHEN excluded.evidence_version_id IS NULL THEN studio_item_evidence.basis ELSE excluded.basis END,"
                       "read_status=excluded.read_status,updated_at=excluded.updated_at",
                       (item_id, project_id, version_id, basis, status[:40], now()))
            if not version_id and status in ("unavailable", "login_required", "paywall", "read_failed") and status != row["body_status"]:
                self.evidence.dependencies.record_read_failure(project_id, "item:" + item_id, "read_" + status, db=db)
            if donor:
                extra["twin_source"] = {k: donor[k] for k in ("id", "title", "url")}
                extra['resolved_url'] = actual['url']
                db.execute("UPDATE studio_items SET extra=? WHERE id=?",
                           (json.dumps(extra, ensure_ascii=False), item_id))

    def item_evidence(self, project_id: str, item_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM studio_item_evidence WHERE project_id=? AND item_id=?",
                             (project_id, item_id)).fetchone()
        return dict(row) if row else None

    def items_with_body(self, project_id: str, days: int = 21) -> list[dict]:
        """Recent items whose own article was read (donors for redirect-only links)."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT id, title, url, body, category, published_at, collected_at FROM studio_items WHERE project_id=?
                AND body_status IN ('article_body', 'partial_body') AND collected_at >= ?
                ORDER BY collected_at DESC LIMIT 500""", (project_id, since)).fetchall()
        return [dict(r) for r in rows]

    def body_coverage(self, project_id: str, days: int = 30) -> dict:
        """How much of the recent news was judged on real text, per outcome and per outlet."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT url, source, body_status,
                json_extract(CASE WHEN json_valid(extra) THEN extra END, '$.body_prefilter.read') AS prefilter_read
                FROM studio_items WHERE project_id=?
                AND collected_at >= ? AND category NOT IN ('paper', 'trial', 'patent')""", (project_id, since)).fetchall()
        # "declined": never fetched because the relevance prefilter judged it a
        # same-name/unrelated item (R44: counted as unreadable, which overstated gaps).
        outcome = {"article": 0, "twin": 0, "summary": 0, "none": 0, "declined": 0}
        outlets: dict[str, list[int]] = {}
        for r in rows:
            status = r["body_status"]
            kind = ("article" if status in ("article_body", "partial_body", "filing_text") else "twin" if status == "twin_body"
                    else "summary" if status == "search_summary"
                    else "declined" if not status and r["prefilter_read"] == 0 else "none")
            outcome[kind] += 1
            if kind == "declined":
                continue
            if status not in ("skipped", "twin_body"):
                host = (urlsplit(r["url"] or "").hostname or r["source"] or "").removeprefix("www.")
                ok_total = outlets.setdefault(host, [0, 0])
                ok_total[0] += kind == "article"
                ok_total[1] += 1
        worst = sorted(((h, v[0], v[1]) for h, v in outlets.items() if v[1] >= 2), key=lambda x: (x[1] / x[2], -x[2]))
        return {"total": len(rows), **outcome,
                "outlets": [{"host": h, "read": ok, "total": total} for h, ok, total in worst[:8]]}

    # ---------- notifications / small state ----------
    def get_state(self, key: str) -> str | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM studio_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO studio_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))

    def pending_major_events(self, days: int = 3) -> list[dict]:
        """Major events not yet pushed (recent only: a backlog is never pushed in bulk)."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with closing(self._connect()) as db:
            ids = [r["id"] for r in db.execute(
                """SELECT e.id FROM studio_events e JOIN studio_projects p ON p.project_id=e.project_id
                WHERE e.importance='major' AND e.status != 'dismissed' AND p.status='active'
                AND e.notified_at IS NULL AND e.updated_at >= ? ORDER BY e.updated_at DESC LIMIT 500""", (since,))]
        if not ids:
            return []
        # Recency is the upgrade/update time, not the original event date.
        return self.list_events(event_ids=ids, limit=500)

    def mark_notified(self, event_ids: list[str]) -> None:
        with closing(self._connect()) as db, db:
            db.executemany("UPDATE studio_events SET notified_at=? WHERE id=?", [(now(), i) for i in event_ids])

    def doc_changes_since(self, days: int) -> list[dict]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with closing(self._connect()) as db:
            rows = db.execute("SELECT project_id FROM studio_doc_changes WHERE created_at >= ? GROUP BY project_id",
                              (since,)).fetchall()
        out = []
        for r in rows:
            out += [dict(c, project_id=r["project_id"]) for c in self.doc_changes(r["project_id"], 5)
                    if c["created_at"] >= since]
        return out

    # ---------- events ----------
    def unclustered(self, project_id: str) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT * FROM studio_items WHERE project_id=? AND triaged=1 AND relevant=1
                AND event_id IS NULL ORDER BY COALESCE(published_at, collected_at)""",
                (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, project_id: str, days: int = 21) -> list[dict]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT e.*, (SELECT group_concat(i.title, '') FROM studio_items i
                   WHERE i.event_id=e.id) AS item_titles,
                   (SELECT group_concat(i.title || ' ' || i.summary, char(31)) FROM studio_items i
                   WHERE i.event_id=e.id) AS item_evidence
                FROM studio_events e WHERE e.project_id=? AND COALESCE(e.event_at, e.created_at) >= ?""",
                (project_id, cutoff)).fetchall()
        return [dict(r) for r in rows]

    def create_event(self, project_id: str, event: dict, item_ids: list[str]) -> str:
        with closing(self._connect()) as db, db:
            return self._insert_event(db, project_id, event, item_ids)

    @staticmethod
    def _insert_event(db: sqlite3.Connection, project_id: str, event: dict,
                      item_ids: list[str], *, status: str = "new") -> str:
        ts = now()
        event_id = uuid.uuid4().hex
        db.execute(
            """INSERT INTO studio_events (id, project_id, title, summary, impact, category,
            importance, event_at, created_at, updated_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, project_id, event["title"][:300], event.get("summary", "")[:600],
             event.get("impact", "")[:600], event["category"], event["importance"],
             event.get("event_at"), ts, ts, status))
        db.executemany("UPDATE studio_items SET event_id=? WHERE id=? AND project_id=?",
                       [(event_id, i, project_id) for i in item_ids])
        return event_id

    def attach_to_event(self, event_id: str, item_ids: list[str], *, importance: str,
                        impact: str = "", summary: str = "") -> None:
        rank = {"major": 0, "notable": 1, "routine": 2}
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT importance, impact, summary FROM studio_events WHERE id=?",
                             (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            upgraded = rank.get(importance, 2) < rank.get(row["importance"], 2)
            # A more important report upgrades the event and re-surfaces it as unread.
            new_impact = impact if (upgraded and impact) else (row["impact"] or impact)
            new_summary = summary if (upgraded and summary) else (row["summary"] or summary)
            db.execute(
                """UPDATE studio_events SET importance=?, impact=?, summary=?, updated_at=?,
                status=CASE WHEN ? AND status='read' THEN 'new' ELSE status END,
                notified_at=CASE WHEN ? THEN NULL ELSE notified_at END WHERE id=?""",
                (importance if upgraded else row["importance"], new_impact[:600], new_summary[:600],
                 now(), 1 if upgraded else 0, 1 if upgraded and importance == "major" else 0, event_id))
            db.executemany("UPDATE studio_items SET event_id=? WHERE id=?",
                           [(event_id, i) for i in item_ids])

    def list_events(self, *, project_id: str | None = None, category: str | None = None,
                    importance: str | None = None, include_dismissed: bool = False,
                    since_days: int | None = None, limit: int = 100, offset: int = 0,
                    event_ids: list[str] | None = None) -> list[dict]:
        where, args = ["1=1"], []
        if event_ids is not None:
            if not event_ids:
                return []
            where.append("e.id IN (" + ",".join("?" for _ in event_ids) + ")")
            args.extend(event_ids)
        if project_id:
            where.append("e.project_id=?")
            args.append(project_id)
        else:  # Cross-project feeds leave out projects removed from the watchlist.
            where.append("NOT EXISTS (SELECT 1 FROM studio_projects rp WHERE rp.project_id=e.project_id AND rp.status='removed')")
        if category:
            where.append("e.category=?")
            args.append(category)
        if importance:
            where.append("e.importance=?")
            args.append(importance)
        if not include_dismissed:
            where.append("e.status != 'dismissed'")
        if since_days:
            where.append("COALESCE(e.event_at, (SELECT max(ai.published_at) FROM studio_items ai "
                         "WHERE ai.event_id=e.id AND ai.key LIKE 'agent:%'), e.created_at) >= ?")
            args.append((datetime.now(UTC) - timedelta(days=since_days)).isoformat())
        sql = f"""SELECT e.*, p.name AS project_name FROM studio_events e
            JOIN projects p ON p.id=e.project_id WHERE {' AND '.join(where)}
            ORDER BY COALESCE(e.event_at, (SELECT max(ai.published_at) FROM studio_items ai
              WHERE ai.event_id=e.id AND ai.key LIKE 'agent:%'), e.created_at) DESC LIMIT ? OFFSET ?"""
        with closing(self._connect()) as db:
            events = [dict(r) for r in db.execute(sql, (*args, limit, offset)).fetchall()]
            ids = [e["id"] for e in events]
            sources: dict[str, list[dict]] = {i: [] for i in ids}
            for chunk in (ids[n:n + 500] for n in range(0, len(ids), 500)):
                marks = ",".join("?" * len(chunk))
                for r in db.execute(
                        f"""SELECT id, event_id, title, url, source, published_at, collected_at,
                        body_status, extra FROM studio_items WHERE event_id IN ({marks})
                        ORDER BY COALESCE(published_at, collected_at)""", chunk):
                    item = dict(r)
                    item["extra"] = json.loads(item.get("extra") or "{}")
                    sources[item.pop("event_id")].append(item)
        for e in events:
            e["sources"] = sources.get(e["id"], [])
            e["source_count"] = len(e["sources"])
            # Link to a report the reader can open directly: a read article, then any direct link.
            first = (next((i for i in e["sources"] if i.get("body_status") in ("article_body", "partial_body")), None)
                     or next((i for i in e["sources"] if "news.google.com" not in (i.get("url") or "")), None)
                     or (e["sources"][0] if e["sources"] else {}))
            e["url"] = first.get("url", "")
            e["source"] = first.get("source", "")
        return events

    def set_event_status(self, event_id: str, status: str) -> None:
        if status not in ITEM_STATUS:
            raise ValueError("invalid status")
        with closing(self._connect()) as db, db:
            if db.execute("UPDATE studio_events SET status=? WHERE id=?",
                          (status, event_id)).rowcount == 0:
                raise KeyError(event_id)
            if status == "dismissed":
                db.execute("UPDATE studio_items SET status='dismissed' WHERE event_id=?", (event_id,))

    # ---------- jobs ----------
    def enqueue(self, project_id: str, kind: str, *, automatic: bool = False, input_version: str = "", db=None) -> dict:
        if kind not in JOB_KINDS or len(input_version) > 100:
            raise ValueError("invalid job kind")
        own_connection = db is None
        with (closing(self._connect()) if own_connection else nullcontext(db)) as db, (db if own_connection else nullcontext()):
            if own_connection:
                db.execute("BEGIN IMMEDIATE")
            mode = db.execute('SELECT execution_mode FROM studio_projects WHERE project_id=?', (project_id,)).fetchone()
            if mode and mode[0] == 'agent':
                return {'id': None, 'project_id': project_id, 'kind': kind, 'status': 'agent_required'}
            if kind == 'external':
                continuation = db.execute("SELECT * FROM studio_jobs WHERE project_id=? "
                    "AND kind='external_followup' AND status IN ('queued','running') "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1", (project_id,)).fetchone()
                if continuation:
                    return dict(continuation)
            existing = db.execute(
                "SELECT * FROM studio_jobs WHERE project_id=? AND kind=? AND status IN ('queued','running') ORDER BY created_at,id LIMIT 1",
                (project_id, kind)).fetchone()
            if existing:
                return dict(existing)
            if automatic:
                held = db.execute("SELECT * FROM studio_jobs WHERE project_id=? AND kind=? AND status='failed' "
                                  "AND retry_after>? AND (?='' OR input_version=?) ORDER BY created_at DESC LIMIT 1",
                                  (project_id, kind, now(), input_version, input_version)).fetchone()
                if held:
                    return dict(held)
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO studio_jobs (id, project_id, kind, status, created_at,input_version) "
                       "VALUES (?,?,?,'queued',?,?)", (job_id, project_id, kind, now(), input_version))
            if kind in ('external', 'external_followup'):
                # Persist intent with admission, not by guessing from a later
                # empty input_version. Reusing an active job above never edits
                # its frozen mode or turns a scheduled job into manual work.
                db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                           (job_id, 'collection_request_v1', json.dumps(dict(
                               schema_version=1, mode='incremental' if automatic else 'refresh',
                               origin='scheduler' if automatic else 'owner'))))
            return dict(db.execute("SELECT * FROM studio_jobs WHERE id=?", (job_id,)).fetchone())

    def queue_external_followup(self, job: dict, data: dict) -> dict:
        """Atomically hand off persisted input and the brief before releasing the first lease."""
        encoded = json.dumps(data)
        if len(encoded) > 1_000_000:
            raise ValueError('followup input exceeds bounds')
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            parent = db.execute("SELECT * FROM studio_jobs WHERE id=? AND project_id=? AND kind='external'",
                                (job['id'], job['project_id'])).fetchone()
            if (not parent or parent['status'] != 'running' or parent['lease_until'] <= now()
                    or parent['lease_token'] != job.get('lease_token')):
                raise RuntimeError('job lease expired or replaced')
            saved = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='external_first_ready'",
                               (job['id'],)).fetchone()
            if saved:
                child_id = json.loads(saved[0])['followup_id']
                return dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (child_id,)).fetchone())
            # Both inserts and their receipts commit together. A crash cannot leave
            # an orphan child with no input, or a committed handoff with no brief.
            active = db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind='external_followup' "
                                "AND status IN ('queued','running')", (job['project_id'],)).fetchone()
            if active:
                raise ValueError('外部信息已有补全任务')
            brief = db.execute("SELECT id FROM studio_jobs WHERE project_id=? AND kind='brief' "
                               "AND status IN ('queued','running')", (job['project_id'],)).fetchone()
            if not brief:
                db.execute("INSERT INTO studio_jobs (id,project_id,kind,status,created_at) VALUES (?,?,'brief','queued',?)",
                           (uuid.uuid4().hex, job['project_id'], now()))
            child_id = uuid.uuid4().hex
            db.execute("INSERT INTO studio_jobs (id,project_id,kind,status,created_at,input_version) "
                       "VALUES (?,?,'external_followup','queued',?,?)", (child_id, job['project_id'], now(), job['id']))
            db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (child_id, 'external_followup_input', encoded))
            db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)',
                       (job['id'], 'external_first_ready', json.dumps({'followup_id': child_id})))
            return dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (child_id,)).fetchone())

    def retry_external_followup(self, project_id: str) -> dict:
        """Explicitly resume the same known-outcome child and its saved call receipts."""
        from project_watch.studio.recovery import UNRESOLVED

        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT * FROM studio_jobs WHERE project_id=? AND kind='external_followup' "
                             "ORDER BY created_at DESC,rowid DESC LIMIT 1", (project_id,)).fetchone()
            if not job:
                raise ValueError('没有可继续的补全任务，请重新更新项目')
            if job['status'] in ('queued', 'running'):
                return dict(job)
            if job['status'] != 'failed':
                raise ValueError('补全已完成，请重新更新项目')
            unknown = db.execute(f"SELECT 1 FROM studio_job_calls c WHERE c.job_id=? AND {UNRESOLVED}",
                                 (job['id'],)).fetchone()
            if unknown:
                raise ValueError('请求结果尚未确认，请先核对来源；不会自动重发')
            if job['retry_after'] == '9999-12-31':
                if '监控依据已变化' in (job['error'] or ''):
                    raise FollowupSuperseded('监控依据已变化，请按最新信息重新更新项目')
                raise ValueError('请求结果尚未确认，请先核对来源；不会自动重发')
            db.execute("UPDATE studio_jobs SET status='queued',error='',finished_at=NULL,retry_after=NULL,"
                       "lease_token='',lease_until=NULL WHERE id=?", (job['id'],))
            return dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (job['id'],)).fetchone())

    def claim(self) -> dict | None:
        from project_watch.studio.recovery import UNRESOLVED

        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT * FROM studio_jobs WHERE status='running' AND (lease_until IS NULL OR lease_until<=?)", (now(),)).fetchall()
            for old in expired:
                uncertain = db.execute(f"SELECT 1 FROM studio_job_calls c WHERE c.job_id=? AND {UNRESOLVED} LIMIT 1", (old["id"],)).fetchone()
                unsafe = not old["lease_token"] or uncertain or old["attempts"] >= 3
                if unsafe:
                    db.execute("UPDATE studio_jobs SET status='failed',error='interrupted: request outcome uncertain or recovery limit',"
                               "finished_at=?,retry_after=?,lease_token='' WHERE id=?", (now(), "9999-12-31", old["id"]))
                else:
                    db.execute("UPDATE studio_jobs SET status='queued',lease_token='',lease_until=NULL WHERE id=?", (old["id"],))
            # Prefer finished-input briefs, but reserve every third selection for
            # oldest eligible work. The persisted counter survives worker restarts
            # and is updated under the same lock as the lease claim.
            burst = db.execute("SELECT value FROM studio_state WHERE key='queue_brief_burst'").fetchone()
            prefer_brief = int(burst[0] if burst else 0) < 2
            row = db.execute("""SELECT j.* FROM studio_jobs j
                LEFT JOIN studio_projects p ON p.project_id=j.project_id
                WHERE j.status='queued' AND COALESCE(p.execution_mode,'api')='api' AND (j.kind NOT IN ('external','external_followup','public_profile','landscape','business','competition','details','commercial','insight','brief')
                    OR COALESCE(p.status,'active')='active')
                AND (j.kind!='external_followup' OR EXISTS (SELECT 1 FROM studio_jobs parent
                     WHERE parent.id=j.input_version AND parent.project_id=j.project_id AND parent.kind='external' AND parent.status='done')
                     OR EXISTS (SELECT 1 FROM studio_job_checkpoints link JOIN studio_jobs parent
                       ON parent.id=json_extract(link.data,'$.job_id')
                       JOIN studio_call_recoveries grant ON grant.job_id=parent.id
                         AND grant.previous_key=json_extract(link.data,'$.previous_key')
                       WHERE link.job_id=j.id AND link.stage='recovery_origin' AND parent.project_id=j.project_id
                         AND parent.kind IN ('external','external_followup') AND parent.status='done'
                         AND j.input_version='recovery:'||parent.id))
                AND (j.kind!='external' OR NOT EXISTS (SELECT 1 FROM studio_jobs material
                     WHERE material.project_id=j.project_id AND material.kind='internal' AND material.status IN ('queued','running')))
                ORDER BY CASE WHEN ? AND j.kind='brief' THEN 0 ELSE 1 END,
                         COALESCE(j.queue_at,j.created_at),j.rowid LIMIT 1""", (prefer_brief,)).fetchone()
            if not row:
                return None
            next_burst = int(burst[0] if burst else 0) + 1 if prefer_brief and row['kind'] == 'brief' else 0
            db.execute("INSERT INTO studio_state VALUES ('queue_brief_burst',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(next_burst),))
            db.execute("UPDATE studio_jobs SET status='running',started_at=COALESCE(started_at,?),lease_token=?,"
                       "lease_until=?,progress_at=?,heartbeat_at=?,attempts=attempts+1 WHERE id=?",
                       (now(), uuid.uuid4().hex, (datetime.now(UTC) + timedelta(seconds=90)).isoformat(), now(), now(), row["id"]))
            return dict(db.execute("SELECT * FROM studio_jobs WHERE id=?", (row["id"],)).fetchone())

    def heartbeat(self, job_id: str, lease_token: str) -> bool:
        with closing(self._connect()) as db, db:
            return bool(db.execute("UPDATE studio_jobs SET lease_until=? WHERE id=? AND status='running' AND lease_token=? AND lease_until>?",
                ((datetime.now(UTC) + timedelta(seconds=90)).isoformat(), job_id, lease_token, now())).rowcount)

    def set_stage(self, job_id: str, stage: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE studio_jobs SET stage=?,progress_at=? WHERE id=? AND stage!=?",
                       (stage[:200], now(), job_id, stage[:200]))

    def yield_job(self, job: dict) -> bool:
        """Cooperative handoff only after a durable stage checkpoint, not an error."""
        with closing(self._connect()) as db, db:
            return bool(db.execute("UPDATE studio_jobs SET status='queued',stage='已保存进度，排队继续',"
                "lease_token='',lease_until=NULL,queue_at=?,yield_count=yield_count+1,attempts=MAX(0,attempts-1) "
                "WHERE id=? AND status='running' AND lease_token=? AND lease_until>?",
                (now(), job['id'], job['lease_token'], now())).rowcount)

    def defer_paused(self, job: dict) -> bool:
        """Keep the same job/receipts queued for resume; never call this a failure."""
        with closing(self._connect()) as db, db:
            return bool(db.execute("UPDATE studio_jobs SET status='queued',stage='已暂停，恢复后继续',"
                "lease_token='',lease_until=NULL WHERE id=? AND status='running' AND lease_token=? AND lease_until>?",
                (job['id'], job['lease_token'], now())).rowcount)

    def finish(self, job_id: str, *, error: str = "", lease_token: str | None = None) -> bool:
        from project_watch.studio.recovery import _RECEIVED_MODEL_FAILURE, UNRESOLVED

        with closing(self._connect()) as db, db:
            unknown = db.execute(f"SELECT 1 FROM studio_job_calls c WHERE c.job_id=? AND {UNRESOLVED} LIMIT 1", (job_id,)).fetchone()
            if unknown and not error:
                error = 'request_outcome_unknown: 已保留已有结果，等待确认未返回的请求'
            model_failure = db.execute(f'SELECT 1 FROM studio_job_calls c WHERE c.job_id=? AND ({_RECEIVED_MODEL_FAILURE}) LIMIT 1', (job_id,)).fetchone()
            if model_failure and not error:
                error = 'model_http_5xx: 已保留已有结果，模型请求未取得可用回答，等待确认恢复'
            business = db.execute("SELECT 1 FROM studio_jobs WHERE id=? AND kind IN ('business','competition','details')", (job_id,)).fetchone()
            held = bool(model_failure) or 'request_outcome_unknown' in error or '监控依据已变化' in error
            retry_after = "9999-12-31" if error and (unknown or business or held) else (datetime.now(UTC) + timedelta(minutes=30)).isoformat() if error else None
            return bool(db.execute("UPDATE studio_jobs SET status=?,error=?,finished_at=?,stage='',retry_after=?,lease_token='' "
                       "WHERE id=? AND (? IS NULL OR (status='running' AND lease_token=? AND lease_until>?))",
                       ("failed" if error else "done", error[:500], now(),
                        retry_after,
                        job_id, lease_token, lease_token, now())).rowcount)

    def jobs(self, project_id: str, limit: int = 10) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM studio_jobs WHERE project_id=? "
                              "ORDER BY created_at DESC LIMIT ?", (project_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---------- usage ----------
    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        day = datetime.now(UTC).date().isoformat()
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO studio_usage VALUES (?,1,?,?) ON CONFLICT(day) DO UPDATE SET
                calls=calls+1, input_tokens=input_tokens+excluded.input_tokens,
                output_tokens=output_tokens+excluded.output_tokens""",
                (day, int(input_tokens or 0), int(output_tokens or 0)))

    def usage(self, days: int = 30) -> list[dict]:
        from project_watch.studio.usage import rows

        return rows(self, days)
