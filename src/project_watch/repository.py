"""SQLite repository for the single-node development preview."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
import uuid
from contextlib import closing, nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from project_watch.domain import (
    RESEARCH_TOPICS,
    AuditEvent,
    CompetitivePeer,
    DocumentFragment,
    ExecutionTask,
    FragmentInput,
    HoldingSnapshot,
    IdentityReview,
    IntakeJob,
    MonitoringJob,
    MonitoringPlan,
    Observation,
    ObservationStatus,
    Project,
    ReviewDecision,
    ReviewImpact,
    Signal,
    SignalInput,
    SourceRun,
    SourceRunStatus,
    SourceVersion,
    WatchItem,
    WatchItemChange,
    normalized_project_name,
)
from project_watch.financial_intake import (
    FinancialSuggestion,
    validate_financial_suggestions,
)
from project_watch.holding_input import validated_holding_input
from project_watch.identity_input import review_note, verified_identity_input
from project_watch.monitor_profile_store import ensure_profile_schema, validate_edit
from project_watch.native_intake import NativeCandidate, validate_native_candidates
from project_watch.operating_intake import (
    OperatingSuggestion,
    validate_operating_suggestions,
)
from project_watch.source_topics import impact_topic, review_topics
from project_watch.followup_records import (
    watch_change_input,
    watch_change_review_input,
    watch_item_input,
    watch_review_input,
)


class NotFoundError(LookupError):
    """A project, source, or observation does not exist."""


class ReviewConflictError(RuntimeError):
    """The observation changed after the reviewer loaded it."""


class IdentityConflictError(RuntimeError):
    """The project's identity decision changed after the reviewer loaded it."""


class IntakeConflictError(RuntimeError):
    """An intake job was not in the required state."""


