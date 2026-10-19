"""Partial PostgreSQL repository, isolated from the active SQLite API.

Only call methods requiring actor IDs after verifying identity at the boundary.
This is not yet a complete ProjectRepository replacement or team deployment.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from project_watch.analysis.financial_mapping import (
    FinancialSuggestion,
    validate_financial_suggestions,
)
from project_watch.analysis.operating_mapping import (
    OperatingSuggestion,
    validate_operating_suggestions,
)
from project_watch.analysis.source_update import impact_topic, review_topics
from project_watch.domain import (
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
from project_watch.holding_snapshots import validated_holding_input
from project_watch.identity_review import review_note, verified_identity_input
from project_watch.native_intake import NativeCandidate, validate_native_candidates
from project_watch.postgres_pool import open_project_watch_pool
from project_watch.repository import (
    IdentityConflictError,
    IntakeConflictError,
    NotFoundError,
    ProjectRepository,
    ReviewConflictError,
)
from project_watch.watch_items import (
    watch_change_input,
    watch_change_review_input,
    watch_item_input,
    watch_review_input,
)


class PostgresCoreRepository:
    """Small, explicit first slice of the team repository; not wired to API."""

    def __init__(self, dsn: str, *, pool_size: int = 8) -> None:
        self._pool = open_project_watch_pool(dsn, pool_size=pool_size)

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> PostgresCoreRepository:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def _require_project(connection, project_id: str) -> None:
        if (
            connection.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,)).fetchone()
            is None
        ):
            raise NotFoundError("project not found")

    @staticmethod
    def _audit(
        connection,
        *,
        project_id: str,
        action: str,
        actor_kind: str,
        actor_ref: str,
        channel: str,
        subject_id: str,
        source_id: str | None = None,
        source_sha256: str | None = None,
        source_locator: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events
               (id, project_id, action, actor_kind, actor_ref, channel, subject_id,
                source_id, source_sha256, source_locator, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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

    def create_project(self, name: str, *, verified_actor_id: str) -> Project:
        if not verified_actor_id:
            raise ValueError("a verified actor is required")
        normalized = normalized_project_name(name)
        key = normalized.casefold()
        project_id = str(uuid.uuid4())
        with self._pool.connection() as connection:
            row = connection.execute(
                """INSERT INTO projects (id, name, name_key, identity_status, created_at)
                   VALUES (%s, %s, %s, 'unverified', %s)
                   ON CONFLICT (name_key) DO NOTHING RETURNING *""",
                (project_id, normalized, key, datetime.now(UTC).isoformat()),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM projects WHERE name_key = %s", (key,)
                ).fetchone()
            else:
                self._audit(
                    connection,
                    project_id=project_id,
                    action="project.created",
                    actor_kind="verified_user",
                    actor_ref=verified_actor_id,
                    channel="team_web",
                    subject_id=project_id,
                )
            return ProjectRepository._project(row)

    def get_project(self, project_id: str) -> Project | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = %s", (project_id,)
            ).fetchone()
            return ProjectRepository._project(row) if row else None

    def list_competitive_peers(self, project_id: str) -> list[CompetitivePeer]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM competitive_peers WHERE project_id = %s ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._competitive_peer(row) for row in rows]

    def list_holding_snapshots(self, project_id: str) -> list[HoldingSnapshot]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM holding_snapshots WHERE project_id = %s ORDER BY as_of DESC, id",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._holding_snapshot(row) for row in rows]

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
        verified_actor_id: str,
    ) -> HoldingSnapshot:
        if not verified_actor_id:
            raise ValueError("verified actor required")
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            source = connection.execute(
                "SELECT * FROM sources WHERE id = %s AND project_id = %s",
                (source_id, project_id),
            ).fetchone()
            if source is None:
                raise NotFoundError("holding source not found")
            fragments = connection.execute(
                """SELECT text, locator FROM document_fragments
                   WHERE project_id = %s AND source_id = %s AND locator = %s""",
                (project_id, source_id, source_locator),
            ).fetchall()
            if len(fragments) != 1:
                raise ValueError("holding source locator must select exactly one extracted fragment")
            fund_name, cost, pct, value = validated_holding_input(
                fund_name=fund_name, as_of=as_of, source_locator=source_locator,
                fragment_locator=fragments[0]["locator"], fragment_text=fragments[0]["text"],
                invested_cost_wan=invested_cost_wan, ownership_pct=ownership_pct,
                current_value_wan=current_value_wan,
            )
            snapshot_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            row = connection.execute(
                """INSERT INTO holding_snapshots
                   (id, project_id, fund_name, as_of, source_id, source_locator,
                    invested_cost_wan, ownership_pct, current_value_wan,
                    status, version, proposed_by, reviewed_by, review_note, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'candidate', 1, %s,
                           NULL, NULL, %s, %s) RETURNING *""",
                (snapshot_id, project_id, fund_name, as_of.isoformat(), source_id,
                 source_locator, str(cost) if cost is not None else None,
                 str(pct) if pct is not None else None,
                 str(value) if value is not None else None, verified_actor_id, now, now),
            ).fetchone()
            self._audit(
                connection, project_id=project_id, action="holding_snapshot.proposed",
                actor_kind="verified_user", actor_ref=verified_actor_id, channel="team_web",
                subject_id=snapshot_id, source_id=source_id,
                source_sha256=source["content_sha256"], source_locator=source_locator,
            )
            return ProjectRepository._holding_snapshot(row)

    def review_holding_snapshot(
        self,
        project_id: str,
        snapshot_id: str,
        *,
        action: str,
        expected_version: int,
        verified_reviewer_id: str,
        note: str | None = None,
    ) -> HoldingSnapshot:
        if action not in {"confirm", "reject"}:
            raise ValueError("invalid holding review action")
        if not verified_reviewer_id or len(verified_reviewer_id) > 200:
            raise ValueError("verified reviewer required")
        note = note.strip() if note else None
        if note and len(note) > 500:
            raise ValueError("holding review note must contain at most 500 characters")
        with self._pool.connection() as connection:
            existing = connection.execute(
                """SELECT * FROM holding_snapshots
                   WHERE id = %s AND project_id = %s FOR UPDATE""",
                (snapshot_id, project_id),
            ).fetchone()
            if existing is None:
                self._require_project(connection, project_id)
                raise NotFoundError("holding snapshot not found")
            if existing["status"] != "candidate" or existing["version"] != expected_version:
                raise ReviewConflictError("holding snapshot is stale or already reviewed")
            if existing["proposed_by"] == verified_reviewer_id:
                raise ValueError("holding snapshot proposer cannot review own proposal")
            reviewed_status = "confirmed" if action == "confirm" else "rejected"
            row = connection.execute(
                """UPDATE holding_snapshots SET status = %s, version = version + 1,
                   reviewed_by = %s, review_note = %s, updated_at = %s
                   WHERE id = %s RETURNING *""",
                (reviewed_status, verified_reviewer_id, note,
                 datetime.now(UTC).isoformat(), snapshot_id),
            ).fetchone()
            source = connection.execute(
                "SELECT content_sha256 FROM sources WHERE id = %s", (row["source_id"],)
            ).fetchone()
            self._audit(
                connection, project_id=project_id,
                action=f"holding_snapshot.{reviewed_status}",
                actor_kind="verified_user", actor_ref=verified_reviewer_id,
                channel="team_web", subject_id=snapshot_id, source_id=row["source_id"],
                source_sha256=source["content_sha256"], source_locator=row["source_locator"],
            )
            return ProjectRepository._holding_snapshot(row)

    def list_watch_items(self, project_id: str) -> list[WatchItem]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM watch_items WHERE project_id = %s ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._watch_item(row) for row in rows]

    def list_watch_item_changes(self, project_id: str) -> list[WatchItemChange]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM watch_item_changes WHERE project_id = %s ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._watch_change(row) for row in rows]

    def propose_watch_item(
        self,
        project_id: str,
        *,
        observation_id: str,
        title: str,
        question: str,
        owner: str | None = None,
        due_on: date | None = None,
        verified_actor_id: str,
    ) -> WatchItem:
        if not verified_actor_id:
            raise ValueError("a verified actor is required")
        title, title_key, question, owner, due = watch_item_input(title, question, owner, due_on)
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            observation = connection.execute(
                """SELECT o.status, o.source_id, o.source_locator, s.content_sha256
                   FROM observations o JOIN sources s ON s.id = o.source_id
                   WHERE o.id = %s AND o.project_id = %s AND s.project_id = %s""",
                (observation_id, project_id, project_id),
            ).fetchone()
            if observation is None:
                raise NotFoundError("confirmed observation not found in project")
            if observation["status"] != "confirmed" or not observation["source_locator"]:
                raise ValueError("watch item requires a confirmed, located observation")
            item_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            row = connection.execute(
                """INSERT INTO watch_items
                   (id, project_id, observation_id, title, title_key, question, owner, due_on,
                    status, version, proposed_by, reviewed_by, review_note, created_at, updated_at,
                    current_observation_id, initial_question, initial_owner, initial_due_on)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'candidate', 1, %s, NULL, NULL,
                           %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (project_id, observation_id, title_key) DO NOTHING RETURNING *""",
                (
                    item_id,
                    project_id,
                    observation_id,
                    title,
                    title_key,
                    question,
                    owner,
                    due,
                    verified_actor_id,
                    now,
                    now,
                    observation_id,
                    question,
                    owner,
                    due,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT * FROM watch_items
                       WHERE project_id = %s AND observation_id = %s AND title_key = %s""",
                    (project_id, observation_id, title_key),
                ).fetchone()
                if row["question"] != question or row["owner"] != owner or row["due_on"] != due:
                    raise ReviewConflictError("watch item already exists with different details")
                return ProjectRepository._watch_item(row)
            connection.execute(
                """INSERT INTO watch_item_events
                   (id, watch_item_id, project_id, version, from_status, to_status,
                    actor_ref, note, created_at)
                   VALUES (%s, %s, %s, 1, NULL, 'candidate', %s, NULL, %s)""",
                (str(uuid.uuid4()), item_id, project_id, verified_actor_id, now),
            )
            self._audit(
                connection,
                project_id=project_id,
                action="watch_item.proposed",
                actor_kind="verified_user",
                actor_ref=verified_actor_id,
                channel="team_web",
                subject_id=item_id,
                source_id=observation["source_id"],
                source_sha256=observation["content_sha256"],
                source_locator=observation["source_locator"],
            )
            return ProjectRepository._watch_item(row)

    def review_watch_item(
        self,
        project_id: str,
        item_id: str,
        *,
        action: str,
        expected_version: int,
        verified_reviewer_id: str,
        note: str | None = None,
    ) -> WatchItem:
        status, reviewer, note = watch_review_input(action, verified_reviewer_id, note)
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE watch_items SET status = %s, version = version + 1,
                   reviewed_by = %s, review_note = %s, updated_at = %s
                   WHERE id = %s AND project_id = %s AND status = 'candidate'
                     AND version = %s RETURNING *""",
                (
                    status,
                    reviewer,
                    note,
                    datetime.now(UTC).isoformat(),
                    item_id,
                    project_id,
                    expected_version,
                ),
            ).fetchone()
            if row is None:
                self._require_project(connection, project_id)
                existing = connection.execute(
                    "SELECT 1 FROM watch_items WHERE id = %s AND project_id = %s",
                    (item_id, project_id),
                ).fetchone()
                if existing is None:
                    raise NotFoundError("watch item not found")
                raise ReviewConflictError("watch item is stale or already reviewed")
            observation = connection.execute(
                """SELECT o.source_id, o.source_locator, s.content_sha256
                   FROM observations o JOIN sources s ON s.id = o.source_id
                   WHERE o.id = %s AND o.project_id = %s""",
                (row["observation_id"], project_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO watch_item_events
                   (id, watch_item_id, project_id, version, from_status, to_status,
                    actor_ref, note, created_at)
                   VALUES (%s, %s, %s, %s, 'candidate', %s, %s, %s, %s)""",
                (
                    str(uuid.uuid4()),
                    item_id,
                    project_id,
                    row["version"],
                    status,
                    reviewer,
                    note,
                    row["updated_at"],
                ),
            )
            self._audit(
                connection,
                project_id=project_id,
                action=f"watch_item.{status}",
                actor_kind="verified_user",
                actor_ref=reviewer,
                channel="team_web",
                subject_id=item_id,
                source_id=observation["source_id"],
                source_sha256=observation["content_sha256"],
                source_locator=observation["source_locator"],
            )
            return ProjectRepository._watch_item(row)

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
        verified_actor_id: str,
    ) -> WatchItemChange:
        if not verified_actor_id:
            raise ValueError("a verified actor is required")
        target_status, question, owner, due, reason = watch_change_input(
            target_status, question, owner, due_on, reason
        )
        with self._pool.connection() as connection:
            item = connection.execute(
                "SELECT * FROM watch_items WHERE id = %s AND project_id = %s FOR UPDATE",
                (item_id, project_id),
            ).fetchone()
            if item is None:
                self._require_project(connection, project_id)
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
                   WHERE o.id = %s AND o.project_id = %s AND s.project_id = %s""",
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
                   WHERE watch_item_id = %s AND project_id = %s AND status = 'candidate'""",
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
                    return ProjectRepository._watch_change(pending)
                raise ReviewConflictError("another watch change is awaiting review")
            change_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            row = connection.execute(
                """INSERT INTO watch_item_changes
                   (id, project_id, watch_item_id, base_version, target_status, question, owner,
                    due_on, evidence_observation_id, reason, status, proposed_by, reviewed_by,
                    review_note, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'candidate', %s,
                           NULL, NULL, %s, %s) RETURNING *""",
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
                    verified_actor_id,
                    now,
                    now,
                ),
            ).fetchone()
            self._audit(
                connection,
                project_id=project_id,
                action="watch_item.change_proposed",
                actor_kind="verified_user",
                actor_ref=verified_actor_id,
                channel="team_web",
                subject_id=item_id,
                source_id=evidence["source_id"],
                source_sha256=evidence["content_sha256"],
                source_locator=evidence["source_locator"],
            )
            return ProjectRepository._watch_change(row)

    def review_watch_item_change(
        self,
        project_id: str,
        item_id: str,
        change_id: str,
        *,
        action: str,
        expected_version: int,
        verified_reviewer_id: str,
        note: str | None = None,
    ) -> WatchItem:
        action, reviewer, note = watch_change_review_input(action, verified_reviewer_id, note)
        with self._pool.connection() as connection:
            item = connection.execute(
                "SELECT * FROM watch_items WHERE id = %s AND project_id = %s FOR UPDATE",
                (item_id, project_id),
            ).fetchone()
            if item is None:
                self._require_project(connection, project_id)
                raise NotFoundError("watch item not found")
            change = connection.execute(
                """SELECT * FROM watch_item_changes
                   WHERE id = %s AND watch_item_id = %s AND project_id = %s FOR UPDATE""",
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
                item = connection.execute(
                    """UPDATE watch_items SET status = %s, question = %s, owner = %s,
                       due_on = %s, current_observation_id = COALESCE(%s, current_observation_id),
                       resolved_at = %s, version = version + 1, reviewed_by = %s,
                       review_note = %s, updated_at = %s WHERE id = %s RETURNING *""",
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
                ).fetchone()
                connection.execute(
                    """INSERT INTO watch_item_events
                       (id, watch_item_id, project_id, version, from_status, to_status,
                        actor_ref, note, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                """UPDATE watch_item_changes SET status = %s, reviewed_by = %s,
                   review_note = %s, updated_at = %s WHERE id = %s""",
                ("accepted" if action == "accept" else "rejected", reviewer, note, now, change_id),
            )
            evidence = connection.execute(
                """SELECT o.source_id, o.source_locator, s.content_sha256
                   FROM observations o JOIN sources s ON s.id = o.source_id
                   WHERE o.id = %s AND o.project_id = %s AND s.project_id = %s""",
                (
                    change["evidence_observation_id"] or item["current_observation_id"],
                    project_id,
                    project_id,
                ),
            ).fetchone()
            self._audit(
                connection,
                project_id=project_id,
                action=f"watch_item.change_{action}",
                actor_kind="verified_user",
                actor_ref=reviewer,
                channel="team_web",
                subject_id=item_id,
                source_id=evidence["source_id"],
                source_sha256=evidence["content_sha256"],
                source_locator=evidence["source_locator"],
            )
            return ProjectRepository._watch_item(item)

    def propose_competitive_peer(
        self,
        project_id: str,
        *,
        name: str,
        linked_project_id: str | None = None,
        verified_actor_id: str,
    ) -> CompetitivePeer:
        if not verified_actor_id:
            raise ValueError("a verified actor is required")
        normalized = normalized_project_name(name)
        if len(normalized) > 200:
            raise ValueError("peer name must contain at most 200 characters")
        if linked_project_id == project_id:
            raise ValueError("project cannot be its own peer")
        peer_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            if linked_project_id:
                self._require_project(connection, linked_project_id)
            row = connection.execute(
                """INSERT INTO competitive_peers
                   (id, project_id, name, name_key, linked_project_id, status, version,
                    proposed_by, reviewed_by, note, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, 'candidate', 1, %s, NULL, NULL, %s, %s)
                   ON CONFLICT (project_id, name_key) DO NOTHING RETURNING *""",
                (
                    peer_id,
                    project_id,
                    normalized,
                    normalized.casefold(),
                    linked_project_id,
                    verified_actor_id,
                    now,
                    now,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM competitive_peers WHERE project_id = %s AND name_key = %s FOR UPDATE",
                    (project_id, normalized.casefold()),
                ).fetchone()
                if row["linked_project_id"] is None and linked_project_id:
                    row = connection.execute(
                        """UPDATE competitive_peers SET linked_project_id = %s,
                           status = 'candidate', version = version + 1, proposed_by = %s,
                           reviewed_by = NULL, note = NULL, updated_at = %s
                           WHERE id = %s RETURNING *""",
                        (
                            linked_project_id,
                            verified_actor_id,
                            datetime.now(UTC).isoformat(),
                            row["id"],
                        ),
                    ).fetchone()
                    self._audit(
                        connection,
                        project_id=project_id,
                        action="competitive_peer.link_proposed",
                        actor_kind="verified_user",
                        actor_ref=verified_actor_id,
                        channel="team_web",
                        subject_id=str(row["id"]),
                    )
                elif row["linked_project_id"] != linked_project_id:
                    raise ValueError("peer name already has a different project link")
            else:
                self._audit(
                    connection,
                    project_id=project_id,
                    action="competitive_peer.proposed",
                    actor_kind="verified_user",
                    actor_ref=verified_actor_id,
                    channel="team_web",
                    subject_id=peer_id,
                )
            return ProjectRepository._competitive_peer(row)

    def review_competitive_peer(
        self,
        project_id: str,
        peer_id: str,
        *,
        action: str,
        expected_version: int,
        verified_reviewer_id: str,
        note: str | None = None,
    ) -> CompetitivePeer:
        if action not in {"confirm", "reject"}:
            raise ValueError("invalid peer review action")
        if not verified_reviewer_id or len(verified_reviewer_id) > 200:
            raise ValueError("a verified reviewer is required")
        note = note.strip() if note else None
        if note and len(note) > 500:
            raise ValueError("peer review note must contain at most 500 characters")
        peer_status = "confirmed" if action == "confirm" else "rejected"
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE competitive_peers SET status = %s, version = version + 1,
                   reviewed_by = %s, note = %s, updated_at = %s
                   WHERE id = %s AND project_id = %s AND status = 'candidate'
                     AND version = %s RETURNING *""",
                (
                    peer_status,
                    verified_reviewer_id,
                    note,
                    datetime.now(UTC).isoformat(),
                    peer_id,
                    project_id,
                    expected_version,
                ),
            ).fetchone()
            if row is None:
                self._require_project(connection, project_id)
                existing = connection.execute(
                    "SELECT 1 FROM competitive_peers WHERE id = %s AND project_id = %s",
                    (peer_id, project_id),
                ).fetchone()
                if existing is None:
                    raise NotFoundError("competitive peer not found")
                raise ReviewConflictError("competitive peer is stale or already reviewed")
            self._audit(
                connection,
                project_id=project_id,
                action=f"competitive_peer.{peer_status}",
                actor_kind="verified_user",
                actor_ref=verified_reviewer_id,
                channel="team_web",
                subject_id=peer_id,
            )
            return ProjectRepository._competitive_peer(row)

    def get_identity_review(self, project_id: str) -> IdentityReview | None:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                """SELECT * FROM project_identity_reviews WHERE project_id = %s
                   ORDER BY version DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            return ProjectRepository._identity_review(row) if row else None

    def list_identity_reviews(self, project_id: str, *, limit: int = 50) -> list[IdentityReview]:
        if not 1 <= limit <= 100:
            raise ValueError("identity history limit must be between 1 and 100")
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM project_identity_reviews WHERE project_id = %s
                   ORDER BY version DESC LIMIT %s""",
                (project_id, limit),
            ).fetchall()
            return [ProjectRepository._identity_review(row) for row in rows]

    @staticmethod
    def _identity_version(connection, project_id: str) -> int:
        row = connection.execute(
            "SELECT MAX(version) AS version FROM project_identity_reviews WHERE project_id = %s",
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
        with self._pool.connection() as connection:
            project = connection.execute(
                "SELECT identity_status FROM projects WHERE id = %s FOR UPDATE", (project_id,)
            ).fetchone()
            if project is None:
                raise NotFoundError("project not found")
            version = self._identity_version(connection, project_id)
            if version != expected_version:
                raise IdentityConflictError("project identity review changed")
            source_hash = None
            if details.evidence_source_id:
                source = connection.execute(
                    """SELECT content_sha256 FROM sources
                       WHERE id = %s AND project_id = %s AND kind = 'document'""",
                    (details.evidence_source_id, project_id),
                ).fetchone()
                if (
                    source is None
                    or connection.execute(
                        """SELECT 1 FROM document_fragments WHERE source_id = %s
                       AND project_id = %s AND locator = %s LIMIT 1""",
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
                   VALUES (%s, %s, %s, 'verify', %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                "UPDATE projects SET identity_status = 'verified' WHERE id = %s", (project_id,)
            )
            self._audit(
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
                "SELECT * FROM project_identity_reviews WHERE id = %s", (review_id,)
            ).fetchone()
            return ProjectRepository._identity_review(row)

    def reopen_project_identity(
        self, project_id: str, *, note: str, expected_version: int, actor_user_id: str
    ) -> IdentityReview:
        if not actor_user_id or expected_version < 0:
            raise ValueError("verified reviewer and revision are required")
        reason = review_note(note)
        with self._pool.connection() as connection:
            project = connection.execute(
                "SELECT identity_status FROM projects WHERE id = %s FOR UPDATE", (project_id,)
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
                   VALUES (%s, %s, %s, 'reopen', %s, %s, %s)""",
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
                "UPDATE projects SET identity_status = 'unverified' WHERE id = %s", (project_id,)
            )
            self._audit(
                connection,
                project_id=project_id,
                action="identity.reopened",
                actor_kind="verified_user",
                actor_ref=actor_user_id,
                channel="team_web",
                subject_id=review_id,
            )
            row = connection.execute(
                "SELECT * FROM project_identity_reviews WHERE id = %s", (review_id,)
            ).fetchone()
            return ProjectRepository._identity_review(row)

    def list_projects_page(
        self, *, after: tuple[str, str] | None = None, limit: int = 50
    ) -> list[Project]:
        if not 1 <= limit <= 100:
            raise ValueError("project page limit must be between 1 and 100")
        with self._pool.connection() as connection:
            if after is None:
                rows = connection.execute(
                    "SELECT * FROM projects ORDER BY created_at, id LIMIT %s", (limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM projects WHERE (created_at, id) > (%s, %s)
                       ORDER BY created_at, id LIMIT %s""",
                    (*after, limit),
                ).fetchall()
            return [ProjectRepository._project(row) for row in rows]

    def list_visible_projects_page(
        self, verified_user_id: str, *, after: tuple[str, str] | None = None, limit: int = 50
    ) -> list[Project]:
        """Filter membership in SQL so page boundaries never expose other projects."""
        if not 1 <= limit <= 100:
            raise ValueError("project page limit must be between 1 and 100")
        where_after = "" if after is None else "AND (p.created_at, p.id) > (%s, %s)"
        values = (verified_user_id, limit) if after is None else (verified_user_id, *after, limit)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """SELECT p.* FROM projects p
                   JOIN team_users u ON u.id = %s AND u.active = 1
                   LEFT JOIN team_memberships m ON m.project_id = p.id AND m.user_id = u.id
                   WHERE (u.global_admin = 1 OR m.user_id IS NOT NULL)
                   """
                + where_after
                + " ORDER BY p.created_at, p.id LIMIT %s",
                values,
            ).fetchall()
            return [ProjectRepository._project(row) for row in rows]

    def add_source(
        self, project_id: str, *, kind: str, reference: str, content_sha256: str
    ) -> SourceVersion:
        kind = kind.strip()
        reference = reference.strip()
        digest = content_sha256.lower().strip()
        if kind not in {"document", "url", "note"}:
            raise ValueError("source kind must be document, url, or note")
        if not reference or len(reference) > 500:
            raise ValueError("source reference must contain 1-500 characters")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("source content_sha256 must be a SHA-256 hex digest")
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                """INSERT INTO sources
                   (id, project_id, kind, reference, content_sha256, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (project_id, content_sha256) DO NOTHING RETURNING *""",
                (
                    str(uuid.uuid4()),
                    project_id,
                    kind,
                    reference,
                    digest,
                    datetime.now(UTC).isoformat(),
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM sources WHERE project_id = %s AND content_sha256 = %s",
                    (project_id, digest),
                ).fetchone()
            return ProjectRepository._source(row)

    def get_source(self, project_id: str, source_id: str) -> SourceVersion | None:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM sources WHERE id = %s AND project_id = %s", (source_id, project_id)
            ).fetchone()
            return ProjectRepository._source(row) if row else None

    def list_sources(self, project_id: str) -> list[SourceVersion]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT s.*, di.topic, di.basis AS topic_basis FROM sources s
                   LEFT JOIN document_index di ON di.source_id = s.id AND di.project_id = s.project_id
                   WHERE s.project_id = %s ORDER BY s.created_at, s.id""",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._source(row) for row in rows]

    def get_execution_task(self, project_id: str, task_id: str) -> ExecutionTask | None:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM execution_tasks WHERE project_id = %s AND id = %s",
                (project_id, task_id),
            ).fetchone()
            return ProjectRepository._execution_task(row) if row else None

    def list_execution_tasks(self, project_id: str, *, limit: int = 20) -> list[ExecutionTask]:
        if not 1 <= limit <= 100:
            raise ValueError("task list limit must be between 1 and 100")
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM execution_tasks WHERE project_id = %s
                   ORDER BY created_at DESC, id DESC LIMIT %s""",
                (project_id, limit),
            ).fetchall()
            return [ProjectRepository._execution_task(row) for row in rows]

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
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 180:
            raise ValueError("invalid task idempotency key")
        try:
            outcome_json = json.dumps(outcome, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid task outcome") from exc
        if len(outcome_json) > 32_000:
            raise ValueError("task outcome is too large")
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            existing = connection.execute(
                """SELECT * FROM execution_tasks
                   WHERE project_id = %s AND idempotency_key = %s""",
                (project_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                task = ProjectRepository._execution_task(existing)
                if task.kind != "coverage" or task.source_ids != source_ids:
                    raise ValueError("task idempotency key reused for a different request")
                return task
            source_count = connection.execute(
                "SELECT count(*) FROM sources WHERE project_id = %s AND id = ANY(%s::text[])",
                (project_id, list(source_ids)),
            ).fetchone()["count"]
            if source_count != len(source_ids):
                raise ValueError("source belongs to another project or does not exist")
            row = connection.execute(
                """INSERT INTO execution_tasks
                   (id, project_id, contract_version, kind, mode, status, source_ids_json,
                    outcome_json, idempotency_key, created_at)
                   VALUES (%s, %s, 1, 'coverage', 'native', 'completed', %s, %s, %s, %s)
                   ON CONFLICT (project_id, idempotency_key) DO NOTHING RETURNING *""",
                (
                    str(uuid.uuid4()),
                    project_id,
                    json.dumps(source_ids),
                    outcome_json,
                    idempotency_key,
                    datetime.now(UTC).isoformat(),
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT * FROM execution_tasks
                       WHERE project_id = %s AND idempotency_key = %s""",
                    (project_id, idempotency_key),
                ).fetchone()
            task = ProjectRepository._execution_task(row)
            if task.kind != "coverage" or task.source_ids != source_ids:
                raise ValueError("task idempotency key reused for a different request")
            return task

    def enqueue_intake(self, project_id: str, source_id: str) -> IntakeJob:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            source = connection.execute(
                "SELECT project_id, kind FROM sources WHERE id = %s", (source_id,)
            ).fetchone()
            if source is None:
                raise NotFoundError("source not found")
            if source["project_id"] != project_id:
                raise ValueError("source belongs to another project")
            if source["kind"] != "document":
                raise ValueError("only document sources can be ingested")
            now = datetime.now(UTC).isoformat()
            row = connection.execute(
                """INSERT INTO intake_jobs
                   (id, project_id, source_id, status, attempts, error_code, created_at, updated_at)
                   VALUES (%s, %s, %s, 'queued', 0, NULL, %s, %s)
                   ON CONFLICT (source_id) DO NOTHING RETURNING *""",
                (str(uuid.uuid4()), project_id, source_id, now, now),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM intake_jobs WHERE source_id = %s", (source_id,)
                ).fetchone()
            return ProjectRepository._intake_job(row)

    def get_intake_job(self, project_id: str, job_id: str) -> IntakeJob | None:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = %s AND project_id = %s",
                (job_id, project_id),
            ).fetchone()
            return ProjectRepository._intake_job(row) if row else None

    def list_intake(self, project_id: str) -> list[IntakeJob]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM intake_jobs WHERE project_id = %s ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._intake_job(row) for row in rows]

    def claim_next_intake(self) -> IntakeJob | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE intake_jobs SET status = 'processing', attempts = attempts + 1,
                   updated_at = %s WHERE id = (
                       SELECT id FROM intake_jobs WHERE status = 'queued'
                       ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
                   ) RETURNING *""",
                (datetime.now(UTC).isoformat(),),
            ).fetchone()
            return ProjectRepository._intake_job(row) if row else None

    def complete_intake(
        self,
        project_id: str,
        job_id: str,
        fragments: list[FragmentInput],
        *,
        expected_attempt: int,
        needs_ocr: bool = False,
        native_candidates: tuple[NativeCandidate, ...] = (),
        native_financial_candidates: tuple[FinancialSuggestion, ...] = (),
        native_operating_candidates: tuple[OperatingSuggestion, ...] = (),
        native_topic: str | None = None,
        native_topic_basis: str | None = None,
    ) -> IntakeJob:
        if not fragments and not needs_ocr:
            raise ValueError("completed intake requires readable fragments")
        if len({fragment.ordinal for fragment in fragments}) != len(fragments):
            raise ValueError("fragment ordinals must be unique")
        for fragment in fragments:
            if fragment.ordinal < 1:
                raise ValueError("fragment ordinal must be positive")
            if not fragment.locator.strip() or len(fragment.locator) > 200:
                raise ValueError("fragment locator must contain 1-200 characters")
            if not fragment.text.strip() or len(fragment.text) > 20_000:
                raise ValueError("fragment text must contain 1-20000 characters")
        validate_native_candidates(native_candidates, fragments)
        ProjectRepository._validate_native_index(native_topic, native_topic_basis)
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            job = connection.execute(
                """SELECT * FROM intake_jobs WHERE id = %s AND project_id = %s
                   FOR UPDATE""",
                (job_id, project_id),
            ).fetchone()
            if job is None:
                raise NotFoundError("intake job not found")
            if job["status"] != "processing" or job["attempts"] != expected_attempt:
                raise IntakeConflictError("intake job was reassigned or is not processing")
            for fragment in fragments:
                connection.execute(
                    """INSERT INTO document_fragments
                       (id, project_id, source_id, job_id, ordinal, locator, text)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(uuid.uuid4()),
                        project_id,
                        job["source_id"],
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
                       VALUES (%s, %s, %s, %s, 2, %s)""",
                    (
                        job["source_id"],
                        project_id,
                        native_topic,
                        native_topic_basis,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if not needs_ocr:
                    topics = review_topics(native_topic, native_candidates)
                    if topics:
                        self._create_review_impacts(
                            connection, project_id, job["source_id"], topics
                        )
            if native_candidates:
                source = connection.execute(
                    "SELECT content_sha256 FROM sources WHERE id = %s AND project_id = %s",
                    (job["source_id"], project_id),
                ).fetchone()
                for candidate in native_candidates:
                    fingerprint = ProjectRepository._observation_fingerprint(
                        source_id=job["source_id"],
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
                           VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, %s,
                                   'candidate', 1, %s, %s)
                           ON CONFLICT (project_id, fingerprint) DO NOTHING RETURNING id""",
                        (
                            observation_id,
                            project_id,
                            job["source_id"],
                            candidate.subject,
                            candidate.statement,
                            candidate.source_locator,
                            datetime.now(UTC).isoformat(),
                            fingerprint,
                        ),
                    ).fetchone()
                    if created is not None:
                        self._audit(
                            connection,
                            project_id=project_id,
                            action="candidate.created",
                            actor_kind="system",
                            actor_ref="native_rule_v1",
                            channel="native_intake",
                            subject_id=observation_id,
                            source_id=job["source_id"],
                            source_sha256=source["content_sha256"],
                            source_locator=candidate.source_locator,
                        )
            if native_financial_candidates:
                source = connection.execute(
                    "SELECT reference, content_sha256 FROM sources WHERE id = %s AND project_id = %s",
                    (job["source_id"], project_id),
                ).fetchone()
                validate_financial_suggestions(
                    source["reference"], fragments, native_financial_candidates
                )
                for candidate in native_financial_candidates:
                    fingerprint = ProjectRepository._observation_fingerprint(
                        source_id=job["source_id"],
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
                           VALUES (%s, %s, %s, '财务表现', %s, %s, %s, %s, %s, %s,
                                   'candidate', 1, %s, %s, %s)
                           ON CONFLICT (project_id, fingerprint) DO NOTHING RETURNING id""",
                        (
                            observation_id,
                            project_id,
                            job["source_id"],
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
                    ).fetchone()
                    if created is not None:
                        self._audit(
                            connection,
                            project_id=project_id,
                            action="candidate.created",
                            actor_kind="system",
                            actor_ref="financial_map_v1",
                            channel="native_intake",
                            subject_id=observation_id,
                            source_id=job["source_id"],
                            source_sha256=source["content_sha256"],
                            source_locator=candidate.source_locator,
                        )
            if native_operating_candidates:
                source = connection.execute(
                    "SELECT reference, content_sha256 FROM sources WHERE id = %s AND project_id = %s",
                    (job["source_id"], project_id),
                ).fetchone()
                validate_operating_suggestions(
                    source["reference"], fragments, native_operating_candidates
                )
                for candidate in native_operating_candidates:
                    fingerprint = ProjectRepository._observation_fingerprint(
                        source_id=job["source_id"],
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
                           VALUES (%s, %s, %s, '经营情况', %s, %s, %s, %s, %s, %s,
                                   'candidate', 1, %s, %s, %s)
                           ON CONFLICT (project_id, fingerprint) DO NOTHING RETURNING id""",
                        (
                            observation_id,
                            project_id,
                            job["source_id"],
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
                    ).fetchone()
                    if created is not None:
                        self._audit(
                            connection,
                            project_id=project_id,
                            action="candidate.created",
                            actor_kind="system",
                            actor_ref="operating_map_v1",
                            channel="native_intake",
                            subject_id=observation_id,
                            source_id=job["source_id"],
                            source_sha256=source["content_sha256"],
                            source_locator=candidate.source_locator,
                        )
            row = connection.execute(
                """UPDATE intake_jobs SET status = %s, error_code = NULL,
                   updated_at = %s WHERE id = %s RETURNING *""",
                ("needs_ocr" if needs_ocr else "completed", datetime.now(UTC).isoformat(), job_id),
            ).fetchone()
            return ProjectRepository._intake_job(row)

    @staticmethod
    def _create_review_impacts(
        connection, project_id: str, source_id: str, topics: tuple[str, ...]
    ) -> None:
        now = datetime.now(UTC).isoformat()
        seen: set[str] = set()
        for topic in topics:
            observations = connection.execute(
                """SELECT o.id, o.subject FROM observations o
                   LEFT JOIN document_index old ON old.source_id = o.source_id
                     AND old.project_id = o.project_id
                   WHERE o.project_id = %s AND o.source_id <> %s AND o.status = 'confirmed'
                     AND (o.subject = %s OR old.topic = %s)""",
                (project_id, source_id, topic, topic),
            ).fetchall()
            for observation in observations:
                if observation["id"] in seen:
                    continue
                seen.add(observation["id"])
                connection.execute(
                    """INSERT INTO review_impacts
                       (id, project_id, trigger_source_id, observation_id, topic, status, created_at)
                       VALUES (%s, %s, %s, %s, %s, 'pending', %s)""",
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
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM review_impacts WHERE project_id = %s
                   ORDER BY created_at DESC, id DESC""",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._review_impact(row) for row in rows]

    def resolve_review_impact(
        self,
        project_id: str,
        impact_id: str,
        *,
        resolution: str,
        note: str,
        verified_reviewer_id: str,
    ) -> ReviewImpact:
        note, reviewer = ProjectRepository._validate_impact_resolution(
            resolution, note, verified_reviewer_id
        )
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE review_impacts SET status = 'reviewed', resolution = %s,
                   note = %s, reviewer_ref = %s, reviewed_at = %s
                   WHERE id = %s AND project_id = %s AND status = 'pending' RETURNING *""",
                (resolution, note, reviewer, datetime.now(UTC).isoformat(), impact_id, project_id),
            ).fetchone()
            if row is None:
                self._require_project(connection, project_id)
                existing = connection.execute(
                    "SELECT 1 FROM review_impacts WHERE id = %s AND project_id = %s",
                    (impact_id, project_id),
                ).fetchone()
                if existing is None:
                    raise NotFoundError("review impact not found")
                raise ReviewConflictError("review impact already reviewed")
            source = connection.execute(
                "SELECT content_sha256 FROM sources WHERE id = %s AND project_id = %s",
                (row["trigger_source_id"], project_id),
            ).fetchone()
            self._audit(
                connection,
                project_id=project_id,
                action="impact.reviewed",
                actor_kind="verified_user",
                actor_ref=reviewer,
                channel="team_web",
                subject_id=impact_id,
                source_id=row["trigger_source_id"],
                source_sha256=source["content_sha256"],
            )
            return ProjectRepository._review_impact(row)

    def fail_intake(
        self, project_id: str, job_id: str, *, expected_attempt: int, error_code: str
    ) -> IntakeJob:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ValueError("invalid intake error code")
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE intake_jobs SET status = 'failed', error_code = %s,
                   updated_at = %s WHERE id = %s AND project_id = %s
                   AND status = 'processing' AND attempts = %s RETURNING *""",
                (error_code, datetime.now(UTC).isoformat(), job_id, project_id, expected_attempt),
            ).fetchone()
            if row is None:
                self._require_project(connection, project_id)
                if (
                    connection.execute(
                        "SELECT 1 FROM intake_jobs WHERE id = %s AND project_id = %s",
                        (job_id, project_id),
                    ).fetchone()
                    is None
                ):
                    raise NotFoundError("intake job not found")
                raise IntakeConflictError("intake job was reassigned or is not processing")
            return ProjectRepository._intake_job(row)

    def retry_failed_intake(self, project_id: str, job_id: str) -> IntakeJob:
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE intake_jobs SET status = 'queued', error_code = NULL,
                   updated_at = %s WHERE id = %s AND project_id = %s AND status = 'failed'
                   RETURNING *""",
                (datetime.now(UTC).isoformat(), job_id, project_id),
            ).fetchone()
            if row is None:
                self._require_project(connection, project_id)
                if (
                    connection.execute(
                        "SELECT 1 FROM intake_jobs WHERE id = %s AND project_id = %s",
                        (job_id, project_id),
                    ).fetchone()
                    is None
                ):
                    raise NotFoundError("intake job not found")
                raise IntakeConflictError("only failed intake jobs can be retried")
            return ProjectRepository._intake_job(row)

    def recover_interrupted_intake_jobs(self, *, workers_stopped: bool = False) -> int:
        """Operator-only recovery after every intake worker is confirmed stopped."""
        if workers_stopped is not True:
            raise ValueError("all intake workers must be stopped before recovery")
        with self._pool.connection() as connection:
            return connection.execute(
                """UPDATE intake_jobs SET status = 'queued', updated_at = %s
                   WHERE status = 'processing'""",
                (datetime.now(UTC).isoformat(),),
            ).rowcount

    def list_fragments(self, project_id: str, source_id: str) -> list[DocumentFragment]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            if (
                connection.execute(
                    "SELECT 1 FROM sources WHERE id = %s AND project_id = %s",
                    (source_id, project_id),
                ).fetchone()
                is None
            ):
                raise NotFoundError("source not found")
            rows = connection.execute(
                """SELECT * FROM document_fragments WHERE source_id = %s
                   AND project_id = %s ORDER BY ordinal""",
                (source_id, project_id),
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

    def add_observation(
        self,
        project_id: str,
        *,
        source_id: str,
        subject: str,
        statement: str,
        source_locator: str,
        verified_actor_id: str,
        submission_channel: str = "team_web",
        number: Decimal | None = None,
        unit: str | None = None,
        period: str | None = None,
        scope: str | None = None,
        basis: str = "unknown",
    ) -> Observation:
        if not verified_actor_id or submission_channel not in {"team_web", "team_mcp"}:
            raise ValueError("team proposal requires a verified actor and channel")
        subject, statement, source_locator = (
            subject.strip(),
            statement.strip(),
            source_locator.strip(),
        )
        unit, period, scope = (value.strip() if value else None for value in (unit, period, scope))
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
            if not number.is_finite() or not unit or not period:
                raise ValueError("numeric observation requires finite number, period and unit")
        fingerprint = ProjectRepository._observation_fingerprint(
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
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            source = connection.execute(
                "SELECT project_id, content_sha256 FROM sources WHERE id = %s", (source_id,)
            ).fetchone()
            if source is None:
                raise NotFoundError("source not found")
            if source["project_id"] != project_id:
                raise ValueError("source belongs to another project")
            observation_id = str(uuid.uuid4())
            row = connection.execute(
                """INSERT INTO observations
                   (id, project_id, source_id, subject, statement, number, unit, period,
                    scope, source_locator, status, version, created_at, fingerprint, basis)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'candidate', 1, %s, %s, %s)
                   ON CONFLICT (project_id, fingerprint) DO NOTHING RETURNING *""",
                (
                    observation_id,
                    project_id,
                    source_id,
                    subject,
                    statement,
                    str(number) if number is not None else None,
                    unit,
                    period,
                    scope,
                    source_locator,
                    datetime.now(UTC).isoformat(),
                    fingerprint,
                    basis,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM observations WHERE project_id = %s AND fingerprint = %s",
                    (project_id, fingerprint),
                ).fetchone()
            else:
                self._audit(
                    connection,
                    project_id=project_id,
                    action="candidate.created",
                    actor_kind="verified_user"
                    if submission_channel == "team_web"
                    else "verified_executor",
                    actor_ref=verified_actor_id,
                    channel=submission_channel,
                    subject_id=observation_id,
                    source_id=source_id,
                    source_sha256=str(source["content_sha256"]),
                    source_locator=source_locator,
                )
            return ProjectRepository._observation(row)

    def get_observation(self, project_id: str, observation_id: str) -> Observation | None:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM observations WHERE id = %s AND project_id = %s",
                (observation_id, project_id),
            ).fetchone()
            return ProjectRepository._observation(row) if row else None

    def list_observations(
        self, project_id: str, *, status: ObservationStatus | None = None
    ) -> list[Observation]:
        if status is not None and status not in {"candidate", "confirmed", "rejected"}:
            raise ValueError("invalid observation status")
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            if status is None:
                rows = connection.execute(
                    """SELECT * FROM observations WHERE project_id = %s
                       ORDER BY created_at, id""",
                    (project_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM observations WHERE project_id = %s
                       AND status = %s ORDER BY created_at, id""",
                    (project_id, status),
                ).fetchall()
            return [ProjectRepository._observation(row) for row in rows]

    def confirm_observation(
        self,
        project_id: str,
        observation_id: str,
        *,
        verified_reviewer_id: str,
        expected_version: int,
    ) -> Observation:
        return self._review_observation(
            project_id,
            observation_id,
            action="confirm",
            verified_reviewer_id=verified_reviewer_id,
            expected_version=expected_version,
        )

    def review_observations_batch(
        self,
        project_id: str,
        decisions: tuple[tuple[str, int, Literal["confirm", "reject"]], ...],
        *,
        verified_reviewer_id: str,
    ) -> list[Observation]:
        """Lock every candidate before changing any review state."""
        reviewer = ProjectRepository._validate_batch_review(decisions, verified_reviewer_id)
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            locked = {}
            for observation_id in sorted(item[0] for item in decisions):
                row = connection.execute(
                    """SELECT * FROM observations WHERE id = %s AND project_id = %s
                       FOR UPDATE""",
                    (observation_id, project_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("observation not found")
                locked[observation_id] = row
            for observation_id, expected_version, _ in decisions:
                row = locked[observation_id]
                if row["status"] != "candidate" or row["version"] != expected_version:
                    raise ReviewConflictError("observation is stale or already reviewed")
            result = []
            for observation_id, expected_version, action in decisions:
                original = locked[observation_id]
                updated = connection.execute(
                    """UPDATE observations SET status = %s, version = version + 1
                       WHERE id = %s AND project_id = %s RETURNING *""",
                    (
                        "confirmed" if action == "confirm" else "rejected",
                        observation_id,
                        project_id,
                    ),
                ).fetchone()
                connection.execute(
                    """INSERT INTO review_decisions
                       (id, observation_id, reviewer, action, expected_version, decided_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        str(uuid.uuid4()),
                        observation_id,
                        reviewer,
                        action,
                        expected_version,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._audit(
                    connection,
                    project_id=project_id,
                    action="candidate.confirmed" if action == "confirm" else "candidate.rejected",
                    actor_kind="verified_user",
                    actor_ref=reviewer,
                    channel="team_web",
                    subject_id=observation_id,
                    source_id=str(original["source_id"]),
                    source_locator=str(original["source_locator"]),
                )
                result.append(ProjectRepository._observation(updated))
            return result

    def reject_observation(
        self,
        project_id: str,
        observation_id: str,
        *,
        verified_reviewer_id: str,
        expected_version: int,
    ) -> Observation:
        return self._review_observation(
            project_id,
            observation_id,
            action="reject",
            verified_reviewer_id=verified_reviewer_id,
            expected_version=expected_version,
        )

    def _review_observation(
        self,
        project_id: str,
        observation_id: str,
        *,
        action: Literal["confirm", "reject"],
        verified_reviewer_id: str,
        expected_version: int,
    ) -> Observation:
        if not verified_reviewer_id or len(verified_reviewer_id) > 200:
            raise ValueError("a verified reviewer is required")
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE observations SET status = %s, version = version + 1
                   WHERE id = %s AND project_id = %s AND status = 'candidate'
                     AND version = %s RETURNING *""",
                (
                    "confirmed" if action == "confirm" else "rejected",
                    observation_id,
                    project_id,
                    expected_version,
                ),
            ).fetchone()
            if row is None:
                self._require_project(connection, project_id)
                existing = connection.execute(
                    "SELECT 1 FROM observations WHERE id = %s AND project_id = %s",
                    (observation_id, project_id),
                ).fetchone()
                if existing is None:
                    raise NotFoundError("observation not found")
                raise ReviewConflictError("observation is stale or already reviewed")
            connection.execute(
                """INSERT INTO review_decisions
                   (id, observation_id, reviewer, action, expected_version, decided_at)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    str(uuid.uuid4()),
                    observation_id,
                    verified_reviewer_id,
                    action,
                    expected_version,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._audit(
                connection,
                project_id=project_id,
                action="candidate.confirmed" if action == "confirm" else "candidate.rejected",
                actor_kind="verified_user",
                actor_ref=verified_reviewer_id,
                channel="team_web",
                subject_id=observation_id,
                source_id=str(row["source_id"]),
                source_locator=str(row["source_locator"]),
            )
            return ProjectRepository._observation(row)

    def review_history(self, project_id: str, observation_id: str) -> list[ReviewDecision]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT d.* FROM review_decisions d
                   JOIN observations o ON o.id = d.observation_id
                   WHERE o.id = %s AND o.project_id = %s ORDER BY d.decided_at, d.id""",
                (observation_id, project_id),
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
        """Fetch confirmation decisions for the bounded set shown in a brief."""
        ids = sorted(set(observation_ids))
        if len(ids) > 150:
            raise ValueError("too many report observations")
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            if not ids:
                return []
            placeholders = ",".join("%s" for _ in ids)
            rows = connection.execute(
                f"""SELECT d.* FROM review_decisions d
                    JOIN observations o ON o.id = d.observation_id
                    WHERE o.project_id = %s AND o.id IN ({placeholders})
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
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            if not requested:
                return {}
            values = ",".join("(%s, %s)" for _ in requested)
            rows = connection.execute(
                f"""WITH requested(source_id, locator) AS (VALUES {values})
                    SELECT r.source_id, r.locator,
                      EXISTS(SELECT 1 FROM document_fragments f
                             WHERE f.project_id = %s AND f.source_id = r.source_id
                               AND f.locator = r.locator) AS matched,
                      EXISTS(SELECT 1 FROM document_fragments f
                             WHERE f.project_id = %s AND f.source_id = r.source_id) AS indexed
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

    def list_audit_events(self, project_id: str, *, limit: int = 100) -> list[AuditEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("audit list limit must be between 1 and 500")
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM audit_events WHERE project_id = %s
                   ORDER BY created_at, id LIMIT %s""",
                (project_id, limit),
            ).fetchall()
            return [
                AuditEvent(
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
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            ]

    @staticmethod
    def _plan_query() -> str:
        return """SELECT p.*, (
            SELECT r.status FROM source_runs r WHERE r.plan_id = p.id
            ORDER BY r.started_at DESC, r.id DESC LIMIT 1
        ) AS last_run_status, (
            SELECT r.error_code FROM source_runs r WHERE r.plan_id = p.id
            ORDER BY r.started_at DESC, r.id DESC LIMIT 1
        ) AS last_run_error_code, (
            SELECT r.item_count FROM source_runs r WHERE r.plan_id = p.id
            ORDER BY r.started_at DESC, r.id DESC LIMIT 1
        ) AS last_item_count, (
            SELECT r.matched_count FROM source_runs r WHERE r.plan_id = p.id
            ORDER BY r.started_at DESC, r.id DESC LIMIT 1
        ) AS last_matched_count FROM monitoring_plans p"""

    def create_monitoring_plan(
        self, project_id: str, *, kind: str, target: str, cadence_minutes: int
    ) -> MonitoringPlan:
        target = ProjectRepository._validate_monitoring_target(kind, target, cadence_minutes)
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            plan_id = str(uuid.uuid4())
            row = connection.execute(
                """INSERT INTO monitoring_plans
                   (id, project_id, kind, target, cadence_minutes, enabled, created_at)
                   VALUES (%s, %s, %s, %s, %s, 0, %s)
                   ON CONFLICT (project_id, kind, target) DO NOTHING RETURNING id""",
                (plan_id, project_id, kind, target, cadence_minutes, datetime.now(UTC).isoformat()),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT id FROM monitoring_plans
                       WHERE project_id = %s AND kind = %s AND target = %s""",
                    (project_id, kind, target),
                ).fetchone()
            plan = connection.execute(
                self._plan_query() + " WHERE p.id = %s", (row["id"],)
            ).fetchone()
            return ProjectRepository._monitoring_plan(plan)

    def reserve_brave_news_request(self, monthly_limit: int) -> bool:
        """An atomic global monthly ceiling, including unsuccessful outbound calls."""
        if isinstance(monthly_limit, bool) or not 0 <= monthly_limit <= 100_000:
            raise ValueError("invalid Brave monthly limit")
        if monthly_limit == 0:
            return False
        month = datetime.now(UTC).strftime("%Y-%m")
        with self._pool.connection() as connection:
            row = connection.execute(
                """INSERT INTO brave_news_budget (month_utc, used) VALUES (%s, 1)
                   ON CONFLICT (month_utc) DO UPDATE SET used = brave_news_budget.used + 1
                   WHERE brave_news_budget.used < %s RETURNING used""",
                (month, monthly_limit),
            ).fetchone()
            return row is not None

    def brave_news_usage(self) -> int:
        month = datetime.now(UTC).strftime("%Y-%m")
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT used FROM brave_news_budget WHERE month_utc = %s", (month,)
            ).fetchone()
            return int(row["used"]) if row is not None else 0

    def get_monitoring_plan(self, project_id: str, plan_id: str) -> MonitoringPlan | None:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                self._plan_query() + " WHERE p.id = %s AND p.project_id = %s",
                (plan_id, project_id),
            ).fetchone()
            return ProjectRepository._monitoring_plan(row) if row else None

    def list_monitoring_plans(self, project_id: str) -> list[MonitoringPlan]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                self._plan_query() + " WHERE p.project_id = %s ORDER BY p.created_at, p.id",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._monitoring_plan(row) for row in rows]

    def set_monitoring_plan_enabled(
        self, project_id: str, plan_id: str, enabled: bool
    ) -> MonitoringPlan:
        if not isinstance(enabled, bool):
            raise ValueError("monitoring plan enabled must be boolean")
        with self._pool.connection() as connection:
            changed = connection.execute(
                """UPDATE monitoring_plans SET enabled = %s
                   WHERE id = %s AND project_id = %s RETURNING id""",
                (int(enabled), plan_id, project_id),
            ).fetchone()
            if changed is None:
                raise NotFoundError("monitoring plan not found")
            row = connection.execute(self._plan_query() + " WHERE p.id = %s", (plan_id,)).fetchone()
            return ProjectRepository._monitoring_plan(row)

    def request_monitoring_run(
        self, project_id: str, plan_id: str, *, idempotency_key: str
    ) -> MonitoringJob:
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 180
            or idempotency_key.startswith("scheduled:")
        ):
            raise ValueError("invalid monitoring job idempotency key")
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            plan = connection.execute(
                "SELECT 1 FROM monitoring_plans WHERE id = %s AND project_id = %s",
                (plan_id, project_id),
            ).fetchone()
            if plan is None:
                raise NotFoundError("monitoring plan not found")
            now = datetime.now(UTC).isoformat()
            row = connection.execute(
                """INSERT INTO monitoring_jobs
                   (id, project_id, plan_id, idempotency_key, trigger, status, attempts,
                    lease_until, run_id, error_code, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, 'manual', 'queued', 0,
                           NULL, NULL, NULL, %s, %s)
                   ON CONFLICT (plan_id, idempotency_key) DO NOTHING RETURNING *""",
                (str(uuid.uuid4()), project_id, plan_id, idempotency_key, now, now),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT * FROM monitoring_jobs
                       WHERE plan_id = %s AND idempotency_key = %s""",
                    (plan_id, idempotency_key),
                ).fetchone()
            return ProjectRepository._monitoring_job(row)

    def schedule_due_monitoring(self, *, now: datetime | None = None) -> list[MonitoringJob]:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("schedule time must be timezone-aware")
        scheduled: list[MonitoringJob] = []
        with self._pool.connection() as connection:
            plans = connection.execute(
                """SELECT p.*, (
                       SELECT MAX(r.finished_at) FROM source_runs r WHERE r.plan_id = p.id
                   ) AS last_run_at FROM monitoring_plans p
                   WHERE p.enabled = 1 ORDER BY p.created_at, p.id"""
            ).fetchall()
            for plan in plans:
                outstanding = connection.execute(
                    """SELECT 1 FROM monitoring_jobs WHERE plan_id = %s
                       AND status IN ('queued', 'processing') LIMIT 1""",
                    (plan["id"],),
                ).fetchone()
                if outstanding is not None:
                    continue
                last_run_at = (
                    datetime.fromisoformat(plan["last_run_at"]) if plan["last_run_at"] else None
                )
                due_at = (
                    last_run_at + timedelta(minutes=int(plan["cadence_minutes"]))
                    if last_run_at
                    else datetime.fromisoformat(plan["created_at"])
                )
                if due_at > now:
                    continue
                row = connection.execute(
                    """INSERT INTO monitoring_jobs
                       (id, project_id, plan_id, idempotency_key, trigger, status, attempts,
                        lease_until, run_id, error_code, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, 'scheduled', 'queued', 0,
                               NULL, NULL, NULL, %s, %s)
                       ON CONFLICT (plan_id, idempotency_key) DO NOTHING RETURNING *""",
                    (
                        str(uuid.uuid4()),
                        plan["project_id"],
                        plan["id"],
                        "scheduled:" + due_at.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                ).fetchone()
                if row is not None:
                    scheduled.append(ProjectRepository._monitoring_job(row))
        return scheduled

    def claim_next_monitoring_job(
        self, *, now: datetime | None = None, lease_seconds: int = 300
    ) -> MonitoringJob | None:
        now = now or datetime.now(UTC)
        if now.tzinfo is None or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid monitoring job lease")
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE monitoring_jobs SET status = 'processing',
                   attempts = attempts + 1, lease_until = %s, updated_at = %s
                   WHERE id = (
                       SELECT id FROM monitoring_jobs
                       WHERE status = 'queued'
                          OR (status = 'processing' AND lease_until <= %s)
                       ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
                   ) RETURNING *""",
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            ).fetchone()
            return ProjectRepository._monitoring_job(row) if row else None

    def fail_monitoring_job(
        self, project_id: str, job_id: str, *, expected_attempt: int, error_code: str
    ) -> MonitoringJob:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ValueError("invalid monitoring job error code")
        now = datetime.now(UTC).isoformat()
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE monitoring_jobs SET status = 'failed', error_code = %s,
                   lease_until = NULL, updated_at = %s
                   WHERE id = %s AND project_id = %s AND status = 'processing'
                     AND attempts = %s AND lease_until > %s RETURNING *""",
                (error_code, now, job_id, project_id, expected_attempt, now),
            ).fetchone()
            if row is None:
                self._require_project(connection, project_id)
                if (
                    connection.execute(
                        "SELECT 1 FROM monitoring_jobs WHERE id = %s AND project_id = %s",
                        (job_id, project_id),
                    ).fetchone()
                    is None
                ):
                    raise NotFoundError("monitoring job not found")
                raise IntakeConflictError("monitoring job lease is stale or not processing")
            return ProjectRepository._monitoring_job(row)

    def retry_failed_monitoring_job(self, project_id: str, job_id: str) -> MonitoringJob:
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE monitoring_jobs SET status = 'queued', error_code = NULL,
                   lease_until = NULL, updated_at = %s
                   WHERE id = %s AND project_id = %s AND status = 'failed'
                   RETURNING *""",
                (datetime.now(UTC).isoformat(), job_id, project_id),
            ).fetchone()
            if row is None:
                self._require_project(connection, project_id)
                if (
                    connection.execute(
                        "SELECT 1 FROM monitoring_jobs WHERE id = %s AND project_id = %s",
                        (job_id, project_id),
                    ).fetchone()
                    is None
                ):
                    raise NotFoundError("monitoring job not found")
                raise IntakeConflictError("only failed monitoring jobs can be retried")
            return ProjectRepository._monitoring_job(row)

    def get_monitoring_job(self, project_id: str, job_id: str) -> MonitoringJob | None:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT * FROM monitoring_jobs WHERE id = %s AND project_id = %s",
                (job_id, project_id),
            ).fetchone()
            return ProjectRepository._monitoring_job(row) if row else None

    def list_monitoring_jobs(self, project_id: str) -> list[MonitoringJob]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM monitoring_jobs WHERE project_id = %s
                   ORDER BY created_at, id""",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._monitoring_job(row) for row in rows]

    @staticmethod
    def _validate_run_result(
        status: SourceRunStatus, error_code: str | None, signals: tuple[SignalInput, ...]
    ) -> None:
        if status not in {"completed", "no_results", "unreachable", "rate_limited", "not_run"}:
            raise ValueError("invalid source run status")
        if status == "rate_limited":
            if error_code not in {"rate_limited", "quota_exhausted"}:
                raise ValueError("rate-limited source requires its error code")
        elif status == "unreachable":
            if error_code is None or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
                raise ValueError("unreachable source requires an error code")
        elif error_code is not None:
            raise ValueError("error code requires unreachable status")
        if signals and status != "completed":
            raise ValueError("only completed runs can contain signals")
        if len(signals) > 500:
            raise ValueError("too many source signals")
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

    def finish_monitoring_job(
        self,
        project_id: str,
        job_id: str,
        *,
        expected_attempt: int,
        status: SourceRunStatus,
        error_code: str | None = None,
        signals: tuple[SignalInput, ...] = (),
        fetched_count: int | None = None,
    ) -> tuple[MonitoringJob, SourceRun]:
        """Atomically fence a worker, record its receipt/signals and close the job."""
        self._validate_run_result(status, error_code, signals)
        if fetched_count is None:
            fetched_count = len(signals)
        if (
            isinstance(fetched_count, bool)
            or not isinstance(fetched_count, int)
            or not len(signals) <= fetched_count <= 500
            or (status != "completed" and fetched_count != 0)
        ):
            raise ValueError("invalid fetched item count")
        now = datetime.now(UTC)
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            job = connection.execute(
                """SELECT * FROM monitoring_jobs WHERE id = %s AND project_id = %s
                   FOR UPDATE""",
                (job_id, project_id),
            ).fetchone()
            if job is None:
                raise NotFoundError("monitoring job not found")
            if (
                job["status"] != "processing"
                or job["attempts"] != expected_attempt
                or job["lease_until"] is None
                or datetime.fromisoformat(job["lease_until"]) <= now
            ):
                raise IntakeConflictError("monitoring job lease is stale or not processing")
            started_at = datetime.fromisoformat(job["updated_at"])
            finished_at = max(now, started_at)
            run_id = str(uuid.uuid4())
            inserted = connection.execute(
                """INSERT INTO source_runs
                   (id, project_id, plan_id, status, error_code, started_at,
                    finished_at, idempotency_key, item_count, matched_count, new_count)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
                   ON CONFLICT (plan_id, idempotency_key) DO NOTHING RETURNING id""",
                (
                    run_id,
                    project_id,
                    job["plan_id"],
                    status,
                    error_code,
                    job["updated_at"],
                    finished_at.isoformat(),
                    job["idempotency_key"],
                    fetched_count,
                    len(signals),
                ),
            ).fetchone()
            if inserted is None:
                raise IntakeConflictError("monitoring run already recorded for this job")
            new_count = 0
            for signal in signals:
                content = json.dumps(
                    [signal.title, signal.url, signal.summary],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                created = connection.execute(
                    """INSERT INTO signals
                       (id, project_id, plan_id, run_id, external_id, title, url, summary,
                        published_at, first_seen_at, content_sha256, freshness, status)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'candidate')
                       ON CONFLICT (plan_id, external_id, content_sha256)
                       DO NOTHING RETURNING id""",
                    (
                        str(uuid.uuid4()),
                        project_id,
                        job["plan_id"],
                        run_id,
                        signal.external_id.strip(),
                        signal.title.strip(),
                        signal.url,
                        signal.summary,
                        signal.published_at.isoformat() if signal.published_at else None,
                        now.isoformat(),
                        content_hash,
                        signal.freshness,
                    ),
                ).fetchone()
                if created is not None and signal.freshness == "current":
                    new_count += 1
            connection.execute(
                "UPDATE source_runs SET new_count = %s WHERE id = %s", (new_count, run_id)
            )
            finished = connection.execute(
                """UPDATE monitoring_jobs SET status = 'done', run_id = %s,
                   lease_until = NULL, updated_at = %s WHERE id = %s RETURNING *""",
                (run_id, finished_at.isoformat(), job_id),
            ).fetchone()
            receipt = connection.execute(
                "SELECT * FROM source_runs WHERE id = %s", (run_id,)
            ).fetchone()
            return ProjectRepository._monitoring_job(finished), ProjectRepository._source_run(
                receipt
            )

    def list_source_runs(self, project_id: str, plan_id: str) -> list[SourceRun]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            if (
                connection.execute(
                    "SELECT 1 FROM monitoring_plans WHERE id = %s AND project_id = %s",
                    (plan_id, project_id),
                ).fetchone()
                is None
            ):
                raise NotFoundError("monitoring plan not found")
            rows = connection.execute(
                """SELECT * FROM source_runs WHERE plan_id = %s AND project_id = %s
                   ORDER BY started_at, id""",
                (plan_id, project_id),
            ).fetchall()
            return [ProjectRepository._source_run(row) for row in rows]

    def list_latest_source_runs(self, project_id: str) -> list[SourceRun]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT DISTINCT ON (plan_id) * FROM source_runs
                   WHERE project_id = %s
                   ORDER BY plan_id, finished_at DESC, started_at DESC, id DESC""",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._source_run(row) for row in rows]

    def list_signals(self, project_id: str) -> list[Signal]:
        with self._pool.connection() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                """SELECT * FROM signals WHERE project_id = %s
                   ORDER BY first_seen_at DESC, id DESC""",
                (project_id,),
            ).fetchall()
            return [ProjectRepository._signal(row) for row in rows]
