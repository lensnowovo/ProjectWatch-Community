"""Small, explicit domain records for the independent monitoring product."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

IdentityStatus = Literal["unverified", "verified"]

RESEARCH_TOPICS = (
    "公司与团队",
    "产品与技术",
    "经营情况",
    "财务表现",
    "行业与竞争",
    "风险与合规",
)


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    identity_status: IdentityStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IdentityReview:
    id: str
    project_id: str
    version: int
    action: Literal["verify", "reopen"]
    legal_name: str | None
    registration_code: str | None
    evidence_source_id: str | None
    evidence_locator: str | None
    evidence_url: str | None
    note: str
    actor_ref: str
    created_at: datetime


ObservationStatus = Literal["candidate", "confirmed", "rejected"]
MeasurementBasis = Literal["actual", "forecast", "budget", "unknown"]


@dataclass(frozen=True, slots=True)
class SourceVersion:
    id: str
    project_id: str
    kind: str
    reference: str
    content_sha256: str
    created_at: datetime
    topic: str | None = None
    topic_basis: str | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    id: str
    project_id: str
    source_id: str
    subject: str
    statement: str
    number: Decimal | None
    unit: str | None
    period: str | None
    scope: str | None
    source_locator: str
    status: ObservationStatus
    version: int
    created_at: datetime
    basis: MeasurementBasis = "unknown"


@dataclass(frozen=True, slots=True)
class CompetitivePeer:
    id: str
    project_id: str
    name: str
    linked_project_id: str | None
    status: Literal["candidate", "confirmed", "rejected"]
    version: int
    proposed_by: str | None
    reviewed_by: str | None
    note: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class HoldingSnapshot:
    id: str
    project_id: str
    fund_name: str
    as_of: date
    source_id: str
    source_locator: str
    invested_cost_wan: Decimal | None
    ownership_pct: Decimal | None
    current_value_wan: Decimal | None
    status: Literal["candidate", "confirmed", "rejected"]
    version: int
    proposed_by: str | None
    reviewed_by: str | None
    review_note: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WatchItem:
    id: str
    project_id: str
    observation_id: str
    current_observation_id: str
    title: str
    question: str
    owner: str | None
    due_on: date | None
    status: Literal["candidate", "watching", "action_required", "rejected"]
    version: int
    proposed_by: str | None
    reviewed_by: str | None
    review_note: str | None
    initial_question: str
    initial_owner: str | None
    initial_due_on: date | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WatchItemChange:
    id: str
    project_id: str
    watch_item_id: str
    base_version: int
    target_status: Literal["watching", "action_required", "resolved"]
    question: str
    owner: str | None
    due_on: date | None
    evidence_observation_id: str | None
    reason: str
    status: Literal["candidate", "accepted", "rejected"]
    proposed_by: str | None
    reviewed_by: str | None
    review_note: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    id: str
    observation_id: str
    reviewer: str
    action: Literal["confirm", "reject"]
    expected_version: int
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewImpact:
    id: str
    project_id: str
    trigger_source_id: str
    observation_id: str
    topic: str
    status: Literal["pending", "reviewed"]
    resolution: Literal["unchanged", "follow_up"] | None
    note: str | None
    reviewer_ref: str | None
    created_at: datetime
    reviewed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: str
    project_id: str
    action: str
    actor_kind: str
    actor_ref: str | None
    channel: str
    subject_id: str
    source_id: str | None
    source_sha256: str | None
    source_locator: str | None
    created_at: datetime


IntakeStatus = Literal["queued", "processing", "completed", "needs_ocr", "failed"]


@dataclass(frozen=True, slots=True)
class IntakeJob:
    id: str
    project_id: str
    source_id: str
    status: IntakeStatus
    attempts: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FragmentInput:
    ordinal: int
    locator: str
    text: str


@dataclass(frozen=True, slots=True)
class DocumentFragment:
    id: str
    project_id: str
    source_id: str
    job_id: str
    ordinal: int
    locator: str
    text: str


def normalized_project_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("project name must be text")
    normalized = " ".join(name.split())
    if not normalized or len(normalized) > 200:
        raise ValueError("project name must contain 1-200 characters")
    return normalized


SourceRunStatus = Literal["completed", "no_results", "unreachable", "rate_limited", "not_run"]


@dataclass(frozen=True, slots=True)
class MonitoringPlan:
    id: str
    project_id: str
    kind: Literal["rss"]
    target: str
    cadence_minutes: int
    enabled: bool
    last_run_status: SourceRunStatus
    created_at: datetime
    last_item_count: int | None = None
    last_matched_count: int | None = None
    last_run_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SourceRun:
    id: str
    project_id: str
    plan_id: str
    status: SourceRunStatus
    error_code: str | None
    started_at: datetime
    finished_at: datetime
    idempotency_key: str | None = None
    item_count: int = 0
    matched_count: int | None = None
    new_count: int = 0


@dataclass(frozen=True, slots=True)
class SignalInput:
    external_id: str
    title: str
    url: str
    summary: str
    published_at: datetime | None
    freshness: Literal["current", "historical", "unknown"]


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    project_id: str
    plan_id: str
    run_id: str
    external_id: str
    title: str
    url: str
    summary: str
    published_at: datetime | None
    first_seen_at: datetime
    content_sha256: str
    freshness: Literal["current", "historical", "unknown"]
    status: Literal["candidate"]


@dataclass(frozen=True, slots=True)
class MonitoringJob:
    id: str
    project_id: str
    plan_id: str
    idempotency_key: str
    trigger: Literal["manual", "scheduled"]
    status: Literal["queued", "processing", "done", "failed"]
    attempts: int
    lease_until: datetime | None
    run_id: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionTask:
    id: str
    project_id: str
    contract_version: int
    kind: Literal["coverage"]
    mode: Literal["native"]
    status: Literal["completed"]
    source_ids: tuple[str, ...]
    outcome: dict[str, object]
    idempotency_key: str
    created_at: datetime