class ProjectRepository:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.parent.is_dir():
            raise ValueError("database directory does not exist")
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL UNIQUE,
                    identity_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    kind TEXT NOT NULL,
                    reference TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (project_id, content_sha256)
                )
                """
            )
            ensure_profile_schema(connection)
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS sources_id_project ON sources (id, project_id)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS brave_news_budget (
                    month_utc TEXT PRIMARY KEY,
                    used INTEGER NOT NULL CHECK (used >= 0)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS competitive_peers (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL,
                    linked_project_id TEXT REFERENCES projects(id),
                    status TEXT NOT NULL CHECK (status IN ('candidate', 'confirmed', 'rejected')),
                    version INTEGER NOT NULL CHECK (version > 0),
                    proposed_by TEXT,
                    reviewed_by TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (project_id, name_key),
                    CHECK (linked_project_id IS NULL OR linked_project_id <> project_id)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS competitive_peers_project ON competitive_peers (project_id, created_at, id)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS holding_snapshots (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    fund_name TEXT NOT NULL CHECK (length(fund_name) BETWEEN 1 AND 200),
                    as_of TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_locator TEXT NOT NULL,
                    invested_cost_wan TEXT,
                    ownership_pct TEXT,
                    current_value_wan TEXT,
                    status TEXT NOT NULL CHECK (status IN ('candidate', 'confirmed', 'rejected')),
                    version INTEGER NOT NULL CHECK (version > 0),
                    proposed_by TEXT,
                    reviewed_by TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (invested_cost_wan IS NOT NULL OR ownership_pct IS NOT NULL
                           OR current_value_wan IS NOT NULL),
                    FOREIGN KEY (source_id, project_id) REFERENCES sources(id, project_id)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS holding_snapshots_project_fund_date
                   ON holding_snapshots (project_id, fund_name, as_of DESC, id)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS watch_items (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    observation_id TEXT NOT NULL,
                    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
                    title_key TEXT NOT NULL,
                    question TEXT NOT NULL CHECK (length(question) BETWEEN 1 AND 1000),
                    owner TEXT CHECK (length(owner) BETWEEN 1 AND 128),
                    due_on TEXT,
                    status TEXT NOT NULL CHECK (status IN (
                        'candidate', 'watching', 'action_required', 'rejected')),
                    version INTEGER NOT NULL CHECK (version > 0),
                    proposed_by TEXT,
                    reviewed_by TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (project_id, observation_id, title_key),
                    UNIQUE (id, project_id),
                    FOREIGN KEY (observation_id, project_id) REFERENCES observations(id, project_id)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watch_items_project_status ON watch_items (project_id, status, due_on)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS watch_item_events (
                    id TEXT PRIMARY KEY,
                    watch_item_id TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    version INTEGER NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    actor_ref TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (watch_item_id, version),
                    FOREIGN KEY (watch_item_id, project_id) REFERENCES watch_items(id, project_id)
                )"""
            )
            watch_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(watch_items)")
            }
            for name, definition in {
                "current_observation_id": "TEXT",
                "initial_question": "TEXT",
                "initial_owner": "TEXT",
                "initial_due_on": "TEXT",
                "resolved_at": "TEXT",
            }.items():
                if name not in watch_columns:
                    connection.execute(f"ALTER TABLE watch_items ADD COLUMN {name} {definition}")
            connection.execute(
                """UPDATE watch_items SET
                   current_observation_id = COALESCE(current_observation_id, observation_id),
                   initial_question = COALESCE(initial_question, question),
                   initial_owner = COALESCE(initial_owner, owner),
                   initial_due_on = COALESCE(initial_due_on, due_on)
                   WHERE current_observation_id IS NULL OR initial_question IS NULL"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS watch_item_changes (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    watch_item_id TEXT NOT NULL,
                    base_version INTEGER NOT NULL CHECK (base_version > 0),
                    target_status TEXT NOT NULL CHECK (target_status IN (
                        'watching', 'action_required', 'resolved')),
                    question TEXT NOT NULL CHECK (length(question) BETWEEN 1 AND 1000),
                    owner TEXT CHECK (length(owner) BETWEEN 1 AND 128),
                    due_on TEXT,
                    evidence_observation_id TEXT,
                    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 5 AND 500),
                    status TEXT NOT NULL CHECK (status IN ('candidate', 'accepted', 'rejected')),
                    proposed_by TEXT,
                    reviewed_by TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (watch_item_id, project_id) REFERENCES watch_items(id, project_id),
                    FOREIGN KEY (evidence_observation_id, project_id)
                        REFERENCES observations(id, project_id)
                )"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS watch_item_changes_one_pending
                   ON watch_item_changes (watch_item_id) WHERE status = 'candidate'"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS project_identity_reviews (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    version INTEGER NOT NULL CHECK (version > 0),
                    action TEXT NOT NULL CHECK (action IN ('verify', 'reopen')),
                    legal_name TEXT,
                    registration_code TEXT,
                    evidence_source_id TEXT,
                    evidence_locator TEXT,
                    evidence_url TEXT,
                    note TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (project_id, version),
                    FOREIGN KEY (evidence_source_id, project_id) REFERENCES sources(id, project_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS document_index (
                    source_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    topic TEXT NOT NULL CHECK (topic IN (
                        '公司与团队', '产品与技术', '经营情况', '财务表现',
                        '行业与竞争', '风险与合规', '未分类')),
                    basis TEXT NOT NULL CHECK (basis IN (
                        'filename', 'content', 'filename_and_content', 'none')),
                    rule_version INTEGER NOT NULL CHECK (rule_version > 0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (source_id, project_id) REFERENCES sources(id, project_id)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS document_index_project_topic ON document_index (project_id, topic)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    source_id TEXT NOT NULL REFERENCES sources(id),
                    subject TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    number TEXT,
                    unit TEXT,
                    period TEXT,
                    scope TEXT,
                    basis TEXT NOT NULL DEFAULT 'unknown' CHECK (basis IN ('actual', 'forecast', 'budget', 'unknown')),
                    source_locator TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    fingerprint TEXT
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS observations_id_project ON observations (id, project_id)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS review_impacts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    trigger_source_id TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    topic TEXT NOT NULL CHECK (topic IN (
                        '公司与团队', '产品与技术', '经营情况', '财务表现',
                        '行业与竞争', '风险与合规')),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'reviewed')),
                    resolution TEXT CHECK (resolution IN ('unchanged', 'follow_up')),
                    note TEXT CHECK (length(note) BETWEEN 1 AND 500),
                    reviewer_ref TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    UNIQUE (trigger_source_id, observation_id),
                    FOREIGN KEY (trigger_source_id, project_id) REFERENCES sources(id, project_id),
                    FOREIGN KEY (observation_id, project_id) REFERENCES observations(id, project_id)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS review_impacts_project_status
                   ON review_impacts (project_id, status, created_at, id)"""
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(observations)").fetchall()
            }
            if "fingerprint" not in columns:
                connection.execute("ALTER TABLE observations ADD COLUMN fingerprint TEXT")
            if "basis" not in columns:
                connection.execute(
                    "ALTER TABLE observations ADD COLUMN basis TEXT NOT NULL DEFAULT 'unknown'"
                )
            legacy_rows = connection.execute(
                """SELECT * FROM observations WHERE fingerprint IS NULL
                   ORDER BY created_at, id"""
            ).fetchall()
            seen_fingerprints = {
                (str(row["project_id"]), str(row["fingerprint"]))
                for row in connection.execute(
                    "SELECT project_id, fingerprint FROM observations WHERE fingerprint IS NOT NULL"
                ).fetchall()
            }
            for row in legacy_rows:
                fingerprint = self._observation_fingerprint(
                    source_id=row["source_id"],
                    subject=row["subject"],
                    statement=row["statement"],
                    number=row["number"],
                    unit=row["unit"],
                    period=row["period"],
                    scope=row["scope"],
                    source_locator=row["source_locator"],
                )
                key = (str(row["project_id"]), fingerprint)
                if key in seen_fingerprints:
                    # Preserve old duplicate records; future submissions bind to the first.
                    fingerprint = f"{fingerprint}:{row['id']}"
                seen_fingerprints.add(key)
                connection.execute(
                    "UPDATE observations SET fingerprint = ? WHERE id = ?",
                    (fingerprint, row["id"]),
                )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS observations_project_fingerprint
                   ON observations (project_id, fingerprint)"""
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_decisions (
                    id TEXT PRIMARY KEY,
                    observation_id TEXT NOT NULL REFERENCES observations(id),
                    reviewer TEXT NOT NULL,
                    action TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    decided_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    action TEXT NOT NULL,
                    actor_kind TEXT NOT NULL,
                    actor_ref TEXT,
                    channel TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    source_id TEXT REFERENCES sources(id),
                    source_sha256 TEXT,
                    source_locator TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS audit_events_project_order
                   ON audit_events (project_id, created_at, id)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS audit_events_subject
                   ON audit_events (project_id, subject_id, action)"""
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    source_id TEXT NOT NULL UNIQUE REFERENCES sources(id),
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS document_fragments (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    source_id TEXT NOT NULL REFERENCES sources(id),
                    job_id TEXT NOT NULL REFERENCES intake_jobs(id),
                    ordinal INTEGER NOT NULL,
                    locator TEXT NOT NULL,
                    text TEXT NOT NULL,
                    UNIQUE (source_id, ordinal)
                )
                """
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS monitoring_plans (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    cadence_minutes INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE (project_id, kind, target)
                )"""
            )
            from project_watch.external_focus_schema import ensure_external_focus_schema
            ensure_external_focus_schema(connection)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS source_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    plan_id TEXT NOT NULL REFERENCES monitoring_plans(id),
                    status TEXT NOT NULL,
                    error_code TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    idempotency_key TEXT,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    matched_count INTEGER,
                    new_count INTEGER NOT NULL DEFAULT 0
                )"""
            )
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(source_runs)").fetchall()
            }
            for name, definition in (
                ("idempotency_key", "TEXT"),
                ("item_count", "INTEGER NOT NULL DEFAULT 0"),
                ("matched_count", "INTEGER"),
                ("new_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in run_columns:
                    connection.execute(f"ALTER TABLE source_runs ADD COLUMN {name} {definition}")
            connection.execute(
                """CREATE INDEX IF NOT EXISTS source_runs_plan_order
                   ON source_runs (plan_id, started_at, id)"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS source_runs_idempotency
                   ON source_runs (plan_id, idempotency_key)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    plan_id TEXT NOT NULL REFERENCES monitoring_plans(id),
                    run_id TEXT NOT NULL REFERENCES source_runs(id),
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    published_at TEXT,
                    first_seen_at TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    freshness TEXT NOT NULL,
                    status TEXT NOT NULL,
                    UNIQUE (plan_id, external_id, content_sha256)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS monitoring_jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    plan_id TEXT NOT NULL REFERENCES monitoring_plans(id),
                    idempotency_key TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_until TEXT,
                    run_id TEXT REFERENCES source_runs(id),
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (plan_id, idempotency_key)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS monitoring_jobs_queue
                   ON monitoring_jobs (status, lease_until, created_at)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS execution_tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    contract_version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (project_id, idempotency_key)
                )"""
            )

    @staticmethod
    def _observation_fingerprint(
        *,
        source_id: str,
        subject: str,
        statement: str,
        number: str | None,
        unit: str | None,
        period: str | None,
        scope: str | None,
        source_locator: str,
        basis: str = "unknown",
    ) -> str:
        claim = [source_id, subject, statement, number, unit, period, scope, source_locator]
        if basis != "unknown":
            claim.append(basis)
        serialized = json.dumps(claim, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _project(row: sqlite3.Row) -> Project:
        return Project(
            id=str(row["id"]),
            name=str(row["name"]),
            identity_status=row["identity_status"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def create_project(self, name: str, *, actor_id: str | None = None, db=None) -> Project:
        normalized = normalized_project_name(name)
        name_key = normalized.casefold()
        def write(connection):
            existing = connection.execute(
                "SELECT * FROM projects WHERE name_key = ?", (name_key,)
            ).fetchone()
            if existing is not None:
                return self._project(existing)
            project = Project(
                id=str(uuid.uuid4()),
                name=normalized,
                identity_status="unverified",
                created_at=datetime.now(UTC),
            )
            connection.execute(
                """INSERT INTO projects (id, name, name_key, identity_status, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    project.id,
                    project.name,
                    name_key,
                    project.identity_status,
                    project.created_at.isoformat(),
                ),
            )
            if actor_id is not None:
                self._append_audit_event(
                    connection,
                    project_id=project.id,
                    action="project.created",
                    actor_kind="verified_user",
                    actor_ref=actor_id,
                    channel="team_web",
                    subject_id=project.id,
                    source_id=None,
                    source_sha256=None,
                    source_locator=None,
                )
            return project

        if db is not None:
            return write(db)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return write(connection)

    def get_project(self, project_id: str) -> Project | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            return self._project(row) if row is not None else None

    def list_projects(self) -> list[Project]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY created_at, id").fetchall()
            return [self._project(row) for row in rows]

    def monitor_profile_history(self, project_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM monitor_profile_revisions WHERE project_id=? ORDER BY version",
                (project_id,),
            ).fetchall()
            return [{"version": row["version"], "fields": json.loads(row["fields_json"]),
                     "focus": json.loads(row["focus_json"]) if row["focus_json"] else None,
                     "actor_ref": row["actor_ref"], "created_at": row["created_at"]}
                    for row in rows]

    def edit_monitor_profile(
        self, project_id: str, *, expected_version: int, fields: dict | None = None,
        focus: list[str] | None = None, restore_fields: list[str] | None = None,
        restore_focus: bool = False,
    ) -> int:
        """Local human preferences only; does not review observations or bind identity."""
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("无效的档案版本。")
        fields, focus = validate_edit(fields or {}, focus, restore_fields or [], restore_focus)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            prior = connection.execute(
                "SELECT * FROM monitor_profile_revisions WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            version = prior["version"] if prior else 0
            if version != expected_version:
                raise ReviewConflictError("档案已更新，请重新打开后修改。")
            merged = json.loads(prior["fields_json"]) if prior else {}
            old_focus = json.loads(prior["focus_json"]) if prior and prior["focus_json"] else None
            for key in restore_fields or []:
                merged.pop(key, None)
            merged.update(fields)
            new_focus = None if restore_focus else focus if focus is not None else old_focus
            if prior and merged == json.loads(prior["fields_json"]) and new_focus == old_focus:
                return version
            version += 1
            connection.execute(
                "INSERT INTO monitor_profile_revisions VALUES (?,?,?,?,?,?)",
                (project_id, version, json.dumps(merged, ensure_ascii=False),
                 json.dumps(new_focus, ensure_ascii=False) if new_focus is not None else None,
                 "local_user", datetime.now(UTC).isoformat()),
            )
            self._append_audit_event(
                connection, project_id=project_id, action="monitor_profile.edited",
                actor_kind="unverified_local_user", actor_ref="local_user", channel="local_workspace",
                subject_id=f"{project_id}:profile:{version}", source_id=None,
                source_sha256=None, source_locator=None,
            )
            return version

    @staticmethod
    def _competitive_peer(row: sqlite3.Row) -> CompetitivePeer:
        return CompetitivePeer(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            name=str(row["name"]),
            linked_project_id=row["linked_project_id"],
            status=row["status"],
            version=int(row["version"]),
            proposed_by=row["proposed_by"],
            reviewed_by=row["reviewed_by"],
            note=row["note"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_competitive_peers(self, project_id: str) -> list[CompetitivePeer]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM competitive_peers WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return [self._competitive_peer(row) for row in rows]

    @staticmethod
    def _holding_snapshot(row: sqlite3.Row) -> HoldingSnapshot:
        return HoldingSnapshot(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            fund_name=str(row["fund_name"]),
            as_of=date.fromisoformat(row["as_of"]),
            source_id=str(row["source_id"]),
            source_locator=str(row["source_locator"]),
            invested_cost_wan=Decimal(row["invested_cost_wan"])
            if row["invested_cost_wan"] is not None else None,
            ownership_pct=Decimal(row["ownership_pct"])
            if row["ownership_pct"] is not None else None,
            current_value_wan=Decimal(row["current_value_wan"])
            if row["current_value_wan"] is not None else None,
            status=row["status"],
            version=int(row["version"]),
            proposed_by=row["proposed_by"],
            reviewed_by=row["reviewed_by"],
            review_note=row["review_note"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_holding_snapshots(self, project_id: str) -> list[HoldingSnapshot]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM holding_snapshots WHERE project_id = ? ORDER BY as_of DESC, id",
                (project_id,),
            ).fetchall()
            return [self._holding_snapshot(row) for row in rows]

    def propose_holding_snapshot(
        self,
        project_id: str,
        *,
        fund_name: str,
        as_of: date,
        source_id: str,
        source_locator: str,
        invested_cost_wan: Decimal | None,
        ownership_pct: Decimal | None,
        current_value_wan: Decimal | None,
        actor_id: str | None = None,
    ) -> HoldingSnapshot:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            source = connection.execute(
                "SELECT * FROM sources WHERE id = ? AND project_id = ?", (source_id, project_id)
            ).fetchone()
            if source is None:
                raise NotFoundError("holding source not found")
            fragments = connection.execute(
                """SELECT text, locator FROM document_fragments
                   WHERE project_id = ? AND source_id = ? AND locator = ?""",
                (project_id, source_id, source_locator),
            ).fetchall()
            if len(fragments) != 1:
                raise ValueError("holding source locator must select exactly one extracted fragment")
            fund_name, cost, pct, value = validated_holding_input(
                fund_name=fund_name,
                as_of=as_of,
                source_locator=source_locator,
                fragment_locator=fragments[0]["locator"],
                fragment_text=fragments[0]["text"],
                invested_cost_wan=invested_cost_wan,
                ownership_pct=ownership_pct,
                current_value_wan=current_value_wan,
            )
            snapshot_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            row = connection.execute(
                """INSERT INTO holding_snapshots
                   (id, project_id, fund_name, as_of, source_id, source_locator,
                    invested_cost_wan, ownership_pct, current_value_wan,
                    status, version, proposed_by, reviewed_by, review_note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', 1, ?, NULL, NULL, ?, ?)
                   RETURNING *""",
                (snapshot_id, project_id, fund_name, as_of.isoformat(), source_id,
                 source_locator, str(cost) if cost is not None else None,
                 str(pct) if pct is not None else None,
                 str(value) if value is not None else None, actor_id, now, now),
            ).fetchone()
            self._append_audit_event(
                connection, project_id=project_id, action="holding_snapshot.proposed",
                actor_kind="verified_user" if actor_id else "unverified_local_user",
                actor_ref=actor_id, channel="team_web" if actor_id else "preview_api",
                subject_id=snapshot_id, source_id=source_id,
                source_sha256=source["content_sha256"], source_locator=source_locator,
            )
            return self._holding_snapshot(row)

    def review_holding_snapshot(
        self,
        project_id: str,
        snapshot_id: str,
        *,
        action: str,
        expected_version: int,
        reviewer: str,
        reviewer_verified: bool = False,
        note: str | None = None,
    ) -> HoldingSnapshot:
        if action not in {"confirm", "reject"}:
            raise ValueError("invalid holding review action")
        reviewer = reviewer.strip()
        if not 1 <= len(reviewer) <= 200:
            raise ValueError("reviewer must contain 1-200 characters")
        note = note.strip() if note else None
        if note and len(note) > 500:
            raise ValueError("holding review note must contain at most 500 characters")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            existing = connection.execute(
                "SELECT * FROM holding_snapshots WHERE id = ? AND project_id = ?",
                (snapshot_id, project_id),
            ).fetchone()
            if existing is None:
                raise NotFoundError("holding snapshot not found")
            if existing["status"] != "candidate" or existing["version"] != expected_version:
                raise ReviewConflictError("holding snapshot is stale or already reviewed")
            if existing["proposed_by"] and existing["proposed_by"] == reviewer:
                raise ValueError("holding snapshot proposer cannot review own proposal")
            reviewed_status = "confirmed" if action == "confirm" else "rejected"
            row = connection.execute(
                """UPDATE holding_snapshots SET status = ?, version = version + 1,
                   reviewed_by = ?, review_note = ?, updated_at = ? WHERE id = ? RETURNING *""",
                (reviewed_status, reviewer, note, datetime.now(UTC).isoformat(), snapshot_id),
            ).fetchone()
            source = connection.execute(
                "SELECT content_sha256 FROM sources WHERE id = ?", (row["source_id"],)
            ).fetchone()
            self._append_audit_event(
                connection, project_id=project_id,
                action=f"holding_snapshot.{reviewed_status}",
                actor_kind="verified_user" if reviewer_verified else "unverified_local_user",
                actor_ref=reviewer, channel="team_web" if reviewer_verified else "preview_api",
                subject_id=snapshot_id, source_id=row["source_id"],
                source_sha256=source["content_sha256"], source_locator=row["source_locator"],
            )
            return self._holding_snapshot(row)

    @staticmethod
    def _watch_item(row: sqlite3.Row) -> WatchItem:
        return WatchItem(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            observation_id=str(row["observation_id"]),
            current_observation_id=str(row["current_observation_id"]),
            title=str(row["title"]),
            question=str(row["question"]),
            owner=row["owner"],
            due_on=date.fromisoformat(row["due_on"]) if row["due_on"] else None,
            status=row["status"],
            version=int(row["version"]),
            proposed_by=row["proposed_by"],
            reviewed_by=row["reviewed_by"],
            review_note=row["review_note"],
            initial_question=str(row["initial_question"]),
            initial_owner=row["initial_owner"],
            initial_due_on=date.fromisoformat(row["initial_due_on"])
            if row["initial_due_on"]
            else None,
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_watch_items(self, project_id: str) -> list[WatchItem]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM watch_items WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return [self._watch_item(row) for row in rows]

    @staticmethod
    def _watch_change(row: sqlite3.Row) -> WatchItemChange:
        return WatchItemChange(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            watch_item_id=str(row["watch_item_id"]),
            base_version=int(row["base_version"]),
            target_status=row["target_status"],
            question=str(row["question"]),
            owner=row["owner"],
            due_on=date.fromisoformat(row["due_on"]) if row["due_on"] else None,
            evidence_observation_id=row["evidence_observation_id"],
            reason=str(row["reason"]),
            status=row["status"],
            proposed_by=row["proposed_by"],
            reviewed_by=row["reviewed_by"],
            review_note=row["review_note"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_watch_item_changes(self, project_id: str) -> list[WatchItemChange]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM watch_item_changes WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return [self._watch_change(row) for row in rows]

    def propose_watch_item(
        self,
        project_id: str,
        *,
        observation_id: str,
        title: str,
        question: str,
        owner: str | None = None,
        due_on: date | None = None,
        actor_id: str | None = None,
    ) -> WatchItem:
        title, title_key, question, owner, due = watch_item_input(title, question, owner, due_on)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            observation = connection.execute(
                """SELECT o.status, o.source_id, o.source_locator, s.content_sha256
                   FROM observations o JOIN sources s ON s.id = o.source_id
                   WHERE o.id = ? AND o.project_id = ? AND s.project_id = ?""",
                (observation_id, project_id, project_id),
            ).fetchone()
            if observation is None:
                raise NotFoundError("confirmed observation not found in project")
            if observation["status"] != "confirmed" or not observation["source_locator"]:
                raise ValueError("watch item requires a confirmed, located observation")
            existing = connection.execute(
                """SELECT * FROM watch_items
                   WHERE project_id = ? AND observation_id = ? AND title_key = ?""",
                (project_id, observation_id, title_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["question"] != question
                    or existing["owner"] != owner
                    or existing["due_on"] != due
                ):
                    raise ReviewConflictError("watch item already exists with different details")
                return self._watch_item(existing)
            item_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """INSERT INTO watch_items
                   (id, project_id, observation_id, title, title_key, question, owner, due_on,
                    status, version, proposed_by, reviewed_by, review_note, created_at, updated_at,
                    current_observation_id, initial_question, initial_owner, initial_due_on)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', 1, ?, NULL, NULL, ?, ?,
                           ?, ?, ?, ?)""",
                (
                    item_id,
                    project_id,
                    observation_id,
                    title,
                    title_key,
                    question,
                    owner,
                    due,
                    actor_id,
                    now,
                    now,
                    observation_id,
                    question,
                    owner,
                    due,
                ),
            )
            connection.execute(
                """INSERT INTO watch_item_events
                   (id, watch_item_id, project_id, version, from_status, to_status,
                    actor_ref, note, created_at)
                   VALUES (?, ?, ?, 1, NULL, 'candidate', ?, NULL, ?)""",
                (str(uuid.uuid4()), item_id, project_id, actor_id, now),
            )
            self._append_audit_event(
                connection,
                project_id=project_id,
                action="watch_item.proposed",
                actor_kind="verified_user" if actor_id else "unverified_local_user",
                actor_ref=actor_id,
                channel="team_web" if actor_id else "preview_api",
                subject_id=item_id,
                source_id=observation["source_id"],
                source_sha256=observation["content_sha256"],
                source_locator=observation["source_locator"],
            )
            row = connection.execute(
                "SELECT * FROM watch_items WHERE id = ?", (item_id,)
            ).fetchone()
            return self._watch_item(row)

    def review_watch_item(
        self,
        project_id: str,
        item_id: str,
        *,
        action: str,
        expected_version: int,
        reviewer: str,
        reviewer_verified: bool = False,
        note: str | None = None,
    ) -> WatchItem:
        status, reviewer, note = watch_review_input(action, reviewer, note)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            row = connection.execute(
                """SELECT w.*, o.source_id, o.source_locator, s.content_sha256
                   FROM watch_items w JOIN observations o ON o.id = w.observation_id
                   JOIN sources s ON s.id = o.source_id
                   WHERE w.id = ? AND w.project_id = ?""",
                (item_id, project_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("watch item not found")
            if row["status"] != "candidate" or row["version"] != expected_version:
                raise ReviewConflictError("watch item is stale or already reviewed")
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """UPDATE watch_items SET status = ?, version = version + 1,
                   reviewed_by = ?, review_note = ?, updated_at = ? WHERE id = ?""",
                (status, reviewer, note, now, item_id),
            )
            connection.execute(
                """INSERT INTO watch_item_events
                   (id, watch_item_id, project_id, version, from_status, to_status,
                    actor_ref, note, created_at)
                   VALUES (?, ?, ?, ?, 'candidate', ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    item_id,
                    project_id,
                    expected_version + 1,
                    status,
                    reviewer,
                    note,
                    now,
                ),
            )
            self._append_audit_event(
                connection,
                project_id=project_id,
                action=f"watch_item.{status}",
                actor_kind="verified_user" if reviewer_verified else "unverified_local_user",
                actor_ref=reviewer,
                channel="team_web" if reviewer_verified else "preview_api",
                subject_id=item_id,
                source_id=row["source_id"],
                source_sha256=row["content_sha256"],
                source_locator=row["source_locator"],
            )
            updated = connection.execute(
                "SELECT * FROM watch_items WHERE id = ?", (item_id,)
            ).fetchone()
            return self._watch_item(updated)

    def propose_watch_item_change(
        self,
        project_id: str,
        item_id: str,
        *,
        expected_version: int,
        target_status: str,
        question: str,
        owner: str | None,
        due_on: date | None,
        reason: str,
        evidence_observation_id: str | None = None,
        actor_id: str | None = None,
    ) -> WatchItemChange:
        target_status, question, owner, due, reason = watch_change_input(
            target_status, question, owner, due_on, reason
        )
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            item = connection.execute(
                "SELECT * FROM watch_items WHERE id = ? AND project_id = ?",
                (item_id, project_id),
            ).fetchone()
            if item is None:
                raise NotFoundError("watch item not found")
            if item["status"] not in {"watching", "action_required"}:
                raise ReviewConflictError("watch item is not active")
            if item["version"] != expected_version:
                raise ReviewConflictError("watch item version changed")
            if item["resolved_at"] and target_status == "resolved":
                raise ValueError("resolved watch item must be reopened")
            evidence = connection.execute(
                """SELECT o.status, o.source_locator, o.source_id, s.content_sha256
                   FROM observations o JOIN sources s ON s.id = o.source_id
                   WHERE o.id = ? AND o.project_id = ? AND s.project_id = ?""",
                (evidence_observation_id or item["current_observation_id"], project_id, project_id),
            ).fetchone()
            if evidence is None:
                raise NotFoundError("change evidence not found in project")
            if evidence["status"] != "confirmed" or not evidence["source_locator"]:
                raise ValueError("change evidence must be confirmed and located")
            if (
                not item["resolved_at"]
                and target_status == item["status"]
                and question == item["question"]
                and owner == item["owner"]
                and due == item["due_on"]
                and not evidence_observation_id
            ):
                raise ValueError("watch change does not alter the item or evidence")
            pending = connection.execute(
                """SELECT * FROM watch_item_changes
                   WHERE watch_item_id = ? AND project_id = ? AND status = 'candidate'""",
                (item_id, project_id),
            ).fetchone()
            if pending is not None:
                if all(
                    pending[key] == value
                    for key, value in {
                        "base_version": expected_version,
                        "target_status": target_status,
                        "question": question,
                        "owner": owner,
                        "due_on": due,
                        "reason": reason,
                        "evidence_observation_id": evidence_observation_id,
                    }.items()
                ):
                    return self._watch_change(pending)
                raise ReviewConflictError("another watch change is awaiting review")
            change_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """INSERT INTO watch_item_changes
                   (id, project_id, watch_item_id, base_version, target_status, question, owner,
                    due_on, evidence_observation_id, reason, status, proposed_by, reviewed_by,
                    review_note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, NULL, NULL, ?, ?)""",
                (
                    change_id,
                    project_id,
                    item_id,
                    expected_version,
                    target_status,
                    question,
                    owner,
                    due,
                    evidence_observation_id,
                    reason,
                    actor_id,
                    now,
                    now,
                ),
            )
            self._append_audit_event(
                connection,
                project_id=project_id,
                action="watch_item.change_proposed",
                actor_kind="verified_user" if actor_id else "unverified_local_user",
                actor_ref=actor_id,
                channel="team_web" if actor_id else "preview_api",
                subject_id=item_id,
                source_id=evidence["source_id"],
                source_sha256=evidence["content_sha256"],
                source_locator=evidence["source_locator"],
            )
            row = connection.execute(
                "SELECT * FROM watch_item_changes WHERE id = ?", (change_id,)
            ).fetchone()
            return self._watch_change(row)

    def review_watch_item_change(
        self,
        project_id: str,
        item_id: str,
        change_id: str,
        *,
        action: str,
        expected_version: int,
        reviewer: str,
        reviewer_verified: bool = False,
        note: str | None = None,
    ) -> WatchItem:
        action, reviewer, note = watch_change_review_input(action, reviewer, note)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            item = connection.execute(
                "SELECT * FROM watch_items WHERE id = ? AND project_id = ?",
                (item_id, project_id),
            ).fetchone()
            if item is None:
                raise NotFoundError("watch item not found")
            change = connection.execute(
                """SELECT * FROM watch_item_changes
                   WHERE id = ? AND watch_item_id = ? AND project_id = ?""",
                (change_id, item_id, project_id),
            ).fetchone()
            if change is None:
                raise NotFoundError("watch change not found")
            if (
                change["status"] != "candidate"
                or item["version"] != expected_version
                or change["base_version"] != expected_version
            ):
                raise ReviewConflictError("watch change is stale or already reviewed")
            now = datetime.now(UTC).isoformat()
            if action == "accept":
                previous = "resolved" if item["resolved_at"] else item["status"]
                next_status = (
                    item["status"]
                    if change["target_status"] == "resolved"
                    else change["target_status"]
                )
                connection.execute(
                    """UPDATE watch_items SET status = ?, question = ?, owner = ?, due_on = ?,
                       current_observation_id = COALESCE(?, current_observation_id),
                       resolved_at = ?, version = version + 1, reviewed_by = ?, review_note = ?,
                       updated_at = ? WHERE id = ?""",
                    (
                        next_status,
                        change["question"],
                        change["owner"],
                        change["due_on"],
                        change["evidence_observation_id"],
                        now if change["target_status"] == "resolved" else None,
                        reviewer,
                        note,
                        now,
                        item_id,
                    ),
                )
                connection.execute(
                    """INSERT INTO watch_item_events
                       (id, watch_item_id, project_id, version, from_status, to_status,
                        actor_ref, note, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        item_id,
                        project_id,
                        expected_version + 1,
                        previous,
                        change["target_status"],
                        reviewer,
                        note,
                        now,
                    ),
                )
            connection.execute(
                """UPDATE watch_item_changes SET status = ?, reviewed_by = ?, review_note = ?,
                   updated_at = ? WHERE id = ?""",
                ("accepted" if action == "accept" else "rejected", reviewer, note, now, change_id),
            )
            evidence = connection.execute(
                """SELECT o.source_id, o.source_locator, s.content_sha256
                   FROM observations o JOIN sources s ON s.id = o.source_id
                   WHERE o.id = ? AND o.project_id = ? AND s.project_id = ?""",
                (
                    change["evidence_observation_id"] or item["current_observation_id"],
                    project_id,
                    project_id,
                ),
            ).fetchone()
            self._append_audit_event(
                connection,
                project_id=project_id,
                action=f"watch_item.change_{action}",
                actor_kind="verified_user" if reviewer_verified else "unverified_local_user",
                actor_ref=reviewer,
                channel="team_web" if reviewer_verified else "preview_api",
                subject_id=item_id,
                source_id=evidence["source_id"],
                source_sha256=evidence["content_sha256"],
                source_locator=evidence["source_locator"],
            )
            updated = connection.execute(
                "SELECT * FROM watch_items WHERE id = ?", (item_id,)
            ).fetchone()
            return self._watch_item(updated)

    def propose_competitive_peer(
        self,
        project_id: str,
        *,
        name: str,
        linked_project_id: str | None = None,
        actor_id: str | None = None,
    ) -> CompetitivePeer:
        normalized = normalized_project_name(name)
        if len(normalized) > 200:
            raise ValueError("peer name must contain at most 200 characters")
        if linked_project_id == project_id:
            raise ValueError("project cannot be its own peer")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            if linked_project_id:
                self._require_project(connection, linked_project_id)
            existing = connection.execute(
                "SELECT * FROM competitive_peers WHERE project_id = ? AND name_key = ?",
                (project_id, normalized.casefold()),
            ).fetchone()
            if existing is not None:
                if existing["linked_project_id"] is None and linked_project_id:
                    connection.execute(
                        """UPDATE competitive_peers SET linked_project_id = ?, status = 'candidate',
                           version = version + 1, proposed_by = ?, reviewed_by = NULL,
                           note = NULL, updated_at = ? WHERE id = ?""",
                        (
                            linked_project_id,
                            actor_id,
                            datetime.now(UTC).isoformat(),
                            existing["id"],
                        ),
                    )
                    self._append_audit_event(
                        connection,
                        project_id=project_id,
                        action="competitive_peer.link_proposed",
                        actor_kind="verified_user" if actor_id else "unverified_local_user",
                        actor_ref=actor_id,
                        channel="team_web" if actor_id else "preview_api",
                        subject_id=str(existing["id"]),
                        source_id=None,
                        source_sha256=None,
                        source_locator=None,
                    )
                    updated = connection.execute(
                        "SELECT * FROM competitive_peers WHERE id = ?", (existing["id"],)
                    ).fetchone()
                    return self._competitive_peer(updated)
                if existing["linked_project_id"] != linked_project_id:
                    raise ValueError("peer name already has a different project link")
                return self._competitive_peer(existing)
            peer_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """INSERT INTO competitive_peers
                   (id, project_id, name, name_key, linked_project_id, status, version,
                    proposed_by, reviewed_by, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'candidate', 1, ?, NULL, NULL, ?, ?)""",
                (
                    peer_id,
                    project_id,
                    normalized,
                    normalized.casefold(),
                    linked_project_id,
                    actor_id,
                    now,
                    now,
                ),
            )
            self._append_audit_event(
                connection,
                project_id=project_id,
                action="competitive_peer.proposed",
                actor_kind="verified_user" if actor_id else "unverified_local_user",
                actor_ref=actor_id,
                channel="team_web" if actor_id else "preview_api",
                subject_id=peer_id,
                source_id=None,
                source_sha256=None,
                source_locator=None,
            )
            row = connection.execute(
                "SELECT * FROM competitive_peers WHERE id = ?", (peer_id,)
            ).fetchone()
            return self._competitive_peer(row)

    def review_competitive_peer(
        self,
        project_id: str,
        peer_id: str,
        *,
        action: str,
        expected_version: int,
        reviewer: str,
        reviewer_verified: bool = False,
        note: str | None = None,
    ) -> CompetitivePeer:
        if action not in {"confirm", "reject"}:
            raise ValueError("invalid peer review action")
        reviewer = reviewer.strip()
        if not reviewer or len(reviewer) > 200:
            raise ValueError("reviewer must contain 1-200 characters")
        note = note.strip() if note else None
        if note and len(note) > 500:
            raise ValueError("peer review note must contain at most 500 characters")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM competitive_peers WHERE id = ? AND project_id = ?",
                (peer_id, project_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("competitive peer not found")
            if row["status"] != "candidate" or row["version"] != expected_version:
                raise ReviewConflictError("competitive peer is stale or already reviewed")
            status = "confirmed" if action == "confirm" else "rejected"
            connection.execute(
                """UPDATE competitive_peers SET status = ?, version = version + 1,
                   reviewed_by = ?, note = ?, updated_at = ? WHERE id = ?""",
                (status, reviewer, note, datetime.now(UTC).isoformat(), peer_id),
            )
            self._append_audit_event(
                connection,
                project_id=project_id,
                action=f"competitive_peer.{status}",
                actor_kind="verified_user" if reviewer_verified else "unverified_local_user",
                actor_ref=reviewer,
                channel="team_web" if reviewer_verified else "preview_api",
                subject_id=peer_id,
                source_id=None,
                source_sha256=None,
                source_locator=None,
            )
            updated = connection.execute(
                "SELECT * FROM competitive_peers WHERE id = ?", (peer_id,)
            ).fetchone()
            return self._competitive_peer(updated)

    @staticmethod
    def _identity_review(row: sqlite3.Row) -> IdentityReview:
        return IdentityReview(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            version=int(row["version"]),
            action=row["action"],
            legal_name=row["legal_name"],
            registration_code=row["registration_code"],
            evidence_source_id=row["evidence_source_id"],
            evidence_locator=row["evidence_locator"],
            evidence_url=row["evidence_url"],
            note=str(row["note"]),
            actor_ref=str(row["actor_ref"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_identity_review(self, project_id: str) -> IdentityReview | None:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                """SELECT * FROM project_identity_reviews WHERE project_id = ?
                   ORDER BY version DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            return self._identity_review(row) if row else None

    def list_identity_reviews(self, project_id: str, *, limit: int = 50) -> list[IdentityReview]:
        if not 1 <= limit <= 100:
            raise ValueError("identity history limit must be between 1 and 100")
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM project_identity_reviews WHERE project_id = ?
                   ORDER BY version DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
            return [self._identity_review(row) for row in rows]

    @staticmethod
    def _identity_version(connection: sqlite3.Connection, project_id: str) -> int:
        row = connection.execute(
            "SELECT MAX(version) AS version FROM project_identity_reviews WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row["version"] or 0)

    def verify_project_identity(
        self,
        project_id: str,
        *,
        legal_name: str,
        registration_code: str | None,
        evidence_source_id: str | None,
        evidence_locator: str | None,
        evidence_url: str | None,
        note: str,
        expected_version: int,
        actor_user_id: str,
    ) -> IdentityReview:
        if not actor_user_id or expected_version < 0:
            raise ValueError("verified reviewer and revision are required")
        details = verified_identity_input(
            legal_name=legal_name,
            registration_code=registration_code,
            evidence_source_id=evidence_source_id,
            evidence_locator=evidence_locator,
            evidence_url=evidence_url,
            note=note,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            version = self._identity_version(connection, project_id)
            if version != expected_version:
                raise IdentityConflictError("project identity review changed")
            source_hash = None
            if details.evidence_source_id:
                source = connection.execute(
                    """SELECT content_sha256 FROM sources
                       WHERE id = ? AND project_id = ? AND kind = 'document'""",
                    (details.evidence_source_id, project_id),
                ).fetchone()
                if (
                    source is None
                    or connection.execute(
                        """SELECT 1 FROM document_fragments WHERE source_id = ?
                       AND project_id = ? AND locator = ? LIMIT 1""",
                        (details.evidence_source_id, project_id, details.evidence_locator),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("identity evidence does not belong to the selected project")
                source_hash = str(source["content_sha256"])
            review_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO project_identity_reviews
                   (id, project_id, version, action, legal_name, registration_code,
                    evidence_source_id, evidence_locator, evidence_url, note, actor_ref, created_at)
                   VALUES (?, ?, ?, 'verify', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id,
                    project_id,
                    version + 1,
                    details.legal_name,
                    details.registration_code,
                    details.evidence_source_id,
                    details.evidence_locator,
                    details.evidence_url,
                    details.note,
                    actor_user_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute(
                "UPDATE projects SET identity_status = 'verified' WHERE id = ?", (project_id,)
            )
            self._append_audit_event(
                connection,
                project_id=project_id,
                action="identity.verified",
                actor_kind="verified_user",
                actor_ref=actor_user_id,
                channel="team_web",
                subject_id=review_id,
                source_id=details.evidence_source_id,
                source_sha256=source_hash,
                source_locator=details.evidence_locator,
            )
            row = connection.execute(
                "SELECT * FROM project_identity_reviews WHERE id = ?", (review_id,)
            ).fetchone()
            return self._identity_review(row)

    def reopen_project_identity(
        self, project_id: str, *, note: str, expected_version: int, actor_user_id: str
    ) -> IdentityReview:
        if not actor_user_id or expected_version < 0:
            raise ValueError("verified reviewer and revision are required")
        reason = review_note(note)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute(
                "SELECT identity_status FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise NotFoundError("project not found")
            version = self._identity_version(connection, project_id)
            if version != expected_version:
                raise IdentityConflictError("project identity review changed")
            if project["identity_status"] != "verified":
                raise ValueError("only a verified identity can be reopened")
            review_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO project_identity_reviews
                   (id, project_id, version, action, note, actor_ref, created_at)
                   VALUES (?, ?, ?, 'reopen', ?, ?, ?)""",
                (
                    review_id,
                    project_id,
                    version + 1,
                    reason,
                    actor_user_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute(
                "UPDATE projects SET identity_status = 'unverified' WHERE id = ?", (project_id,)
            )
            self._append_audit_event(
                connection,
                project_id=project_id,
                action="identity.reopened",
                actor_kind="verified_user",
                actor_ref=actor_user_id,
                channel="team_web",
                subject_id=review_id,
                source_id=None,
                source_sha256=None,
                source_locator=None,
            )
            row = connection.execute(
                "SELECT * FROM project_identity_reviews WHERE id = ?", (review_id,)
            ).fetchone()
            return self._identity_review(row)

    @staticmethod
    def _source(row: sqlite3.Row) -> SourceVersion:
        keys = row.keys()
        return SourceVersion(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            kind=str(row["kind"]),
            reference=str(row["reference"]),
            content_sha256=str(row["content_sha256"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            topic=row["topic"] if "topic" in keys else None,
            topic_basis=row["topic_basis"] if "topic_basis" in keys else None,
        )

    @staticmethod
    def _review_impact(row: sqlite3.Row) -> ReviewImpact:
        return ReviewImpact(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            trigger_source_id=str(row["trigger_source_id"]),
            observation_id=str(row["observation_id"]),
            topic=str(row["topic"]),
            status=row["status"],
            resolution=row["resolution"],
            note=row["note"],
            reviewer_ref=row["reviewer_ref"],
            created_at=datetime.fromisoformat(row["created_at"]),
            reviewed_at=(
                datetime.fromisoformat(row["reviewed_at"])
                if row["reviewed_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _observation(row: sqlite3.Row) -> Observation:
        return Observation(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            source_id=str(row["source_id"]),
            subject=str(row["subject"]),
            statement=str(row["statement"]),
            number=Decimal(row["number"]) if row["number"] is not None else None,
            unit=row["unit"],
            period=row["period"],
            scope=row["scope"],
            source_locator=str(row["source_locator"]),
            status=row["status"],
            version=int(row["version"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            basis=row["basis"],
        )

    @staticmethod
    def _require_project(connection: sqlite3.Connection, project_id: str) -> None:
        if (
            connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
            is None
        ):
            raise NotFoundError("project not found")

    def add_source(
        self, project_id: str, *, kind: str, reference: str, content_sha256: str,
        db: sqlite3.Connection | None = None,
    ) -> SourceVersion:
        kind = kind.strip()
        reference = reference.strip()
        content_sha256 = content_sha256.lower().strip()
        if kind not in {"document", "url", "note"}:
            raise ValueError("source kind must be document, url, or note")
        if not reference or len(reference) > 500:
            raise ValueError("source reference must contain 1-500 characters")
        if re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None:
            raise ValueError("source content_sha256 must be a SHA-256 hex digest")
        with (closing(self._connect()) if db is None else nullcontext(db)) as connection, (connection if db is None else nullcontext()):
            if db is None:
                connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            existing = connection.execute(
                "SELECT * FROM sources WHERE project_id = ? AND content_sha256 = ?",
                (project_id, content_sha256),
            ).fetchone()
            if existing is not None:
                return self._source(existing)
            source = SourceVersion(
                id=str(uuid.uuid4()),
                project_id=project_id,
                kind=kind,
                reference=reference,
                content_sha256=content_sha256,
                created_at=datetime.now(UTC),
            )
            connection.execute(
                """INSERT INTO sources
                   (id, project_id, kind, reference, content_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    source.id,
                    project_id,
                    kind,
                    reference,
                    content_sha256,
                    source.created_at.isoformat(),
                ),
            )
            return source

    def list_sources(self, project_id: str) -> list[SourceVersion]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT s.*, di.topic, di.basis AS topic_basis FROM sources s
                   LEFT JOIN document_index di ON di.source_id = s.id AND di.project_id = s.project_id
                   WHERE s.project_id = ? ORDER BY s.created_at, s.id""",
                (project_id,),
            ).fetchall()
            return [self._source(row) for row in rows]

    @staticmethod
    def _execution_task(row: sqlite3.Row) -> ExecutionTask:
        return ExecutionTask(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            contract_version=int(row["contract_version"]),
            kind=row["kind"],
            mode=row["mode"],
            status=row["status"],
            source_ids=tuple(json.loads(row["source_ids_json"])),
            outcome=json.loads(row["outcome_json"]),
            idempotency_key=str(row["idempotency_key"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_execution_task(self, project_id: str, task_id: str) -> ExecutionTask | None:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM execution_tasks WHERE project_id = ? AND id = ?",
                (project_id, task_id),
            ).fetchone()
            return self._execution_task(row) if row is not None else None

    def list_execution_tasks(self, project_id: str, *, limit: int = 20) -> list[ExecutionTask]:
        if not 1 <= limit <= 100:
            raise ValueError("task list limit must be between 1 and 100")
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM execution_tasks WHERE project_id = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
            return [self._execution_task(row) for row in rows]

    def save_coverage_task(
        self,
        project_id: str,
        *,
        source_ids: tuple[str, ...],
        idempotency_key: str,
        outcome: dict[str, object],
    ) -> ExecutionTask:
        if len(source_ids) > 100 or len(set(source_ids)) != len(source_ids):
            raise ValueError("coverage task accepts at most 100 distinct source IDs")
        if not idempotency_key or len(idempotency_key) > 180:
            raise ValueError("invalid task idempotency key")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            existing = connection.execute(
                "SELECT * FROM execution_tasks WHERE project_id = ? AND idempotency_key = ?",
                (project_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                task = self._execution_task(existing)
                if task.kind != "coverage" or task.source_ids != source_ids:
                    raise ValueError("task idempotency key reused for a different request")
                return task
            local_sources = connection.execute(
                "SELECT id FROM sources WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            local_ids = {str(row["id"]) for row in local_sources}
            if any(source_id not in local_ids for source_id in source_ids):
                raise ValueError("source belongs to another project or does not exist")
            task_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO execution_tasks
                   (id, project_id, contract_version, kind, mode, status, source_ids_json,
                    outcome_json, idempotency_key, created_at)
                   VALUES (?, ?, 1, 'coverage', 'native', 'completed', ?, ?, ?, ?)""",
                (
                    task_id,
                    project_id,
                    json.dumps(source_ids),
                    json.dumps(outcome, ensure_ascii=False),
                    idempotency_key,
                    datetime.now(UTC).isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM execution_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return self._execution_task(row)

    def add_observation(
        self,
        project_id: str,
        *,
        source_id: str,
        subject: str,
        statement: str,
        source_locator: str,
        number: Decimal | None = None,
        unit: str | None = None,
        period: str | None = None,
        scope: str | None = None,
        basis: str = "unknown",
        submission_channel: str = "preview_api",
        actor_id: str | None = None,
    ) -> Observation:
        if submission_channel not in {"preview_api", "http", "mcp", "team_web", "team_mcp"}:
            raise ValueError("invalid observation submission channel")
        if submission_channel in {"team_web", "team_mcp"} and not actor_id:
            raise ValueError("team proposal requires a verified actor")
        subject = subject.strip()
        statement = statement.strip()
        source_locator = source_locator.strip()
        unit = unit.strip() if unit else None
        period = period.strip() if period else None
        scope = scope.strip() if scope else None
        if basis not in {"actual", "forecast", "budget", "unknown"}:
            raise ValueError("invalid measurement basis")
        if number is None and basis != "unknown":
            raise ValueError("text observation cannot have a measurement basis")
        if not subject or len(subject) > 80:
            raise ValueError("observation subject must contain 1-80 characters")
        if not statement or len(statement) > 5000:
            raise ValueError("observation statement must contain 1-5000 characters")
        if not source_locator or len(source_locator) > 200:
            raise ValueError("source locator must contain 1-200 characters")
        if number is not None:
            number = Decimal(number)
            if not number.is_finite():
                raise ValueError("numeric observation must be finite")
            if not unit or not period:
                raise ValueError("numeric observation requires period and unit")
        fingerprint = self._observation_fingerprint(
            source_id=source_id,
            subject=subject,
            statement=statement,
            number=str(number) if number is not None else None,
            unit=unit,
            period=period,
            scope=scope,
            source_locator=source_locator,
            basis=basis,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            source = connection.execute(
                "SELECT project_id, content_sha256 FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if source is None:
                raise NotFoundError("source not found")
            if source["project_id"] != project_id:
                raise ValueError("source belongs to another project")
            existing = connection.execute(
                "SELECT * FROM observations WHERE project_id = ? AND fingerprint = ?",
                (project_id, fingerprint),
            ).fetchone()
            if existing is not None:
                return self._observation(existing)
            observation = Observation(
                id=str(uuid.uuid4()),
                project_id=project_id,
                source_id=source_id,
                subject=subject,
                statement=statement,
                number=number,
                unit=unit,
                period=period,
                scope=scope,
                source_locator=source_locator,
                status="candidate",
                version=1,
                created_at=datetime.now(UTC),
                basis=basis,
            )
            connection.execute(
                """INSERT INTO observations
                   (id, project_id, source_id, subject, statement, number, unit, period,
                    scope, source_locator, status, version, created_at, fingerprint, basis)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.id,
                    project_id,
                    source_id,
                    subject,
                    statement,
                    str(number) if number is not None else None,
                    unit,
                    period,
                    scope,
                    source_locator,
                    observation.status,
                    observation.version,
                    observation.created_at.isoformat(),
                    fingerprint,
                    basis,
                ),
            )
            self._append_audit_event(
                connection,
                project_id=project_id,
                action="candidate.created",
                actor_kind=(
                    "verified_user"
                    if submission_channel == "team_web"
                    else (
                        "verified_executor"
                        if submission_channel == "team_mcp"
                        else (
                            "unverified_executor"
                            if submission_channel in {"http", "mcp"}
                            else "unverified_local_user"
                        )
                    )
                ),
                actor_ref=actor_id,
                channel=submission_channel,
                subject_id=observation.id,
                source_id=source_id,
                source_sha256=str(source["content_sha256"]),
                source_locator=source_locator,
            )
            return observation

    def get_observation(self, observation_id: str) -> Observation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM observations WHERE id = ?", (observation_id,)
            ).fetchone()
            return self._observation(row) if row is not None else None

    def list_observations(
        self, project_id: str, *, status: ObservationStatus | None = None
    ) -> list[Observation]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            if status is not None and status not in {"candidate", "confirmed", "rejected"}:
                raise ValueError("invalid observation status")
            query = "SELECT * FROM observations WHERE project_id = ?"
            params: tuple[str, ...] = (project_id,)
            if status is not None:
                query += " AND status = ?"
                params += (status,)
            rows = connection.execute(query + " ORDER BY created_at, id", params).fetchall()
            return [self._observation(row) for row in rows]

    def confirm_observation(
        self,
        observation_id: str,
        *,
        reviewer: str,
        expected_version: int,
        reviewer_verified: bool = False,
    ) -> Observation:
        return self._review_observation(
            observation_id,
            action="confirm",
            reviewer=reviewer,
            expected_version=expected_version,
            reviewer_verified=reviewer_verified,
        )

    @staticmethod
    def _validate_batch_review(
        decisions: tuple[tuple[str, int, Literal["confirm", "reject"]], ...],
        reviewer: str,
    ) -> str:
        reviewer = reviewer.strip()
        if not reviewer or len(reviewer) > 200:
            raise ValueError("reviewer must contain 1-200 characters")
        if not 1 <= len(decisions) <= 20:
            raise ValueError("batch review requires 1-20 observations")
        if len({item[0] for item in decisions}) != len(decisions):
            raise ValueError("duplicate observation in batch review")
        if any(
            not item[0]
            or type(item[1]) is not int
            or item[1] < 1
            or item[2] not in {"confirm", "reject"}
            for item in decisions
        ):
            raise ValueError("invalid batch review decision")
        return reviewer

    def review_observations_batch(
        self,
        project_id: str,
        decisions: tuple[tuple[str, int, Literal["confirm", "reject"]], ...],
        *,
        reviewer: str,
        reviewer_verified: bool = False,
    ) -> list[Observation]:
        """Review up to 20 selected candidates in one transaction."""
        reviewer = self._validate_batch_review(decisions, reviewer)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            rows = []
            for observation_id, expected_version, _ in decisions:
                row = connection.execute(
                    "SELECT * FROM observations WHERE id = ? AND project_id = ?",
                    (observation_id, project_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("observation not found")
                if row["status"] != "candidate" or row["version"] != expected_version:
                    raise ReviewConflictError("observation is stale or already reviewed")
                rows.append(row)
            result = []
            for (observation_id, expected_version, action), original in zip(
                decisions, rows, strict=True
            ):
                connection.execute(
                    "UPDATE observations SET status = ?, version = version + 1 WHERE id = ?",
                    ("confirmed" if action == "confirm" else "rejected", observation_id),
                )
                connection.execute(
                    """INSERT INTO review_decisions
                       (id, observation_id, reviewer, action, expected_version, decided_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        observation_id,
                        reviewer,
                        action,
                        expected_version,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._append_audit_event(
                    connection,
                    project_id=project_id,
                    action="candidate.confirmed" if action == "confirm" else "candidate.rejected",
                    actor_kind="verified_user"
                    if reviewer_verified
                    else "unverified_reviewer_claim",
                    actor_ref=reviewer,
                    channel="team_web" if reviewer_verified else "preview_api",
                    subject_id=observation_id,
                    source_id=str(original["source_id"]),
                    source_sha256=None,
                    source_locator=str(original["source_locator"]),
                )
                updated = connection.execute(
                    "SELECT * FROM observations WHERE id = ?", (observation_id,)
                ).fetchone()
                result.append(self._observation(updated))
            return result

    def reject_observation(
        self,
        observation_id: str,
        *,
        reviewer: str,
        expected_version: int,
        reviewer_verified: bool = False,
    ) -> Observation:
        return self._review_observation(
            observation_id,
            action="reject",
            reviewer=reviewer,
            expected_version=expected_version,
            reviewer_verified=reviewer_verified,
        )

    def _review_observation(
        self,
        observation_id: str,
        *,
        action: Literal["confirm", "reject"],
        reviewer: str,
        expected_version: int,
        reviewer_verified: bool,
    ) -> Observation:
        reviewer = reviewer.strip()
        if not reviewer or len(reviewer) > 200:
            raise ValueError("reviewer must contain 1-200 characters")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM observations WHERE id = ?", (observation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("observation not found")
            if row["status"] != "candidate" or row["version"] != expected_version:
                raise ReviewConflictError("observation is stale or already reviewed")
            connection.execute(
                "UPDATE observations SET status = ?, version = version + 1 WHERE id = ?",
                ("confirmed" if action == "confirm" else "rejected", observation_id),
            )
            connection.execute(
                """INSERT INTO review_decisions
                   (id, observation_id, reviewer, action, expected_version, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    observation_id,
                    reviewer,
                    action,
                    expected_version,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._append_audit_event(
                connection,
                project_id=str(row["project_id"]),
                action="candidate.confirmed" if action == "confirm" else "candidate.rejected",
                actor_kind="verified_user" if reviewer_verified else "unverified_reviewer_claim",
                actor_ref=reviewer,
                channel="team_web" if reviewer_verified else "preview_api",
                subject_id=observation_id,
                source_id=str(row["source_id"]),
                source_sha256=None,
                source_locator=str(row["source_locator"]),
            )
            updated = connection.execute(
                "SELECT * FROM observations WHERE id = ?", (observation_id,)
            ).fetchone()
            return self._observation(updated)

    @staticmethod
    def _append_audit_event(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        action: str,
        actor_kind: str,
        actor_ref: str | None,
        channel: str,
        subject_id: str,
        source_id: str | None,
        source_sha256: str | None,
        source_locator: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events
               (id, project_id, action, actor_kind, actor_ref, channel, subject_id,
                source_id, source_sha256, source_locator, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                project_id,
                action,
                actor_kind,
                actor_ref,
                channel,
                subject_id,
                source_id,
                source_sha256,
                source_locator,
                datetime.now(UTC).isoformat(),
            ),
        )

    def list_audit_events(self, project_id: str, *, limit: int = 100) -> list[AuditEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("audit list limit must be between 1 and 500")
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM audit_events WHERE project_id = ?
                   ORDER BY created_at, rowid LIMIT ?""",
                (project_id, limit),
            ).fetchall()
            return [
                AuditEvent(
                    id=str(row["id"]),
                    project_id=str(row["project_id"]),
                    action=str(row["action"]),
                    actor_kind=str(row["actor_kind"]),
                    actor_ref=str(row["actor_ref"]) if row["actor_ref"] is not None else None,
                    channel=str(row["channel"]),
                    subject_id=str(row["subject_id"]),
                    source_id=str(row["source_id"]) if row["source_id"] is not None else None,
                    source_sha256=(
                        str(row["source_sha256"]) if row["source_sha256"] is not None else None
                    ),
                    source_locator=(
                        str(row["source_locator"]) if row["source_locator"] is not None else None
                    ),
                    created_at=datetime.fromisoformat(str(row["created_at"])),
                )
                for row in rows
            ]

    def find_audit_event(
        self, project_id: str, *, subject_id: str, action: str
    ) -> AuditEvent | None:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                """SELECT * FROM audit_events
                   WHERE project_id = ? AND subject_id = ? AND action = ?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (project_id, subject_id, action),
            ).fetchone()
        if row is None:
            return None
        return AuditEvent(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            action=str(row["action"]),
            actor_kind=str(row["actor_kind"]),
            actor_ref=row["actor_ref"],
            channel=str(row["channel"]),
            subject_id=str(row["subject_id"]),
            source_id=row["source_id"],
            source_sha256=row["source_sha256"],
            source_locator=row["source_locator"],
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def review_history(self, observation_id: str) -> list[ReviewDecision]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM review_decisions WHERE observation_id = ?
                   ORDER BY decided_at, id""",
                (observation_id,),
            ).fetchall()
            return [
                ReviewDecision(
                    id=str(row["id"]),
                    observation_id=str(row["observation_id"]),
                    reviewer=str(row["reviewer"]),
                    action=row["action"],
                    expected_version=int(row["expected_version"]),
                    decided_at=datetime.fromisoformat(row["decided_at"]),
                )
                for row in rows
            ]

    def review_decisions_for_report(
        self, project_id: str, observation_ids: list[str]
    ) -> list[ReviewDecision]:
        """Load only the visible report observations, scoped to one project."""
        ids = sorted(set(observation_ids))
        if len(ids) > 150:
            raise ValueError("too many report observations")
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            rows = connection.execute(
                f"""SELECT d.* FROM review_decisions d
                    JOIN observations o ON o.id = d.observation_id
                    WHERE o.project_id = ? AND o.id IN ({placeholders})
                      AND d.action = 'confirm'
                    ORDER BY d.decided_at, d.id""",
                (project_id, *ids),
            ).fetchall()
            return [
                ReviewDecision(
                    id=str(row["id"]),
                    observation_id=str(row["observation_id"]),
                    reviewer=str(row["reviewer"]),
                    action="confirm",
                    expected_version=int(row["expected_version"]),
                    decided_at=datetime.fromisoformat(str(row["decided_at"])),
                )
                for row in rows
            ]

    def report_locator_status(
        self, project_id: str, positions: list[tuple[str, str]]
    ) -> dict[tuple[str, str], str]:
        """Check exact extracted positions in one project without loading document text."""
        requested = sorted(set(positions))
        if len(requested) > 150:
            raise ValueError("too many report source positions")
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            if not requested:
                return {}
            values = ",".join("(?, ?)" for _ in requested)
            rows = connection.execute(
                f"""WITH requested(source_id, locator) AS (VALUES {values})
                    SELECT r.source_id, r.locator,
                      EXISTS(SELECT 1 FROM document_fragments f
                             WHERE f.project_id = ? AND f.source_id = r.source_id
                               AND f.locator = r.locator) AS matched,
                      EXISTS(SELECT 1 FROM document_fragments f
                             WHERE f.project_id = ? AND f.source_id = r.source_id) AS indexed
                    FROM requested r""",
                (*[part for pair in requested for part in pair], project_id, project_id),
            ).fetchall()
            return {
                (str(row["source_id"]), str(row["locator"])): (
                    "indexed_exact"
                    if row["matched"]
                    else "locator_not_found"
                    if row["indexed"]
                    else "not_indexed"
                )
                for row in rows
            }

    @staticmethod
    def _intake_job(row: sqlite3.Row) -> IntakeJob:
        return IntakeJob(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            source_id=str(row["source_id"]),
            status=row["status"],
            attempts=int(row["attempts"]),
            error_code=row["error_code"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_source(self, source_id: str) -> SourceVersion | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            return self._source(row) if row is not None else None

    def enqueue_intake(self, project_id: str, source_id: str, *, db: sqlite3.Connection | None = None) -> IntakeJob:
        with (closing(self._connect()) if db is None else nullcontext(db)) as connection, (connection if db is None else nullcontext()):
            if db is None:
                connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            source = connection.execute(
                "SELECT project_id, kind FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if source is None:
                raise NotFoundError("source not found")
            if source["project_id"] != project_id:
                raise ValueError("source belongs to another project")
            if source["kind"] != "document":
                raise ValueError("only document sources can be ingested")
            existing = connection.execute(
                "SELECT * FROM intake_jobs WHERE source_id = ?", (source_id,)
            ).fetchone()
            if existing is not None:
                return self._intake_job(existing)
            now = datetime.now(UTC)
            job = IntakeJob(
                id=str(uuid.uuid4()),
                project_id=project_id,
                source_id=source_id,
                status="queued",
                attempts=0,
                error_code=None,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """INSERT INTO intake_jobs
                   (id, project_id, source_id, status, attempts, error_code, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.id,
                    job.project_id,
                    job.source_id,
                    job.status,
                    job.attempts,
                    job.error_code,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
            return job

    def get_intake_job(self, job_id: str) -> IntakeJob | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM intake_jobs WHERE id = ?", (job_id,)).fetchone()
            return self._intake_job(row) if row is not None else None

    def list_intake(self, project_id: str) -> list[IntakeJob]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM intake_jobs WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return [self._intake_job(row) for row in rows]

    def claim_next_intake(self) -> IntakeJob | None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM intake_jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE intake_jobs SET status = 'processing', attempts = attempts + 1,
                   updated_at = ? WHERE id = ?""",
                (datetime.now(UTC).isoformat(), row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._intake_job(updated)

    def complete_intake(
        self,
        job_id: str,
        fragments: list[FragmentInput],
        *,
        needs_ocr: bool = False,
        native_candidates: tuple[NativeCandidate, ...] = (),
        native_financial_candidates: tuple[FinancialSuggestion, ...] = (),
        native_operating_candidates: tuple[OperatingSuggestion, ...] = (),
        native_topic: str | None = None,
        native_topic_basis: str | None = None,
    ) -> IntakeJob:
        if not fragments and not needs_ocr:
            raise ValueError("completed intake requires readable fragments")
        for fragment in fragments:
            if fragment.ordinal < 1:
                raise ValueError("fragment ordinal must be positive")
            if not fragment.locator.strip() or len(fragment.locator) > 200:
                raise ValueError("fragment locator must contain 1-200 characters")
            if not fragment.text.strip() or len(fragment.text) > 20_000:
                raise ValueError("fragment text must contain 1-20000 characters")
        if len({fragment.ordinal for fragment in fragments}) != len(fragments):
            raise ValueError("fragment ordinals must be unique")
        validate_native_candidates(native_candidates, fragments)
        self._validate_native_index(native_topic, native_topic_basis)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM intake_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("intake job not found")
            if row["status"] != "processing":
                raise IntakeConflictError("intake job is not processing")
            # Replace only after a successful retry; failed OCR preserves previous text.
            connection.execute("DELETE FROM document_fragments WHERE source_id = ?", (row["source_id"],))
            connection.execute("DELETE FROM document_index WHERE source_id = ?", (row["source_id"],))
            for fragment in fragments:
                connection.execute(
                    """INSERT INTO document_fragments
                       (id, project_id, source_id, job_id, ordinal, locator, text)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        row["project_id"],
                        row["source_id"],
                        job_id,
                        fragment.ordinal,
                        fragment.locator.strip(),
                        fragment.text.strip(),
                    ),
                )
            if native_topic is not None:
                connection.execute(
                    """INSERT INTO document_index
                       (source_id, project_id, topic, basis, rule_version, created_at)
                       VALUES (?, ?, ?, ?, 2, ?)""",
                    (
                        row["source_id"],
                        row["project_id"],
                        native_topic,
                        native_topic_basis,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if not needs_ocr:
                    topics = review_topics(native_topic, native_candidates)
                    if topics:
                        self._create_review_impacts(
                            connection, row["project_id"], row["source_id"], topics
                        )
            if native_candidates:
                source = connection.execute(
                    "SELECT content_sha256 FROM sources WHERE id = ?", (row["source_id"],)
                ).fetchone()
                for candidate in native_candidates:
                    fingerprint = self._observation_fingerprint(
                        source_id=row["source_id"],
                        subject=candidate.subject,
                        statement=candidate.statement,
                        number=None,
                        unit=None,
                        period=None,
                        scope=None,
                        source_locator=candidate.source_locator,
                    )
                    observation_id = str(uuid.uuid4())
                    created = connection.execute(
                        """INSERT INTO observations
                           (id, project_id, source_id, subject, statement, number, unit,
                            period, scope, source_locator, status, version, created_at, fingerprint)
                           VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, 'candidate', 1, ?, ?)
                           ON CONFLICT (project_id, fingerprint) DO NOTHING""",
                        (
                            observation_id,
                            row["project_id"],
                            row["source_id"],
                            candidate.subject,
                            candidate.statement,
                            candidate.source_locator,
                            datetime.now(UTC).isoformat(),
                            fingerprint,
                        ),
                    )
                    if created.rowcount:
                        self._append_audit_event(
                            connection,
                            project_id=row["project_id"],
                            action="candidate.created",
                            actor_kind="system",
                            actor_ref="native_rule_v1",
                            channel="native_intake",
                            subject_id=observation_id,
                            source_id=row["source_id"],
                            source_sha256=source["content_sha256"],
                            source_locator=candidate.source_locator,
                        )
            if native_financial_candidates:
                source = connection.execute(
                    "SELECT reference, content_sha256 FROM sources WHERE id = ?",
                    (row["source_id"],),
                ).fetchone()
                validate_financial_suggestions(
                    source["reference"], fragments, native_financial_candidates
                )
                for candidate in native_financial_candidates:
                    fingerprint = self._observation_fingerprint(
                        source_id=row["source_id"],
                        subject="财务表现",
                        statement=candidate.metric,
                        number=str(candidate.number),
                        unit=candidate.unit,
                        period=candidate.period,
                        scope=candidate.scope,
                        source_locator=candidate.source_locator,
                        basis=candidate.basis,
                    )
                    observation_id = str(uuid.uuid4())
                    created = connection.execute(
                        """INSERT INTO observations
                           (id, project_id, source_id, subject, statement, number, unit,
                            period, scope, source_locator, status, version, created_at,
                            fingerprint, basis)
                           VALUES (?, ?, ?, '财务表现', ?, ?, ?, ?, ?, ?, 'candidate', 1, ?, ?, ?)
                           ON CONFLICT (project_id, fingerprint) DO NOTHING""",
                        (
                            observation_id,
                            row["project_id"],
                            row["source_id"],
                            candidate.metric,
                            str(candidate.number),
                            candidate.unit,
                            candidate.period,
                            candidate.scope,
                            candidate.source_locator,
                            datetime.now(UTC).isoformat(),
                            fingerprint,
                            candidate.basis,
                        ),
                    )
                    if created.rowcount:
                        self._append_audit_event(
                            connection,
                            project_id=row["project_id"],
                            action="candidate.created",
                            actor_kind="system",
                            actor_ref="financial_map_v1",
                            channel="native_intake",
                            subject_id=observation_id,
                            source_id=row["source_id"],
                            source_sha256=source["content_sha256"],
                            source_locator=candidate.source_locator,
                        )
            if native_operating_candidates:
                source = connection.execute(
                    "SELECT reference, content_sha256 FROM sources WHERE id = ?",
                    (row["source_id"],),
                ).fetchone()
                validate_operating_suggestions(
                    source["reference"], fragments, native_operating_candidates
                )
                for candidate in native_operating_candidates:
                    fingerprint = self._observation_fingerprint(
                        source_id=row["source_id"],
                        subject="经营情况",
                        statement=candidate.metric,
                        number=str(candidate.number),
                        unit=candidate.unit,
                        period=candidate.period,
                        scope=candidate.scope,
                        source_locator=candidate.source_locator,
                        basis=candidate.basis,
                    )
                    observation_id = str(uuid.uuid4())
                    created = connection.execute(
                        """INSERT INTO observations
                           (id, project_id, source_id, subject, statement, number, unit,
                            period, scope, source_locator, status, version, created_at,
                            fingerprint, basis)
                           VALUES (?, ?, ?, '经营情况', ?, ?, ?, ?, ?, ?, 'candidate', 1, ?, ?, ?)
                           ON CONFLICT (project_id, fingerprint) DO NOTHING""",
                        (
                            observation_id,
                            row["project_id"],
                            row["source_id"],
                            candidate.metric,
                            str(candidate.number),
                            candidate.unit,
                            candidate.period,
                            candidate.scope,
                            candidate.source_locator,
                            datetime.now(UTC).isoformat(),
                            fingerprint,
                            candidate.basis,
                        ),
                    )
                    if created.rowcount:
                        self._append_audit_event(
                            connection,
                            project_id=row["project_id"],
                            action="candidate.created",
                            actor_kind="system",
                            actor_ref="operating_map_v1",
                            channel="native_intake",
                            subject_id=observation_id,
                            source_id=row["source_id"],
                            source_sha256=source["content_sha256"],
                            source_locator=candidate.source_locator,
                        )
            connection.execute(
                """UPDATE intake_jobs SET status = ?, error_code = NULL, updated_at = ?
                   WHERE id = ?""",
                ("needs_ocr" if needs_ocr else "completed", datetime.now(UTC).isoformat(), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._intake_job(updated)

    @staticmethod
    def _validate_native_index(topic: str | None, basis: str | None) -> None:
        if topic is None and basis is None:
            return
        if topic not in (*RESEARCH_TOPICS, "未分类") or basis not in (
            "filename",
            "content",
            "filename_and_content",
            "none",
        ):
            raise ValueError("invalid native document index")
        if (topic == "未分类") != (basis == "none"):
            raise ValueError("unclassified document index must have no matching basis")

    @staticmethod
    def _create_review_impacts(
        connection: sqlite3.Connection, project_id: str, source_id: str, topics: tuple[str, ...]
    ) -> None:
        now = datetime.now(UTC).isoformat()
        seen: set[str] = set()
        for topic in topics:
            observations = connection.execute(
                """SELECT o.id, o.subject FROM observations o
                   LEFT JOIN document_index old ON old.source_id = o.source_id
                     AND old.project_id = o.project_id
                   WHERE o.project_id = ? AND o.source_id <> ? AND o.status = 'confirmed'
                     AND (o.subject = ? OR old.topic = ?)""",
                (project_id, source_id, topic, topic),
            ).fetchall()
            for observation in observations:
                if observation["id"] in seen:
                    continue
                seen.add(observation["id"])
                connection.execute(
                    """INSERT INTO review_impacts
                       (id, project_id, trigger_source_id, observation_id, topic, status, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        str(uuid.uuid4()),
                        project_id,
                        source_id,
                        observation["id"],
                        impact_topic(observation["subject"], topic),
                        now,
                    ),
                )

    def list_review_impacts(self, project_id: str) -> list[ReviewImpact]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM review_impacts WHERE project_id = ?
                   ORDER BY created_at DESC, id DESC""",
                (project_id,),
            ).fetchall()
            return [self._review_impact(row) for row in rows]

    @staticmethod
    def _validate_impact_resolution(resolution: str, note: str, reviewer: str) -> tuple[str, str]:
        cleaned_note = note.strip()
        cleaned_reviewer = reviewer.strip()
        if resolution not in {"unchanged", "follow_up"}:
            raise ValueError("invalid impact resolution")
        if not 1 <= len(cleaned_note) <= 500:
            raise ValueError("impact review note must contain 1-500 characters")
        if not 1 <= len(cleaned_reviewer) <= 200:
            raise ValueError("impact reviewer must contain 1-200 characters")
        return cleaned_note, cleaned_reviewer

    def resolve_review_impact(
        self,
        project_id: str,
        impact_id: str,
        *,
        resolution: str,
        note: str,
        reviewer: str,
        reviewer_verified: bool = False,
    ) -> ReviewImpact:
        note, reviewer = self._validate_impact_resolution(resolution, note, reviewer)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM review_impacts WHERE id = ? AND project_id = ?",
                (impact_id, project_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("review impact not found")
            if row["status"] != "pending":
                raise ReviewConflictError("review impact already reviewed")
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """UPDATE review_impacts SET status = 'reviewed', resolution = ?, note = ?,
                   reviewer_ref = ?, reviewed_at = ? WHERE id = ?""",
                (resolution, note, reviewer, now, impact_id),
            )
            source = connection.execute(
                "SELECT content_sha256 FROM sources WHERE id = ? AND project_id = ?",
                (row["trigger_source_id"], project_id),
            ).fetchone()
            self._append_audit_event(
                connection,
                project_id=project_id,
                action="impact.reviewed",
                actor_kind="verified_user" if reviewer_verified else "unverified_reviewer_claim",
                actor_ref=reviewer,
                channel="team_web" if reviewer_verified else "preview_api",
                subject_id=impact_id,
                source_id=row["trigger_source_id"],
                source_sha256=source["content_sha256"],
                source_locator=None,
            )
            updated = connection.execute(
                "SELECT * FROM review_impacts WHERE id = ?", (impact_id,)
            ).fetchone()
            return self._review_impact(updated)

    def fail_intake(self, job_id: str, *, error_code: str) -> IntakeJob:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None:
            raise ValueError("invalid intake error code")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM intake_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("intake job not found")
            if row["status"] != "processing":
                raise IntakeConflictError("intake job is not processing")
            connection.execute(
                """UPDATE intake_jobs SET status = 'failed', error_code = ?, updated_at = ?
                   WHERE id = ?""",
                (error_code, datetime.now(UTC).isoformat(), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._intake_job(updated)

    def recover_interrupted_jobs(self) -> int:
        with closing(self._connect()) as connection, connection:
            result = connection.execute(
                """UPDATE intake_jobs SET status = 'queued', updated_at = ?
                   WHERE status = 'processing'""",
                (datetime.now(UTC).isoformat(),),
            )
            return result.rowcount

    def retry_failed_intake(self, job_id: str) -> IntakeJob:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM intake_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("intake job not found")
            if row["status"] not in ("failed", "needs_ocr"):
                raise IntakeConflictError("only failed or incomplete OCR intake jobs can be retried")
            connection.execute(
                """UPDATE intake_jobs SET status = 'queued', error_code = NULL, updated_at = ?
                   WHERE id = ?""",
                (datetime.now(UTC).isoformat(), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._intake_job(updated)

    def list_fragments(self, source_id: str) -> list[DocumentFragment]:
        with closing(self._connect()) as connection:
            if (
                connection.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone()
                is None
            ):
                raise NotFoundError("source not found")
            rows = connection.execute(
                """SELECT * FROM document_fragments WHERE source_id = ?
                   ORDER BY ordinal""",
                (source_id,),
            ).fetchall()
            return [
                DocumentFragment(
                    id=str(row["id"]),
                    project_id=str(row["project_id"]),
                    source_id=str(row["source_id"]),
                    job_id=str(row["job_id"]),
                    ordinal=int(row["ordinal"]),
                    locator=str(row["locator"]),
                    text=str(row["text"]),
                )
                for row in rows
            ]

    @staticmethod
    def _monitoring_plan(row: sqlite3.Row) -> MonitoringPlan:
        return MonitoringPlan(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            kind=row["kind"],
            target=str(row["target"]),
            cadence_minutes=int(row["cadence_minutes"]),
            enabled=bool(row["enabled"]),
            last_run_status=row["last_run_status"] or "not_run",
            created_at=datetime.fromisoformat(row["created_at"]),
            last_item_count=(
                int(row["last_item_count"]) if row["last_item_count"] is not None else None
            ),
            last_matched_count=(
                int(row["last_matched_count"]) if row["last_matched_count"] is not None else None
            ),
            last_run_error_code=row["last_run_error_code"],
        )

    @staticmethod
    def _plan_query() -> str:
        return """SELECT p.*, (
            SELECT r.status FROM source_runs r WHERE r.plan_id = p.id
            ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1
        ) AS last_run_status, (
            SELECT r.error_code FROM source_runs r WHERE r.plan_id = p.id
            ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1
        ) AS last_run_error_code, (
            SELECT r.item_count FROM source_runs r WHERE r.plan_id = p.id
            ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1
        ) AS last_item_count, (
            SELECT r.matched_count FROM source_runs r WHERE r.plan_id = p.id
            ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1
        ) AS last_matched_count FROM monitoring_plans p"""

    @staticmethod
    def _validate_monitoring_target(kind: str, target: str, cadence_minutes: int) -> str:
        if (
            kind not in {"rss", "brave_news"}
            or isinstance(cadence_minutes, bool)
            or not isinstance(cadence_minutes, int)
            or not 15 <= cadence_minutes <= 10080
        ):
            raise ValueError("invalid monitoring plan kind or cadence")
        target = target.strip()
        if not target or len(target) > 2048:
            raise ValueError("invalid monitoring plan target")
        if kind == "brave_news":
            from project_watch.brave_news import is_brave_news_target

            if not is_brave_news_target(target):
                raise ValueError("invalid Brave News target")
        try:
            parsed = urlsplit(target)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid monitoring plan URL") from exc
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.fragment
            or port not in (None, 443)
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))
            or "." not in host
        ):
            raise ValueError("invalid monitoring plan URL")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("invalid monitoring plan URL")
        return target

    def reserve_brave_news_request(self, monthly_limit: int) -> bool:
        """Reserve a paid call before egress, atomically across local worker threads."""
        if isinstance(monthly_limit, bool) or not 0 <= monthly_limit <= 100_000:
            raise ValueError("invalid Brave monthly limit")
        if monthly_limit == 0:
            return False
        month = datetime.now(UTC).strftime("%Y-%m")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO brave_news_budget (month_utc, used) VALUES (?, 0)",
                (month,),
            )
            changed = connection.execute(
                "UPDATE brave_news_budget SET used = used + 1 WHERE month_utc = ? AND used < ?",
                (month, monthly_limit),
            ).rowcount
            return changed == 1

    def brave_news_usage(self) -> int:
        month = datetime.now(UTC).strftime("%Y-%m")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT used FROM brave_news_budget WHERE month_utc = ?", (month,)
            ).fetchone()
            return int(row["used"]) if row is not None else 0

    def create_monitoring_plan(
        self, project_id: str, *, kind: str, target: str, cadence_minutes: int
    ) -> MonitoringPlan:
        target = self._validate_monitoring_target(kind, target, cadence_minutes)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            existing = connection.execute(
                self._plan_query() + " WHERE p.project_id = ? AND p.kind = ? AND p.target = ?",
                (project_id, kind, target),
            ).fetchone()
            if existing is not None:
                return self._monitoring_plan(existing)
            plan_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO monitoring_plans
                   (id, project_id, kind, target, cadence_minutes, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (plan_id, project_id, kind, target, cadence_minutes, datetime.now(UTC).isoformat()),
            )
            row = connection.execute(self._plan_query() + " WHERE p.id = ?", (plan_id,)).fetchone()
            return self._monitoring_plan(row)

    def get_monitoring_plan(self, plan_id: str) -> MonitoringPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(self._plan_query() + " WHERE p.id = ?", (plan_id,)).fetchone()
            return self._monitoring_plan(row) if row is not None else None

    def list_monitoring_plans(self, project_id: str) -> list[MonitoringPlan]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                self._plan_query() + " WHERE p.project_id = ? ORDER BY p.created_at, p.id",
                (project_id,),
            ).fetchall()
            return [self._monitoring_plan(row) for row in rows]

    def set_monitoring_plan_enabled(self, plan_id: str, enabled: bool) -> MonitoringPlan:
        if not isinstance(enabled, bool):
            raise ValueError("monitoring plan enabled must be boolean")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                "UPDATE monitoring_plans SET enabled = ? WHERE id = ?", (int(enabled), plan_id)
            )
            if not result.rowcount:
                raise NotFoundError("monitoring plan not found")
            row = connection.execute(self._plan_query() + " WHERE p.id = ?", (plan_id,)).fetchone()
            return self._monitoring_plan(row)

    @staticmethod
    def _source_run(row: sqlite3.Row) -> SourceRun:
        return SourceRun(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            plan_id=str(row["plan_id"]),
            status=row["status"],
            error_code=row["error_code"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]),
            idempotency_key=row["idempotency_key"],
            item_count=int(row["item_count"]),
            matched_count=int(row["matched_count"]) if row["matched_count"] is not None else None,
            new_count=int(row["new_count"]),
        )

    def record_source_run(
        self,
        plan_id: str,
        *,
        status: SourceRunStatus,
        error_code: str | None,
        idempotency_key: str | None = None,
        signals: tuple[SignalInput, ...] = (),
        fetched_count: int | None = None,
    ) -> SourceRun:
        if status not in {"completed", "no_results", "unreachable", "rate_limited", "not_run"}:
            raise ValueError("invalid source run status")
        if status == "rate_limited" and error_code not in {"rate_limited", "quota_exhausted"}:
            raise ValueError("rate-limited source requires its error code")
        if (
            error_code is not None
            and status != "rate_limited"
            and (
                status != "unreachable" or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None
            )
        ):
            raise ValueError("invalid source run error code")
        if idempotency_key is not None and (
            not idempotency_key.strip() or len(idempotency_key) > 200
        ):
            raise ValueError("invalid source run idempotency key")
        if signals and status != "completed":
            raise ValueError("only completed runs can contain signals")
        if len(signals) > 500:
            raise ValueError("too many source signals")
        item_count = len(signals) if fetched_count is None else fetched_count
        if (
            isinstance(item_count, bool)
            or not isinstance(item_count, int)
            or not len(signals) <= item_count <= 500
            or (status != "completed" and item_count != 0)
        ):
            raise ValueError("invalid source item count")
        for signal in signals:
            try:
                signal_url = urlsplit(signal.url)
            except ValueError as exc:
                raise ValueError("invalid source signal URL") from exc
            if (
                not signal.external_id.strip()
                or len(signal.external_id) > 500
                or not signal.title.strip()
                or len(signal.title) > 300
                or len(signal.summary) > 2000
                or len(signal.url) > 2048
                or signal_url.scheme != "https"
                or not signal_url.hostname
                or (signal.published_at is not None and signal.published_at.tzinfo is None)
                or signal.freshness not in {"current", "historical", "unknown"}
            ):
                raise ValueError("invalid source signal")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM source_runs WHERE plan_id = ? AND idempotency_key = ?",
                    (plan_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    return self._source_run(existing)
            plan = connection.execute(
                "SELECT project_id FROM monitoring_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFoundError("monitoring plan not found")
            now = datetime.now(UTC)
            run = SourceRun(
                id=str(uuid.uuid4()),
                project_id=str(plan["project_id"]),
                plan_id=plan_id,
                status=status,
                error_code=error_code,
                started_at=now,
                finished_at=now,
                idempotency_key=idempotency_key,
                item_count=item_count,
                matched_count=len(signals),
            )
            connection.execute(
                """INSERT INTO source_runs
                   (id, project_id, plan_id, status, error_code, started_at, finished_at,
                    idempotency_key, item_count, matched_count, new_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    run.id,
                    run.project_id,
                    run.plan_id,
                    run.status,
                    run.error_code,
                    run.started_at.isoformat(),
                    run.finished_at.isoformat(),
                    idempotency_key,
                    item_count,
                    len(signals),
                ),
            )
            new_count = 0
            for signal in signals:
                content = json.dumps(
                    [signal.title, signal.url, signal.summary],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                inserted = connection.execute(
                    """INSERT OR IGNORE INTO signals
                       (id, project_id, plan_id, run_id, external_id, title, url, summary,
                        published_at, first_seen_at, content_sha256, freshness, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate')""",
                    (
                        str(uuid.uuid4()),
                        run.project_id,
                        plan_id,
                        run.id,
                        signal.external_id.strip(),
                        signal.title.strip(),
                        signal.url,
                        signal.summary,
                        signal.published_at.isoformat() if signal.published_at else None,
                        now.isoformat(),
                        content_hash,
                        signal.freshness,
                    ),
                )
                if inserted.rowcount and signal.freshness == "current":
                    new_count += 1
            connection.execute(
                "UPDATE source_runs SET new_count = ? WHERE id = ?", (new_count, run.id)
            )
            return SourceRun(
                id=run.id,
                project_id=run.project_id,
                plan_id=run.plan_id,
                status=run.status,
                error_code=run.error_code,
                started_at=run.started_at,
                finished_at=run.finished_at,
                idempotency_key=run.idempotency_key,
                item_count=run.item_count,
                matched_count=run.matched_count,
                new_count=new_count,
            )

    def get_source_run_by_key(self, plan_id: str, idempotency_key: str) -> SourceRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM source_runs WHERE plan_id = ? AND idempotency_key = ?",
                (plan_id, idempotency_key),
            ).fetchone()
            return self._source_run(row) if row is not None else None

    def list_source_runs(self, plan_id: str) -> list[SourceRun]:
        with closing(self._connect()) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM monitoring_plans WHERE id = ?", (plan_id,)
                ).fetchone()
                is None
            ):
                raise NotFoundError("monitoring plan not found")
            rows = connection.execute(
                "SELECT * FROM source_runs WHERE plan_id = ? ORDER BY started_at, rowid",
                (plan_id,),
            ).fetchall()
            return [self._source_run(row) for row in rows]

    def list_latest_source_runs(self, project_id: str) -> list[SourceRun]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM (
                       SELECT r.*, ROW_NUMBER() OVER (
                           PARTITION BY r.plan_id
                           ORDER BY r.finished_at DESC, r.started_at DESC, r.id DESC
                       ) AS run_rank
                       FROM source_runs r WHERE r.project_id = ?
                   ) WHERE run_rank = 1 ORDER BY plan_id""",
                (project_id,),
            ).fetchall()
            return [self._source_run(row) for row in rows]

    @staticmethod
    def _signal(row: sqlite3.Row) -> Signal:
        return Signal(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            plan_id=str(row["plan_id"]),
            run_id=str(row["run_id"]),
            external_id=str(row["external_id"]),
            title=str(row["title"]),
            url=str(row["url"]),
            summary=str(row["summary"]),
            published_at=datetime.fromisoformat(row["published_at"])
            if row["published_at"]
            else None,
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            content_sha256=str(row["content_sha256"]),
            freshness=row["freshness"],
            status=row["status"],
        )

    def list_signals(self, project_id: str) -> list[Signal]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM signals WHERE project_id = ? ORDER BY first_seen_at DESC, rowid DESC",
                (project_id,),
            ).fetchall()
            return [self._signal(row) for row in rows]

    @staticmethod
    def _monitoring_job(row: sqlite3.Row) -> MonitoringJob:
        return MonitoringJob(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            plan_id=str(row["plan_id"]),
            idempotency_key=str(row["idempotency_key"]),
            trigger=row["trigger"],
            status=row["status"],
            attempts=int(row["attempts"]),
            lease_until=datetime.fromisoformat(row["lease_until"]) if row["lease_until"] else None,
            run_id=row["run_id"],
            error_code=row["error_code"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _enqueue_monitoring_job(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        plan_id: str,
        idempotency_key: str,
        trigger: str,
        now: datetime,
    ) -> MonitoringJob:
        existing = connection.execute(
            "SELECT * FROM monitoring_jobs WHERE plan_id = ? AND idempotency_key = ?",
            (plan_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            return ProjectRepository._monitoring_job(existing)
        job_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO monitoring_jobs
               (id, project_id, plan_id, idempotency_key, trigger, status, attempts,
                lease_until, run_id, error_code, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'queued', 0, NULL, NULL, NULL, ?, ?)""",
            (
                job_id,
                project_id,
                plan_id,
                idempotency_key,
                trigger,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        row = connection.execute("SELECT * FROM monitoring_jobs WHERE id = ?", (job_id,)).fetchone()
        return ProjectRepository._monitoring_job(row)

    def request_monitoring_run(self, plan_id: str, *, idempotency_key: str) -> MonitoringJob:
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 180
            or idempotency_key.startswith("scheduled:")
        ):
            raise ValueError("invalid monitoring job idempotency key")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = connection.execute(
                "SELECT project_id FROM monitoring_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFoundError("monitoring plan not found")
            return self._enqueue_monitoring_job(
                connection,
                project_id=str(plan["project_id"]),
                plan_id=plan_id,
                idempotency_key=idempotency_key,
                trigger="manual",
                now=datetime.now(UTC),
            )

    def schedule_due_monitoring(self, *, now: datetime | None = None) -> list[MonitoringJob]:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("schedule time must be timezone-aware")
        scheduled: list[MonitoringJob] = []
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT p.*, (
                       SELECT MAX(r.finished_at) FROM source_runs r WHERE r.plan_id = p.id
                   ) AS last_run_at FROM monitoring_plans p
                   WHERE p.enabled = 1 ORDER BY p.created_at, p.id"""
            ).fetchall()
            for row in rows:
                outstanding = connection.execute(
                    """SELECT 1 FROM monitoring_jobs
                       WHERE plan_id = ? AND status IN ('queued', 'processing') LIMIT 1""",
                    (row["id"],),
                ).fetchone()
                if outstanding is not None:
                    continue
                last_run_at = (
                    datetime.fromisoformat(row["last_run_at"]) if row["last_run_at"] else None
                )
                due_at = (
                    last_run_at + timedelta(minutes=int(row["cadence_minutes"]))
                    if last_run_at
                    else datetime.fromisoformat(row["created_at"])
                )
                if due_at > now:
                    continue
                key = "scheduled:" + due_at.isoformat()
                existing = connection.execute(
                    "SELECT 1 FROM monitoring_jobs WHERE plan_id = ? AND idempotency_key = ?",
                    (row["id"], key),
                ).fetchone()
                if existing is not None:
                    continue
                scheduled.append(
                    self._enqueue_monitoring_job(
                        connection,
                        project_id=str(row["project_id"]),
                        plan_id=str(row["id"]),
                        idempotency_key=key,
                        trigger="scheduled",
                        now=now,
                    )
                )
        return scheduled

    def claim_next_monitoring_job(
        self, *, now: datetime | None = None, lease_seconds: int = 300
    ) -> MonitoringJob | None:
        now = now or datetime.now(UTC)
        if now.tzinfo is None or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid monitoring job lease")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM monitoring_jobs
                   WHERE status = 'queued'
                      OR (status = 'processing' AND lease_until <= ?)
                   ORDER BY created_at, id LIMIT 1""",
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE monitoring_jobs SET status = 'processing', attempts = attempts + 1,
                   lease_until = ?, updated_at = ? WHERE id = ?""",
                ((now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(), row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._monitoring_job(updated)

    def complete_monitoring_job(self, job_id: str, *, run_id: str) -> MonitoringJob:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFoundError("monitoring job not found")
            if job["status"] != "processing":
                raise IntakeConflictError("monitoring job is not processing")
            run = connection.execute(
                "SELECT plan_id FROM source_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None or run["plan_id"] != job["plan_id"]:
                raise ValueError("monitoring run does not belong to job plan")
            connection.execute(
                """UPDATE monitoring_jobs SET status = 'done', run_id = ?,
                   lease_until = NULL, updated_at = ? WHERE id = ?""",
                (run_id, datetime.now(UTC).isoformat(), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._monitoring_job(updated)

    def fail_monitoring_job(self, job_id: str, *, error_code: str) -> MonitoringJob:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None:
            raise ValueError("invalid monitoring job error code")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFoundError("monitoring job not found")
            if job["status"] != "processing":
                raise IntakeConflictError("monitoring job is not processing")
            connection.execute(
                """UPDATE monitoring_jobs SET status = 'failed', error_code = ?,
                   lease_until = NULL, updated_at = ? WHERE id = ?""",
                (error_code, datetime.now(UTC).isoformat(), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._monitoring_job(updated)

    def retry_failed_monitoring_job(self, job_id: str) -> MonitoringJob:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFoundError("monitoring job not found")
            if job["status"] != "failed":
                raise IntakeConflictError("only failed monitoring jobs can be retried")
            connection.execute(
                """UPDATE monitoring_jobs SET status = 'queued', error_code = NULL,
                   lease_until = NULL, updated_at = ? WHERE id = ?""",
                (datetime.now(UTC).isoformat(), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._monitoring_job(updated)

    def list_monitoring_jobs(self, project_id: str) -> list[MonitoringJob]:
        with closing(self._connect()) as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return [self._monitoring_job(row) for row in rows]

    def get_monitoring_job(self, job_id: str) -> MonitoringJob | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._monitoring_job(row) if row is not None else None
