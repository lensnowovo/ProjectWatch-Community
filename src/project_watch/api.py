"""Local development API over the independent project-watch domain service."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, computed_field

from project_watch.analysis.competitive_comparison import (
    compare_competitive_metrics,
    project_competitive_metrics,
)
from project_watch.analysis.financial_mapping import map_financial_fragments
from project_watch.analysis.financial_projection import screen_confirmed_financials
from project_watch.analysis.monitoring_projection import build_monitoring_projection
from project_watch.analysis.operating_mapping import map_operating_fragments
from project_watch.blob_store import BlobStore
from project_watch.brave_news import (
    DISCLOSURE as BRAVE_DISCLOSURE,
)
from project_watch.brave_news import (
    BraveNewsAdapter,
    brave_news_target,
    validate_brave_news_configuration,
)
from project_watch.domain import (
    Observation,
    SourceVersion,
    WatchItem,
    WatchItemChange,
)
from project_watch.feeds import configured_public_feed_adapter
from project_watch.intake_worker import IntakeWorker
from project_watch.metric_series import build_metric_series
from project_watch.monitoring import MonitoringAdapter, MonitoringRunner
from project_watch.parser_container import validate_parser_runtime
from project_watch.public_search import (
    CADENCE_MINUTES,
    DISCLOSURE,
    INITIAL_RUN_KEY,
    gdelt_rss_target,
    parse_loopback_proxy,
)
from project_watch.report import (
    attach_locator_status,
    attach_review_proof,
    brief_observation_ids,
    brief_source_locators,
    build_current_brief,
)
from project_watch.repository import (
    IdentityConflictError,
    IntakeConflictError,
    NotFoundError,
    ProjectRepository,
    ReviewConflictError,
)
from project_watch.scheduler import MonitoringScheduler
from project_watch.signal_projection import deduplicate_project_signals
from project_watch.source_comparison import compare_fragments
from project_watch.static_files import NoStoreStaticFiles
from project_watch.tasks import EvidenceConflictError, TaskService
from project_watch.team_http import (
    SESSION_COOKIE,
    LoginAttemptLimiter,
    protect_request,
    validate_team_origin,
)
from project_watch.watch_items import watch_item_snapshot

if TYPE_CHECKING:
    from project_watch.team_auth import TeamAuthStore, TeamSession


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ProjectCreate(APIModel):
    name: str


class ProjectView(APIModel):
    id: str
    name: str
    identity_status: Literal["unverified", "verified"]
    created_at: datetime


class IdentityReviewView(APIModel):
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
    actor_name: str | None = None
    created_at: datetime


class ProjectIdentityState(APIModel):
    status: Literal["unverified", "verified"]
    history: list[IdentityReviewView]


class IdentityVerificationCreate(APIModel):
    legal_name: str = Field(min_length=2, max_length=200)
    registration_code: str | None = Field(default=None, max_length=60)
    evidence_source_id: str | None = None
    evidence_locator: str | None = Field(default=None, max_length=200)
    evidence_url: str | None = Field(default=None, max_length=1000)
    note: str = Field(min_length=5, max_length=500)
    expected_version: int = Field(ge=0)


class IdentityReopenCreate(APIModel):
    note: str = Field(min_length=5, max_length=500)
    expected_version: int = Field(ge=0)


class ProjectCapabilitiesView(APIModel):
    read: bool
    propose: bool
    review: bool
    manage: bool


class SourceCreate(APIModel):
    kind: Literal["document", "url", "note"]
    reference: str
    content_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")


class SourceView(APIModel):
    id: str
    project_id: str
    kind: str
    reference: str
    content_sha256: str
    created_at: datetime
    topic: str | None = None
    topic_basis: str | None = None


class ObservationCreate(APIModel):
    source_id: str
    subject: str
    statement: str
    source_locator: str
    number: Decimal | None = None
    unit: str | None = None
    period: str | None = None
    scope: str | None = None
    basis: Literal["actual", "forecast", "budget", "unknown"] = "unknown"


class ObservationView(APIModel):
    id: str
    project_id: str
    source_id: str
    subject: str
    statement: str
    source_locator: str
    number: Decimal | None
    unit: str | None
    period: str | None
    scope: str | None
    basis: Literal["actual", "forecast", "budget", "unknown"]
    status: Literal["candidate", "confirmed", "rejected"]
    version: int
    created_at: datetime


    @computed_field
    @property
    def quality_hint(self) -> str | None:
        from project_watch.native_intake import excerpt_quality_hint

        if self.status != "candidate" or self.number is not None:
            return None
        return excerpt_quality_hint(self.statement, self.source_locator)


class CompetitivePeerCreate(APIModel):
    name: str = Field(min_length=1, max_length=200)
    linked_project_id: str | None = None


class CompetitivePeerReview(APIModel):
    action: Literal["confirm", "reject"]
    expected_version: int = Field(ge=1)
    reviewer: str | None = None
    note: str | None = Field(default=None, max_length=500)


class CompetitivePeerView(APIModel):
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


class HoldingSnapshotCreate(APIModel):
    fund_name: str = Field(min_length=1, max_length=200)
    as_of: date
    source_id: str
    source_locator: str = Field(min_length=1, max_length=500)
    invested_cost_wan: Decimal | None = None
    ownership_pct: Decimal | None = None
    current_value_wan: Decimal | None = None


class HoldingSnapshotReview(APIModel):
    action: Literal["confirm", "reject"]
    expected_version: int = Field(ge=1)
    reviewer: str | None = None
    note: str | None = Field(default=None, max_length=500)


class HoldingSnapshotView(APIModel):
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


class WatchItemCreate(APIModel):
    observation_id: str
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=1000)
    owner: str | None = Field(default=None, max_length=128)
    due_on: date | None = None


class WatchItemReview(APIModel):
    action: Literal["watch", "require_action", "reject"]
    expected_version: int = Field(ge=1)
    reviewer: str | None = None
    note: str | None = Field(default=None, max_length=500)


class WatchItemChangeCreate(APIModel):
    expected_version: int = Field(ge=2)
    target_status: Literal["watching", "action_required", "resolved"]
    question: str = Field(min_length=1, max_length=1000)
    owner: str | None = Field(default=None, max_length=128)
    due_on: date | None = None
    evidence_observation_id: str | None = None
    reason: str = Field(min_length=5, max_length=500)


class WatchItemChangeReview(APIModel):
    action: Literal["accept", "reject"]
    expected_version: int = Field(ge=2)
    reviewer: str | None = None
    note: str | None = Field(default=None, max_length=500)


class WatchItemChangeView(APIModel):
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


class WatchItemView(APIModel):
    id: str
    project_id: str
    observation_id: str
    current_observation_id: str
    observation_version: int
    title: str
    question: str
    owner: str | None
    due_on: date | None
    status: Literal["candidate", "watching", "action_required", "rejected", "resolved"]
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
    source_id: str
    source_reference: str
    source_sha256: str
    source_locator: str


def watch_item_views(
    items: list[WatchItem], observations: list[Observation], sources: list[SourceVersion]
) -> list[WatchItemView]:
    observations_by_id = {row.id: row for row in observations}
    sources_by_id = {row.id: row for row in sources}
    return [
        WatchItemView.model_validate(watch_item_snapshot(row, observations_by_id, sources_by_id))
        for row in items
    ]


def watch_change_views(changes: list[WatchItemChange]) -> list[WatchItemChangeView]:
    return [WatchItemChangeView.model_validate(change) for change in changes]


class MetricPointView(APIModel):
    observation_id: str
    observation_version: int
    period: str
    number: Decimal
    source_id: str
    source_reference: str
    source_sha256: str
    source_locator: str
    same_period_conflict: bool


class MetricChangeView(APIModel):
    status: Literal[
        "comparable",
        "non_actual",
        "unsupported_basis",
        "missing_previous",
        "same_period_conflict",
        "source_unavailable",
        "nonpositive_baseline",
        "uncomputable",
    ]
    previous_period: str | None = None
    period: str | None = None
    absolute: Decimal | None = None
    percent: Decimal | None = None
    previous_observation_id: str | None = None
    observation_id: str | None = None


class MetricSeriesView(APIModel):
    subject: str
    name: str
    unit: str
    scope: str | None
    basis: Literal["actual", "forecast", "budget", "unknown"]
    cadence: Literal["year", "quarter", "month"]
    total_points: int
    truncated_points: bool
    points: list[MetricPointView]
    latest_change: MetricChangeView


class MetricSeriesResponse(APIModel):
    series: list[MetricSeriesView]
    total_series: int
    truncated: bool
    unsequenced_count: int


class ReviewRequest(APIModel):
    reviewer: str | None = None
    expected_version: int = Field(ge=1)


class BatchReviewItem(APIModel):
    observation_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    action: Literal["confirm", "reject"]


class BatchReviewRequest(APIModel):
    reviewer: str | None = None
    items: list[BatchReviewItem] = Field(min_length=1, max_length=20)


class ReviewView(APIModel):
    id: str
    observation_id: str
    reviewer: str
    action: Literal["confirm", "reject"]
    expected_version: int
    decided_at: datetime


class ReviewImpactView(APIModel):
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


class ReviewImpactResolution(APIModel):
    resolution: Literal["unchanged", "follow_up"]
    note: str = Field(min_length=1, max_length=500)
    reviewer: str | None = None


class SummaryView(APIModel):
    project: ProjectView
    source_count: int
    candidate_count: int
    confirmed_count: int


class IntakeView(APIModel):
    id: str
    project_id: str
    source_id: str
    status: Literal["queued", "processing", "completed", "needs_ocr", "failed"]
    attempts: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class FragmentView(APIModel):
    id: str
    project_id: str
    source_id: str
    job_id: str
    ordinal: int
    locator: str
    text: str


class FinancialReviewDraftView(APIModel):
    metric: str
    number: str
    unit: str | None
    period: str | None
    scope: str | None
    basis: str
    source_locator: str
    source_cell: str
    column_header: str
    missing_fields: tuple[str, ...]
    context_locators: tuple[str, ...]


class FinancialMappingIssueView(APIModel):
    source_locator: str
    reason: Literal[
        "scope_or_unit_missing",
        "unit_missing",
        "currency_unsupported",
        "period_missing",
        "basis_ambiguous",
        "trial_balance_not_closing",
        "format_unit_conflict",
    ]


class FinancialMappingView(APIModel):
    candidate_count: int
    truncated_count: int
    issues: list[FinancialMappingIssueView]
    review_drafts: list[FinancialReviewDraftView] = []
    review_truncated_count: int = 0


class OperatingMappingIssueView(APIModel):
    source_locator: str
    reason: Literal[
        "scope_missing",
        "unit_missing",
        "period_missing",
        "basis_missing",
        "basis_ambiguous",
        "output_not_actual",
        "invalid_utilization",
        "invalid_quantity",
        "percent_scale_ambiguous",
    ]


class OperatingMappingView(APIModel):
    candidate_count: int
    truncated_count: int
    issues: list[OperatingMappingIssueView]


class SourceChangeView(APIModel):
    locator: str
    kind: Literal["added", "changed", "removed"]
    before: str | None
    after: str | None


class SourceComparisonView(APIModel):
    before_source_id: str
    after_source_id: str
    before_sha256: str
    after_sha256: str
    counts: dict[str, int]
    changes: list[SourceChangeView]
    total_locations: int
    compared_locations: int
    truncated: bool
    excerpt_truncated: bool


class FileUploadView(APIModel):
    source: SourceView
    job: IntakeView


class MonitoringPlanCreate(APIModel):
    kind: Literal["rss"]
    target: str
    cadence_minutes: int = Field(ge=15, le=10080)


class MonitoringPlanPatch(APIModel):
    enabled: bool


class MonitoringPlanView(APIModel):
    id: str
    project_id: str
    kind: Literal["rss", "brave_news"]
    target: str
    cadence_minutes: int
    enabled: bool
    last_run_status: Literal["completed", "no_results", "unreachable", "rate_limited", "not_run"]
    created_at: datetime
    last_item_count: int | None = None
    last_matched_count: int | None = None
    last_run_error_code: str | None = None


class MonitoringRunRequest(APIModel):
    idempotency_key: str = Field(min_length=1, max_length=180)


class PublicSearchStart(APIModel):
    provider: Literal["gdelt", "brave"]


class ExternalFocusStart(APIModel):
    provider: Literal['gdelt', 'brave', 'bing']
    category: Literal['company', 'sector', 'competitors', 'policy', 'industry']
    term: str = Field(min_length=2, max_length=120)
    consent_version: str = Field(min_length=1, max_length=80)


class PublicSearchPreview(APIModel):
    provider: Literal["gdelt", "brave"]
    project_name: str
    target: str | None
    disclosure: str
    eligible: bool
    monthly_limit: int | None = None
    monthly_used: int | None = None


class MonitoringJobView(APIModel):
    id: str
    project_id: str
    plan_id: str
    idempotency_key: str
    trigger: Literal["manual", "scheduled", "strategy"]
    status: Literal["queued", "processing", "done", "failed"]
    attempts: int
    lease_until: datetime | None
    run_id: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class PublicSearchStarted(APIModel):
    plan: MonitoringPlanView
    job: MonitoringJobView


class SourceRunView(APIModel):
    id: str
    project_id: str
    plan_id: str
    status: Literal["completed", "no_results", "unreachable", "rate_limited", "not_run"]
    error_code: str | None
    started_at: datetime
    finished_at: datetime
    idempotency_key: str | None
    item_count: int
    matched_count: int | None
    new_count: int


class SignalView(APIModel):
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


class TaskCreate(APIModel):
    contract_version: Literal[1]
    kind: Literal["coverage"]
    source_ids: tuple[str, ...] = Field(max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=180)


class TaskView(APIModel):
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


class CandidateProposalCreate(APIModel):
    source_id: str
    expected_source_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    source_locator: str
    subject: str
    statement: str
    number: Decimal | None = None
    unit: str | None = None
    period: str | None = None
    scope: str | None = None
    basis: Literal["actual", "forecast", "budget", "unknown"] = "unknown"


class AuditEventView(APIModel):
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


class TeamLogin(APIModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class TeamUserCreate(APIModel):
    username: str
    password: str


class TeamPasswordReset(APIModel):
    password: str = Field(min_length=12, max_length=128)


class TeamUserView(APIModel):
    id: str
    username: str
    global_admin: bool
    active: bool


class TeamRolePatch(APIModel):
    role: Literal["viewer", "analyst", "reviewer", "admin"]


class TeamMemberCreate(TeamRolePatch):
    username: str = Field(min_length=3, max_length=80)


class TeamMemberView(APIModel):
    user_id: str
    username: str
    role: Literal["viewer", "analyst", "reviewer", "admin"]
    active: bool


class TeamSessionView(APIModel):
    user_id: str
    username: str
    global_admin: bool
    csrf_token: str


class TeamSecurityEventView(APIModel):
    id: str
    actor_user_id: str | None
    action: str
    target_user_id: str | None
    project_id: str | None
    subject_id: str | None
    previous_role: str | None
    new_role: str | None
    created_at: datetime


class ExecutorTokenIssue(APIModel):
    label: str = Field(min_length=1, max_length=80)
    permission: Literal["read", "propose"]
    duration_minutes: int = Field(default=60, ge=5, le=480)


class ExecutorTokenView(APIModel):
    id: str
    project_id: str
    issued_by: str
    label: str
    permission: Literal["read", "propose"]
    expires_at: datetime
    revoked_at: datetime | None


class ExecutorTokenIssued(ExecutorTokenView):
    token: str


def create_app(
    db_path: Path | str,
    *,
    start_worker: bool = False,
    monitoring_adapters: dict[str, MonitoringAdapter] | None = None,
    gdelt_proxy_url: str | None = None,
    fixed_feed_proxy_url: str | None = None,
    fixed_feed_targets: tuple[str, ...] = (),
    brave_api_key: str | None = None,
    brave_monthly_limit: int = 0,
    brave_proxy_url: str | None = None,
    team_auth: TeamAuthStore | None = None,
    team_origin: str | None = None,
    parser_image: str | None = None,
    parser_docker_host: str | None = None,
    proposal_model=None,
    isolated_intake: bool = False,
) -> FastAPI:
    """Create a local preview or HTTPS-only authenticated team API."""

    if team_auth is not None:
        if fixed_feed_proxy_url or fixed_feed_targets:
            raise ValueError('fixed public feed proxy is currently local-only')
        if Path(team_auth.db_path).resolve() != Path(db_path).resolve():
            raise ValueError("team identity store must use the same database")
        parsed_origin = validate_team_origin(team_origin)
    else:
        parsed_origin = None

    if (parser_image is None) != (parser_docker_host is None):
        raise ValueError("parser image and local Docker host must be configured together")
    if parser_image is not None and parser_docker_host is not None:
        validate_parser_runtime(parser_image, parser_docker_host)

    default_feed_adapter = configured_public_feed_adapter(
        gdelt_proxy_url, fixed_feed_proxy_url=fixed_feed_proxy_url, fixed_feed_targets=fixed_feed_targets)
    from project_watch.bing_news import SearchFeedAdapter
    default_feed_adapter = SearchFeedAdapter(default_feed_adapter)
    validate_brave_news_configuration(brave_api_key, brave_monthly_limit, brave_proxy_url)
    repository = ProjectRepository(db_path)
    brave_adapter = BraveNewsAdapter(
        brave_api_key,
        monthly_limit=brave_monthly_limit,
        reserve=repository.reserve_brave_news_request,
        proxy_url=brave_proxy_url,
    )
    task_service = TaskService(repository)
    if team_auth is not None and proposal_model is not None:
        raise ValueError("manual model execution is local-only in this release")
    # Personal/local intake accepts whole research documents. Team/container
    # admission quotas remain explicit at the untrusted upload boundary.
    from project_watch.file_validation import DEFAULT_MAX_BYTES

    blob_store = BlobStore(
        Path(db_path).parent / "objects",
        max_bytes=DEFAULT_MAX_BYTES if team_auth is not None or parser_image else None,
    )
    worker = IntakeWorker(
        repository, blob_store, isolated=team_auth is not None or isolated_intake,
        parser_image=parser_image, docker_host=parser_docker_host,
    )
    monitoring_runner = MonitoringRunner(
        repository,
        monitoring_adapters
        if monitoring_adapters is not None
        else {
            "rss": default_feed_adapter,
            "brave_news": brave_adapter,
        },
    )
    monitoring_scheduler = MonitoringScheduler(repository, monitoring_runner)
    from project_watch.source_fallback import SourceFallbackStore
    from project_watch.source_fallback_api import attach_source_fallback
    source_router = None if team_auth else SourceFallbackStore(repository)
    if source_router is not None and monitoring_adapters is None:
        monitoring_runner.source_router = source_router

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            worker.start()
            monitoring_scheduler.start()
            if reading_worker is not None:
                reading_worker.start()
        try:
            yield
        finally:
            if start_worker:
                if reading_worker is not None:
                    reading_worker.stop()
                monitoring_scheduler.stop()
                worker.stop()

    app = FastAPI(
        title="Project Watch — team alpha" if team_auth else "Project Watch — development preview",
        version="0.1.0a0",
        lifespan=lifespan,
        docs_url=None if team_auth else "/docs",
        redoc_url=None if team_auth else "/redoc",
        openapi_url=None if team_auth else "/openapi.json",
    )
    app.state.intake_worker = worker
    app.state.monitoring_scheduler = monitoring_scheduler
    from project_watch.manual_model_api import attach_manual_model_routes
    from project_watch.model_settings import ModelSettings, attach_model_settings
    from project_watch.reading_authorization_api import attach_reading_authorization
    from project_watch.workspace_api import attach_workspace_routes

    model_settings = ModelSettings(proposal_model)
    attach_model_settings(app, model_settings, team_mode=team_auth is not None)
    attach_workspace_routes(app, repository, team_mode=team_auth is not None)
    attach_manual_model_routes(app, repository, model_settings, team_mode=team_auth is not None)
    attach_reading_authorization(app, repository, model_settings, team_mode=team_auth is not None)
    from project_watch.reading_worker import ReadingWorker
    reading_worker = None if team_auth else ReadingWorker(repository, app.state.reading_jobs, model_settings)
    app.state.reading_worker = reading_worker
    app.state.reading_worker_enabled = start_worker and reading_worker is not None
    from project_watch.monitor_autopilot import MonitorAutopilot
    from project_watch.monitor_autopilot_api import attach_monitor_autopilot
    monitor_autopilot = None if team_auth else MonitorAutopilot(repository, model_settings)
    app.state.monitor_autopilot = monitor_autopilot
    if reading_worker is not None:
        reading_worker.monitor_autopilot = monitor_autopilot
        monitor_autopilot.stopping = reading_worker._stop.is_set
    attach_monitor_autopilot(app, repository, model_settings, monitor_autopilot)
    from project_watch.external_analysis_jobs import ExternalAnalysisStore, ExternalAnalysisWorker
    external_store = None if team_auth else ExternalAnalysisStore(repository)
    external_worker = None if team_auth else ExternalAnalysisWorker(external_store, model_settings)
    app.state.external_analysis_store = external_store
    app.state.external_analysis_worker = external_worker
    if reading_worker is not None:
        reading_worker.external_worker = external_worker
    from project_watch.external_analysis_api import attach_external_analysis
    attach_external_analysis(app, repository, model_settings, store=external_store, worker=external_worker,
                             team_mode=team_auth is not None)
    from project_watch.monitor_activation_api import attach_monitor_activation
    attach_source_fallback(app, repository, source_router)
    attach_monitor_activation(app, repository, team_mode=team_auth is not None,
                              brave_available=lambda: bool(brave_api_key) and
                              brave_monthly_limit > repository.brave_news_usage())
    if team_auth is not None:
        app.state.login_limiter = LoginAttemptLimiter()
    static_root = Path(__file__).parent / "static"
    app.mount("/static", NoStoreStaticFiles(directory=static_root), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        return await protect_request(
            request, call_next, team_auth=team_auth, team_origin=parsed_origin
        )

    @app.get("/", include_in_schema=False)
    def web_preview() -> FileResponse:
        return FileResponse(static_root / "index.html", media_type="text/html")

    @app.get("/api/runtime")
    def runtime_info() -> dict:
        return {
            "mode": "team" if team_auth else "local",
            "parser": "container" if parser_image else "subprocess" if (
                team_auth or isolated_intake) else "in_process",
            "private_local_intake": bool(parser_image and not team_auth),
            "authenticated_review": team_auth is not None,
        }

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ReviewConflictError)
    async def conflict_handler(_request, exc: ReviewConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(EvidenceConflictError)
    async def evidence_conflict_handler(_request, exc: EvidenceConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(IntakeConflictError)
    async def intake_conflict_handler(_request, exc: IntakeConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error_handler(_request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    def current_session(request: Request) -> TeamSession | None:
        return getattr(request.state, "team_session", None) if team_auth is not None else None

    def require_project(request: Request, project_id: str, permission: str) -> None:
        if team_auth is None:
            return
        from project_watch.team_auth import AuthorizationError

        session = current_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            team_auth.require(session.user_id, project_id, permission)
        except AuthorizationError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc

    def visible_peer(request: Request, peer: object) -> CompetitivePeerView:
        view = CompetitivePeerView.model_validate(peer)
        if view.linked_project_id:
            try:
                require_project(request, view.linked_project_id, "read")
            except HTTPException:
                return view.model_copy(update={"linked_project_id": None})
        return view

    def require_global_admin(request: Request) -> None:
        if team_auth is not None:
            session = current_session(request)
            if session is None or not session.global_admin:
                raise HTTPException(status_code=403, detail="admin required")

    if team_auth is not None:

        @app.post("/api/auth/login", response_model=TeamSessionView)
        def team_login(body: TeamLogin, request: Request, response: Response) -> TeamSessionView:
            from project_watch.team_auth import AuthenticationError

            if not app.state.login_limiter.allow(
                request.client.host if request.client else None, body.username
            ):
                raise HTTPException(
                    status_code=429,
                    detail="too many login attempts",
                    headers={"Retry-After": "60"},
                )
            try:
                token, csrf = team_auth.login(body.username, body.password)
            except AuthenticationError as exc:
                raise HTTPException(status_code=401, detail="invalid credentials") from exc
            session = team_auth.session(token)
            assert session is not None
            response.set_cookie(
                key=SESSION_COOKIE,
                value=token,
                max_age=8 * 3600,
                path="/",
                secure=True,
                httponly=True,
                samesite="strict",
            )
            response.headers["Cache-Control"] = "no-store"
            return TeamSessionView(
                user_id=session.user_id,
                username=session.username,
                global_admin=session.global_admin,
                csrf_token=csrf,
            )

        @app.get("/api/auth/session", response_model=TeamSessionView)
        def team_session(request: Request, response: Response) -> TeamSessionView:
            session = current_session(request)
            assert session is not None
            response.headers["Cache-Control"] = "no-store"
            return TeamSessionView(
                user_id=session.user_id,
                username=session.username,
                global_admin=session.global_admin,
                csrf_token=team_auth.csrf_for(request.state.team_token),
            )

        @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
        def team_logout(request: Request, response: Response) -> None:
            team_auth.logout(request.state.team_token)
            response.delete_cookie(SESSION_COOKIE, path="/", secure=True, samesite="strict")

        @app.post(
            "/api/team/users", response_model=TeamUserView, status_code=status.HTTP_201_CREATED
        )
        def create_team_user(request: Request, body: TeamUserCreate) -> TeamUserView:
            require_global_admin(request)
            try:
                user = team_auth.create_user(
                    body.username,
                    body.password,
                    actor_user_id=current_session(request).user_id,
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="username already exists") from exc
            return TeamUserView.model_validate(user)

        @app.get("/api/team/security-events", response_model=list[TeamSecurityEventView])
        def list_team_security_events(request: Request) -> list[TeamSecurityEventView]:
            require_global_admin(request)
            return [
                TeamSecurityEventView.model_validate(event)
                for event in team_auth.list_security_events()
            ]

        @app.post(
            "/api/team/users/{user_id}/unlock",
            status_code=status.HTTP_204_NO_CONTENT,
            response_model=None,
        )
        def unlock_team_user(request: Request, user_id: str) -> None:
            require_global_admin(request)
            if team_auth.get_user(user_id) is None:
                raise HTTPException(status_code=404, detail="user not found")
            team_auth.unlock_user(user_id, actor_user_id=current_session(request).user_id)

        @app.post(
            "/api/team/users/{user_id}/reset-password",
            status_code=status.HTTP_204_NO_CONTENT,
            response_model=None,
        )
        def reset_team_password(
            request: Request, response: Response, user_id: str, body: TeamPasswordReset
        ) -> None:
            require_global_admin(request)
            try:
                team_auth.reset_password(
                    user_id, body.password, actor_user_id=current_session(request).user_id
                )
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="user not found") from exc
            response.headers["Cache-Control"] = "no-store"

        @app.post(
            "/api/team/users/{user_id}/deactivate",
            status_code=status.HTTP_204_NO_CONTENT,
            response_model=None,
        )
        def deactivate_team_user(request: Request, user_id: str) -> None:
            require_global_admin(request)
            user = team_auth.get_user(user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="user not found")
            if user.global_admin:
                raise HTTPException(
                    status_code=409, detail="bootstrap administrator cannot be disabled"
                )
            team_auth.deactivate_user(user_id, actor_user_id=current_session(request).user_id)

        @app.put(
            "/api/projects/{project_id}/members/{user_id}",
            status_code=status.HTTP_204_NO_CONTENT,
            response_model=None,
        )
        def grant_team_member(
            request: Request, project_id: str, user_id: str, body: TeamRolePatch
        ) -> None:
            require_project(request, project_id, "manage")
            team_auth.grant_project_role(
                project_id, user_id, body.role, actor_user_id=current_session(request).user_id
            )

        @app.get("/api/projects/{project_id}/members", response_model=list[TeamMemberView])
        def list_team_members(
            request: Request,
            response: Response,
            project_id: str,
            limit: int = Query(default=100, ge=1, le=100),
            after_username: str | None = None,
        ) -> list[TeamMemberView]:
            require_project(request, project_id, "manage")
            response.headers["Cache-Control"] = "no-store"
            return [
                TeamMemberView.model_validate(member)
                for member in team_auth.list_project_members(
                    project_id, limit=limit, after_username=after_username
                )
            ]

        @app.post(
            "/api/projects/{project_id}/members",
            response_model=TeamMemberView,
            status_code=status.HTTP_201_CREATED,
        )
        def assign_team_member(
            request: Request, response: Response, project_id: str, body: TeamMemberCreate
        ) -> TeamMemberView:
            require_project(request, project_id, "manage")
            user = team_auth.find_active_user(body.username)
            if user is None:
                raise HTTPException(status_code=404, detail="active team account not found")
            try:
                team_auth.grant_project_role(
                    project_id, user.id, body.role, actor_user_id=current_session(request).user_id
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=404, detail="active team account not found"
                ) from exc
            response.headers["Cache-Control"] = "no-store"
            return TeamMemberView(
                user_id=user.id, username=user.username, role=body.role, active=True
            )

        @app.delete(
            "/api/projects/{project_id}/members/{user_id}",
            status_code=status.HTTP_204_NO_CONTENT,
            response_model=None,
        )
        def revoke_team_member(request: Request, project_id: str, user_id: str) -> None:
            require_project(request, project_id, "manage")
            team_auth.revoke_project_role(
                project_id, user_id, actor_user_id=current_session(request).user_id
            )

        @app.post(
            "/api/projects/{project_id}/executor-tokens",
            response_model=ExecutorTokenIssued,
            status_code=status.HTTP_201_CREATED,
        )
        def issue_executor_token(
            request: Request,
            response: Response,
            project_id: str,
            body: ExecutorTokenIssue,
        ) -> ExecutorTokenIssued:
            from project_watch.team_auth import AuthorizationError

            require_project(request, project_id, "manage")
            try:
                secret, grant = team_auth.issue_executor_token(
                    project_id,
                    current_session(request).user_id,
                    label=body.label,
                    permission=body.permission,
                    duration_minutes=body.duration_minutes,
                )
            except AuthorizationError as exc:
                raise HTTPException(status_code=404, detail="project not found") from exc
            response.headers["Cache-Control"] = "no-store"
            return ExecutorTokenIssued(
                **ExecutorTokenView.model_validate(grant).model_dump(), token=secret
            )

        @app.get(
            "/api/projects/{project_id}/executor-tokens",
            response_model=list[ExecutorTokenView],
        )
        def list_executor_tokens(request: Request, project_id: str) -> list[ExecutorTokenView]:
            require_project(request, project_id, "manage")
            return [
                ExecutorTokenView.model_validate(grant)
                for grant in team_auth.list_executor_tokens(project_id)
            ]

        @app.delete(
            "/api/projects/{project_id}/executor-tokens/{token_id}",
            status_code=status.HTTP_204_NO_CONTENT,
            response_model=None,
        )
        def revoke_executor_token(request: Request, project_id: str, token_id: str) -> None:
            from project_watch.team_auth import AuthorizationError

            require_project(request, project_id, "manage")
            try:
                revoked = team_auth.revoke_executor_token(
                    project_id, token_id, actor_user_id=current_session(request).user_id
                )
            except AuthorizationError as exc:
                raise HTTPException(status_code=404, detail="project not found") from exc
            if not revoked:
                raise HTTPException(status_code=404, detail="executor token not found")

    @app.post("/api/projects", response_model=ProjectView, status_code=status.HTTP_201_CREATED)
    def create_project(request: Request, body: ProjectCreate) -> ProjectView:
        require_global_admin(request)
        session = current_session(request)
        return ProjectView.model_validate(
            repository.create_project(body.name, actor_id=session.user_id if session else None)
        )

    @app.get("/api/projects", response_model=list[ProjectView])
    def list_projects(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        after_created_at: str | None = None,
        after_id: str | None = None,
    ) -> list[ProjectView]:
        if (after_created_at is None) != (after_id is None):
            raise HTTPException(status_code=422, detail="both cursor fields are required")
        projects = repository.list_projects()
        if team_auth is not None:
            from project_watch.team_auth import AuthorizationError

            session = current_session(request)
            assert session is not None
            visible = []
            for project in projects:
                try:
                    team_auth.require(session.user_id, project.id, "read")
                    visible.append(project)
                except AuthorizationError:
                    continue
            projects = visible
        if after_created_at is not None:
            try:
                after = datetime.fromisoformat(after_created_at.replace("Z", "+00:00"))
                if after.tzinfo is None:
                    raise ValueError("timezone required")
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid project cursor") from exc
            projects = [row for row in projects if (row.created_at, row.id) > (after, after_id)]
        return [ProjectView.model_validate(row) for row in projects[:limit]]

    @app.get("/api/projects/{project_id}", response_model=ProjectView)
    def get_project(request: Request, project_id: str) -> ProjectView:
        require_project(request, project_id, "read")
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return ProjectView.model_validate(project)

    @app.get("/api/projects/{project_id}/identity", response_model=ProjectIdentityState)
    def project_identity(
        request: Request, response: Response, project_id: str
    ) -> ProjectIdentityState:
        require_project(request, project_id, "read")
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        response.headers["Cache-Control"] = "no-store"

        def named_review(row):
            user = team_auth.get_user(row.actor_ref) if team_auth is not None else None
            return IdentityReviewView.model_validate(row).model_copy(
                update={"actor_name": user.username if user else None}
            )

        return ProjectIdentityState(
            status=project.identity_status,
            history=[named_review(row) for row in repository.list_identity_reviews(project_id)],
        )

    if team_auth is not None:

        @app.post(
            "/api/projects/{project_id}/identity",
            response_model=IdentityReviewView,
            status_code=status.HTTP_201_CREATED,
        )
        def verify_project_identity(
            request: Request,
            response: Response,
            project_id: str,
            body: IdentityVerificationCreate,
        ) -> IdentityReviewView:
            require_project(request, project_id, "manage")
            try:
                review = repository.verify_project_identity(
                    project_id,
                    **body.model_dump(),
                    actor_user_id=current_session(request).user_id,
                )
            except IdentityConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            response.headers["Cache-Control"] = "no-store"
            return IdentityReviewView.model_validate(review).model_copy(
                update={"actor_name": current_session(request).username}
            )

        @app.post(
            "/api/projects/{project_id}/identity/reopen",
            response_model=IdentityReviewView,
            status_code=status.HTTP_201_CREATED,
        )
        def reopen_project_identity(
            request: Request,
            response: Response,
            project_id: str,
            body: IdentityReopenCreate,
        ) -> IdentityReviewView:
            require_project(request, project_id, "manage")
            try:
                review = repository.reopen_project_identity(
                    project_id,
                    note=body.note,
                    expected_version=body.expected_version,
                    actor_user_id=current_session(request).user_id,
                )
            except IdentityConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            response.headers["Cache-Control"] = "no-store"
            return IdentityReviewView.model_validate(review).model_copy(
                update={"actor_name": current_session(request).username}
            )

    @app.get("/api/projects/{project_id}/capabilities", response_model=ProjectCapabilitiesView)
    def project_capabilities(request: Request, project_id: str) -> ProjectCapabilitiesView:
        require_project(request, project_id, "read")
        if repository.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        if team_auth is None:
            return ProjectCapabilitiesView(read=True, propose=True, review=True, manage=True)
        from project_watch.team_auth import AuthorizationError

        session = current_session(request)
        assert session is not None
        allowed = {"read": True}
        for permission in ("propose", "review", "manage"):
            try:
                team_auth.require(session.user_id, project_id, permission)
                allowed[permission] = True
            except AuthorizationError:
                allowed[permission] = False
        return ProjectCapabilitiesView(**allowed)

    @app.post(
        "/api/projects/{project_id}/sources",
        response_model=SourceView,
        status_code=status.HTTP_201_CREATED,
    )
    def add_source(request: Request, project_id: str, body: SourceCreate) -> SourceView:
        require_project(request, project_id, "propose")
        source = repository.add_source(project_id, **body.model_dump())
        return SourceView.model_validate(source)

    @app.get("/api/projects/{project_id}/sources", response_model=list[SourceView])
    def list_sources(request: Request, project_id: str) -> list[SourceView]:
        require_project(request, project_id, "read")
        return [SourceView.model_validate(row) for row in repository.list_sources(project_id)]

    @app.post(
        "/api/projects/{project_id}/files",
        response_model=FileUploadView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def upload_file(
        request: Request, project_id: str, file: UploadFile = File(...)
    ) -> FileUploadView:
        require_project(request, project_id, "propose")
        if repository.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        stored = blob_store.put_stream(file.filename or "", file.file)
        source = repository.add_source(
            project_id,
            kind="document",
            reference=stored.display_name,
            content_sha256=stored.sha256,
        )
        job = repository.enqueue_intake(project_id, source.id)
        return FileUploadView(
            source=SourceView.model_validate(source), job=IntakeView.model_validate(job)
        )

    @app.get("/api/projects/{project_id}/intake", response_model=list[IntakeView])
    def list_intake(request: Request, project_id: str) -> list[IntakeView]:
        require_project(request, project_id, "read")
        return [IntakeView.model_validate(row) for row in repository.list_intake(project_id)]

    @app.get("/api/sources/{source_id}/fragments", response_model=list[FragmentView])
    def list_fragments(request: Request, source_id: str) -> list[FragmentView]:
        source = repository.get_source(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        require_project(request, source.project_id, "read")
        return [FragmentView.model_validate(row) for row in repository.list_fragments(source_id)]

    @app.get(
        "/api/projects/{project_id}/sources/{source_id}/fragments",
        response_model=list[FragmentView],
    )
    def project_fragments(request: Request, project_id: str, source_id: str) -> list[FragmentView]:
        require_project(request, project_id, "read")
        source = repository.get_source(source_id)
        if source is None or source.project_id != project_id:
            raise HTTPException(status_code=404, detail="source not found")
        return list_fragments(request, source_id)

    @app.get(
        "/api/projects/{project_id}/sources/{source_id}/financial-mapping",
        response_model=FinancialMappingView,
    )
    def financial_mapping(
        request: Request, project_id: str, source_id: str
    ) -> FinancialMappingView:
        require_project(request, project_id, "read")
        source = repository.get_source(source_id)
        if source is None or source.project_id != project_id or source.kind != "document":
            raise HTTPException(status_code=404, detail="document source not found")
        job = next(
            (item for item in repository.list_intake(project_id) if item.source_id == source_id),
            None,
        )
        if job is None or job.status != "completed":
            raise HTTPException(status_code=409, detail="document extraction is not complete")
        from project_watch.analysis.financial_drafts import financial_review_drafts

        fragments = repository.list_fragments(source_id)
        result = map_financial_fragments(source.reference, fragments)
        drafts, truncated = financial_review_drafts(source.reference, fragments)
        return FinancialMappingView(
            candidate_count=len(result.candidates),
            truncated_count=result.truncated_count,
            issues=[FinancialMappingIssueView.model_validate(item) for item in result.issues],
            review_drafts=[FinancialReviewDraftView.model_validate(item) for item in drafts],
            review_truncated_count=truncated,
        )

    @app.get(
        "/api/projects/{project_id}/sources/{source_id}/operating-mapping",
        response_model=OperatingMappingView,
    )
    def operating_mapping(
        request: Request, project_id: str, source_id: str
    ) -> OperatingMappingView:
        require_project(request, project_id, "read")
        source = repository.get_source(source_id)
        if source is None or source.project_id != project_id or source.kind != "document":
            raise HTTPException(status_code=404, detail="document source not found")
        job = next(
            (item for item in repository.list_intake(project_id) if item.source_id == source_id),
            None,
        )
        if job is None or job.status != "completed":
            raise HTTPException(status_code=409, detail="document extraction is not complete")
        result = map_operating_fragments(source.reference, repository.list_fragments(source_id))
        return OperatingMappingView(
            candidate_count=len(result.candidates),
            truncated_count=result.truncated_count,
            issues=[OperatingMappingIssueView.model_validate(item) for item in result.issues],
        )

    @app.get(
        "/api/projects/{project_id}/sources/{source_id}/compare",
        response_model=SourceComparisonView,
    )
    def compare_sources(
        request: Request, project_id: str, source_id: str, before_source_id: str
    ) -> SourceComparisonView:
        require_project(request, project_id, "read")
        if source_id == before_source_id:
            raise HTTPException(status_code=422, detail="choose two distinct sources")
        before = repository.get_source(before_source_id)
        after = repository.get_source(source_id)
        if (
            before is None
            or after is None
            or before.project_id != project_id
            or after.project_id != project_id
            or before.kind != "document"
            or after.kind != "document"
        ):
            raise HTTPException(status_code=404, detail="document source not found")
        completed = {
            job.source_id for job in repository.list_intake(project_id) if job.status == "completed"
        }
        if before.id not in completed or after.id not in completed:
            raise HTTPException(status_code=409, detail="both documents must finish extraction")
        return SourceComparisonView(
            before_source_id=before.id,
            after_source_id=after.id,
            before_sha256=before.content_sha256,
            after_sha256=after.content_sha256,
            **compare_fragments(
                repository.list_fragments(before.id), repository.list_fragments(after.id)
            ),
        )

    @app.post("/api/intake/{job_id}/retry", response_model=IntakeView)
    def retry_intake(request: Request, job_id: str) -> IntakeView:
        job = repository.get_intake_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="intake job not found")
        require_project(request, job.project_id, "propose")
        return IntakeView.model_validate(repository.retry_failed_intake(job_id))

    @app.post("/api/projects/{project_id}/intake/{job_id}/retry", response_model=IntakeView)
    def project_retry_intake(request: Request, project_id: str, job_id: str) -> IntakeView:
        require_project(request, project_id, "propose")
        job = repository.get_intake_job(job_id)
        if job is None or job.project_id != project_id:
            raise HTTPException(status_code=404, detail="intake job not found")
        return retry_intake(request, job_id)

    @app.post(
        "/api/projects/{project_id}/observations",
        response_model=ObservationView,
        status_code=status.HTTP_201_CREATED,
    )
    def add_observation(
        request: Request, project_id: str, body: ObservationCreate
    ) -> ObservationView:
        require_project(request, project_id, "propose")
        session = current_session(request)
        observation = repository.add_observation(
            project_id,
            **body.model_dump(),
            submission_channel="team_web" if session is not None else "preview_api",
            actor_id=session.user_id if session is not None else None,
        )
        return ObservationView.model_validate(observation)

    @app.get("/api/projects/{project_id}/observations", response_model=list[ObservationView])
    def list_observations(
        request: Request,
        project_id: str,
        status_filter: Literal["candidate", "confirmed", "rejected"] | None = None,
    ) -> list[ObservationView]:
        require_project(request, project_id, "read")
        return [
            ObservationView.model_validate(row)
            for row in repository.list_observations(project_id, status=status_filter)
        ]

    @app.get("/api/projects/{project_id}/watch-items", response_model=list[WatchItemView])
    def list_watch_items(request: Request, project_id: str) -> list[WatchItemView]:
        require_project(request, project_id, "read")
        return watch_item_views(
            repository.list_watch_items(project_id),
            repository.list_observations(project_id),
            repository.list_sources(project_id),
        )

    @app.post(
        "/api/projects/{project_id}/watch-items", response_model=WatchItemView, status_code=201
    )
    def propose_watch_item(
        request: Request, project_id: str, body: WatchItemCreate
    ) -> WatchItemView:
        require_project(request, project_id, "propose")
        verified = current_session(request)
        item = repository.propose_watch_item(
            project_id, **body.model_dump(), actor_id=verified.user_id if verified else None
        )
        return watch_item_views(
            [item], repository.list_observations(project_id), repository.list_sources(project_id)
        )[0]

    @app.post(
        "/api/projects/{project_id}/watch-items/{item_id}/review", response_model=WatchItemView
    )
    def review_watch_item(
        request: Request, project_id: str, item_id: str, body: WatchItemReview
    ) -> WatchItemView:
        require_project(request, project_id, "review")
        verified = current_session(request)
        reviewer = verified.user_id if verified else body.reviewer
        if not reviewer:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        item = repository.review_watch_item(
            project_id,
            item_id,
            action=body.action,
            expected_version=body.expected_version,
            reviewer=reviewer,
            reviewer_verified=verified is not None,
            note=body.note,
        )
        return watch_item_views(
            [item], repository.list_observations(project_id), repository.list_sources(project_id)
        )[0]

    @app.get(
        "/api/projects/{project_id}/watch-item-changes",
        response_model=list[WatchItemChangeView],
    )
    def list_watch_item_changes(request: Request, project_id: str) -> list[WatchItemChangeView]:
        require_project(request, project_id, "read")
        return watch_change_views(repository.list_watch_item_changes(project_id))

    @app.post(
        "/api/projects/{project_id}/watch-items/{item_id}/changes",
        response_model=WatchItemChangeView,
        status_code=201,
    )
    def propose_watch_item_change(
        request: Request, project_id: str, item_id: str, body: WatchItemChangeCreate
    ) -> WatchItemChangeView:
        require_project(request, project_id, "propose")
        verified = current_session(request)
        change = repository.propose_watch_item_change(
            project_id,
            item_id,
            **body.model_dump(),
            actor_id=verified.user_id if verified else None,
        )
        return WatchItemChangeView.model_validate(change)

    @app.post(
        "/api/projects/{project_id}/watch-items/{item_id}/changes/{change_id}/review",
        response_model=WatchItemView,
    )
    def review_watch_item_change(
        request: Request,
        project_id: str,
        item_id: str,
        change_id: str,
        body: WatchItemChangeReview,
    ) -> WatchItemView:
        require_project(request, project_id, "review")
        verified = current_session(request)
        reviewer = verified.user_id if verified else body.reviewer
        if not reviewer:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        item = repository.review_watch_item_change(
            project_id,
            item_id,
            change_id,
            action=body.action,
            expected_version=body.expected_version,
            reviewer=reviewer,
            reviewer_verified=verified is not None,
            note=body.note,
        )
        return watch_item_views(
            [item], repository.list_observations(project_id), repository.list_sources(project_id)
        )[0]

    @app.get(
        "/api/projects/{project_id}/competitive-peers", response_model=list[CompetitivePeerView]
    )
    def list_competitive_peers(request: Request, project_id: str) -> list[CompetitivePeerView]:
        require_project(request, project_id, "read")
        return [visible_peer(request, row) for row in repository.list_competitive_peers(project_id)]

    @app.get("/api/projects/{project_id}/holding-snapshots", response_model=list[HoldingSnapshotView])
    def list_holding_snapshots(request: Request, project_id: str) -> list[HoldingSnapshotView]:
        require_project(request, project_id, "read")
        return [HoldingSnapshotView.model_validate(row) for row in repository.list_holding_snapshots(project_id)]

    @app.post(
        "/api/projects/{project_id}/holding-snapshots",
        response_model=HoldingSnapshotView,
        status_code=201,
    )
    def propose_holding_snapshot(
        request: Request, project_id: str, body: HoldingSnapshotCreate
    ) -> HoldingSnapshotView:
        require_project(request, project_id, "propose")
        verified = current_session(request)
        return HoldingSnapshotView.model_validate(
            repository.propose_holding_snapshot(
                project_id, **body.model_dump(), actor_id=verified.user_id if verified else None,
            )
        )

    @app.post(
        "/api/projects/{project_id}/holding-snapshots/{snapshot_id}/review",
        response_model=HoldingSnapshotView,
    )
    def review_holding_snapshot(
        request: Request, project_id: str, snapshot_id: str, body: HoldingSnapshotReview
    ) -> HoldingSnapshotView:
        require_project(request, project_id, "review")
        verified = current_session(request)
        reviewer = verified.user_id if verified else body.reviewer
        if not reviewer:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        return HoldingSnapshotView.model_validate(
            repository.review_holding_snapshot(
                project_id, snapshot_id, action=body.action,
                expected_version=body.expected_version, reviewer=reviewer,
                reviewer_verified=verified is not None, note=body.note,
            )
        )

    @app.post(
        "/api/projects/{project_id}/competitive-peers",
        response_model=CompetitivePeerView,
        status_code=201,
    )
    def propose_competitive_peer(
        request: Request, project_id: str, body: CompetitivePeerCreate
    ) -> CompetitivePeerView:
        require_project(request, project_id, "propose")
        if body.linked_project_id:
            require_project(request, body.linked_project_id, "read")
        verified = current_session(request)
        return CompetitivePeerView.model_validate(
            repository.propose_competitive_peer(
                project_id,
                name=body.name,
                linked_project_id=body.linked_project_id,
                actor_id=verified.user_id if verified else None,
            )
        )

    @app.post(
        "/api/projects/{project_id}/competitive-peers/{peer_id}/review",
        response_model=CompetitivePeerView,
    )
    def review_competitive_peer(
        request: Request, project_id: str, peer_id: str, body: CompetitivePeerReview
    ) -> CompetitivePeerView:
        require_project(request, project_id, "review")
        verified = current_session(request)
        reviewer = verified.user_id if verified else body.reviewer
        if not reviewer:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        return CompetitivePeerView.model_validate(
            repository.review_competitive_peer(
                project_id,
                peer_id,
                action=body.action,
                expected_version=body.expected_version,
                reviewer=reviewer,
                reviewer_verified=verified is not None,
                note=body.note,
            )
        )

    @app.get("/api/projects/{project_id}/competitive-position")
    def competitive_position(request: Request, project_id: str) -> dict[str, object]:
        require_project(request, project_id, "read")
        peers = repository.list_competitive_peers(project_id)
        approved = [peer for peer in peers if peer.status == "confirmed"]
        metrics = project_competitive_metrics(
            project_id,
            project_id,
            repository.list_observations(project_id),
            repository.list_sources(project_id),
        )
        peer_access: dict[str, str] = {}
        for peer in approved:
            if not peer.linked_project_id:
                peer_access[peer.id] = "no_linked_project"
                continue
            try:
                require_project(request, peer.linked_project_id, "read")
            except HTTPException:
                peer_access[peer.id] = "unavailable"
                continue
            metrics.extend(
                project_competitive_metrics(
                    peer.id,
                    peer.linked_project_id,
                    repository.list_observations(peer.linked_project_id),
                    repository.list_sources(peer.linked_project_id),
                )
            )
            peer_access[peer.id] = "readable"
        return {
            "peers": [visible_peer(request, peer).model_dump(mode="json") for peer in peers],
            "peer_access": peer_access,
            "comparison": compare_competitive_metrics(
                project_id, [peer.id for peer in approved], metrics
            ),
        }

    @app.get("/api/projects/{project_id}/metric-series", response_model=MetricSeriesResponse)
    def metric_series(request: Request, project_id: str) -> MetricSeriesResponse:
        require_project(request, project_id, "read")
        return MetricSeriesResponse.model_validate(
            build_metric_series(
                repository.list_observations(project_id, status="confirmed"),
                repository.list_sources(project_id),
            )
        )

    @app.get("/api/projects/{project_id}/financial-screen")
    def financial_screen(request: Request, project_id: str) -> dict[str, object]:
        require_project(request, project_id, "read")
        return screen_confirmed_financials(
            repository.list_observations(project_id), repository.list_sources(project_id)
        )

    @app.get("/api/projects/{project_id}/monitoring/domains")
    def monitoring_domains(request: Request, project_id: str) -> dict[str, object]:
        require_project(request, project_id, "read")
        return build_monitoring_projection(
            project_id,
            repository.list_observations(project_id),
            repository.list_sources(project_id),
            repository.list_monitoring_plans(project_id),
            runs=repository.list_latest_source_runs(project_id),
            holding_snapshots=repository.list_holding_snapshots(project_id),
        )

    @app.get("/api/projects/{project_id}/report/current")
    def current_report(request: Request, response: Response, project_id: str) -> dict[str, object]:
        require_project(request, project_id, "read")
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        response.headers["Cache-Control"] = "no-store"
        brief = build_current_brief(
            project,
            repository.list_observations(project_id),
            repository.list_sources(project_id),
            repository.list_monitoring_plans(project_id),
            repository.list_signals(project_id),
            repository.list_review_impacts(project_id),
            runs=repository.list_latest_source_runs(project_id),
            watch_items=repository.list_watch_items(project_id),
            watch_changes=repository.list_watch_item_changes(project_id),
            holding_snapshots=repository.list_holding_snapshots(project_id),
        )
        attach_review_proof(
            brief,
            repository.review_decisions_for_report(project_id, brief_observation_ids(brief)),
            reviewer_authentication=(
                "authenticated_team" if team_auth is not None else "unverified_preview"
            ),
        )
        return attach_locator_status(
            brief,
            repository.report_locator_status(project_id, brief_source_locators(brief)),
        )

    @app.get("/api/projects/{project_id}/review-impacts", response_model=list[ReviewImpactView])
    def list_review_impacts(request: Request, project_id: str) -> list[ReviewImpactView]:
        require_project(request, project_id, "read")
        return [
            ReviewImpactView.model_validate(row)
            for row in repository.list_review_impacts(project_id)
        ]

    @app.post(
        "/api/projects/{project_id}/review-impacts/{impact_id}/resolve",
        response_model=ReviewImpactView,
    )
    def resolve_review_impact(
        request: Request, project_id: str, impact_id: str, body: ReviewImpactResolution
    ) -> ReviewImpactView:
        require_project(request, project_id, "review")
        session = current_session(request)
        reviewer = session.user_id if session is not None else body.reviewer
        if reviewer is None:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        row = repository.resolve_review_impact(
            project_id,
            impact_id,
            resolution=body.resolution,
            note=body.note,
            reviewer=reviewer,
            reviewer_verified=session is not None,
        )
        return ReviewImpactView.model_validate(row)

    @app.post("/api/observations/{observation_id}/confirm", response_model=ObservationView)
    def confirm_observation(
        request: Request, observation_id: str, body: ReviewRequest
    ) -> ObservationView:
        existing = repository.get_observation(observation_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="observation not found")
        require_project(request, existing.project_id, "review")
        session = current_session(request)
        reviewer = session.user_id if session is not None else body.reviewer
        if reviewer is None:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        observation = repository.confirm_observation(
            observation_id,
            reviewer=reviewer,
            expected_version=body.expected_version,
            reviewer_verified=session is not None,
        )
        return ObservationView.model_validate(observation)

    @app.post(
        "/api/projects/{project_id}/observations/{observation_id}/confirm",
        response_model=ObservationView,
    )
    def project_confirm_observation(
        request: Request, project_id: str, observation_id: str, body: ReviewRequest
    ) -> ObservationView:
        require_project(request, project_id, "review")
        existing = repository.get_observation(observation_id)
        if existing is None or existing.project_id != project_id:
            raise HTTPException(status_code=404, detail="observation not found")
        return confirm_observation(request, observation_id, body)

    @app.post("/api/observations/{observation_id}/reject", response_model=ObservationView)
    def reject_observation(
        request: Request, observation_id: str, body: ReviewRequest
    ) -> ObservationView:
        existing = repository.get_observation(observation_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="observation not found")
        require_project(request, existing.project_id, "review")
        session = current_session(request)
        reviewer = session.user_id if session is not None else body.reviewer
        if reviewer is None:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        observation = repository.reject_observation(
            observation_id,
            reviewer=reviewer,
            expected_version=body.expected_version,
            reviewer_verified=session is not None,
        )
        return ObservationView.model_validate(observation)

    @app.post(
        "/api/projects/{project_id}/observations/{observation_id}/reject",
        response_model=ObservationView,
    )
    def project_reject_observation(
        request: Request, project_id: str, observation_id: str, body: ReviewRequest
    ) -> ObservationView:
        require_project(request, project_id, "review")
        existing = repository.get_observation(observation_id)
        if existing is None or existing.project_id != project_id:
            raise HTTPException(status_code=404, detail="observation not found")
        return reject_observation(request, observation_id, body)

    @app.post(
        "/api/projects/{project_id}/observations/batch-review",
        response_model=list[ObservationView],
    )
    def batch_review_observations(
        request: Request, project_id: str, body: BatchReviewRequest
    ) -> list[ObservationView]:
        require_project(request, project_id, "review")
        user = current_session(request)
        reviewer = user.user_id if user is not None else body.reviewer
        if reviewer is None:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        reviewed = repository.review_observations_batch(
            project_id,
            tuple((item.observation_id, item.expected_version, item.action) for item in body.items),
            reviewer=reviewer,
            reviewer_verified=user is not None,
        )
        return [ObservationView.model_validate(row) for row in reviewed]

    @app.get("/api/observations/{observation_id}/reviews", response_model=list[ReviewView])
    def review_history(request: Request, observation_id: str) -> list[ReviewView]:
        existing = repository.get_observation(observation_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="observation not found")
        require_project(request, existing.project_id, "read")
        return [ReviewView.model_validate(row) for row in repository.review_history(observation_id)]

    @app.get("/api/projects/{project_id}/summary", response_model=SummaryView)
    def get_summary(request: Request, project_id: str) -> SummaryView:
        require_project(request, project_id, "read")
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return SummaryView(
            project=ProjectView.model_validate(project),
            source_count=len(repository.list_sources(project_id)),
            candidate_count=len(repository.list_observations(project_id, status="candidate")),
            confirmed_count=len(repository.list_observations(project_id, status="confirmed")),
        )

    @app.post(
        "/api/projects/{project_id}/tasks",
        response_model=TaskView,
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(request: Request, project_id: str, body: TaskCreate) -> TaskView:
        require_project(request, project_id, "propose")
        task = task_service.run_coverage(
            project_id, source_ids=body.source_ids, idempotency_key=body.idempotency_key
        )
        return TaskView.model_validate(task)

    @app.get("/api/projects/{project_id}/tasks/{task_id}", response_model=TaskView)
    def get_task(request: Request, project_id: str, task_id: str) -> TaskView:
        require_project(request, project_id, "read")
        task = repository.get_execution_task(project_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskView.model_validate(task)

    @app.get("/api/projects/{project_id}/tasks", response_model=list[TaskView])
    def list_tasks(request: Request, project_id: str) -> list[TaskView]:
        require_project(request, project_id, "read")
        return [
            TaskView.model_validate(task) for task in repository.list_execution_tasks(project_id)
        ]

    @app.post(
        "/api/projects/{project_id}/candidate-proposals",
        response_model=ObservationView,
        status_code=status.HTTP_201_CREATED,
    )
    def propose_candidate(
        request: Request, project_id: str, body: CandidateProposalCreate
    ) -> ObservationView:
        require_project(request, project_id, "propose")
        session = current_session(request)
        observation = task_service.propose_candidate(
            project_id,
            **body.model_dump(),
            submission_channel="team_web" if session is not None else "http",
            actor_id=session.user_id if session is not None else None,
        )
        return ObservationView.model_validate(observation)

    @app.get("/api/projects/{project_id}/audit", response_model=list[AuditEventView])
    def list_audit_events(request: Request, project_id: str) -> list[AuditEventView]:
        require_project(request, project_id, "read")
        return [
            AuditEventView.model_validate(event)
            for event in repository.list_audit_events(project_id)
        ]

    def require_project_plan(project_id: str, plan_id: str):
        plan = repository.get_monitoring_plan(plan_id)
        if plan is None or plan.project_id != project_id:
            raise HTTPException(status_code=404, detail="monitoring plan not found")
        return plan

    @app.get('/api/projects/{project_id}/monitoring/external')
    def read_external_focus(request: Request, project_id: str):
        from project_watch.external_focus import external_reading

        if team_auth is not None:
            raise HTTPException(status_code=404, detail='Local feature only')
        require_project(request, project_id, 'read')
        return external_reading(repository, project_id)

    @app.get('/api/projects/{project_id}/monitoring/focus')
    def preview_external_focus(request: Request, project_id: str, category: str, term: str,
                               provider: Literal['gdelt', 'brave', 'bing'] = 'gdelt'):
        from project_watch.external_focus import public_query

        if team_auth is not None:
            raise HTTPException(status_code=404, detail='Local feature only')
        require_project(request, project_id, 'read')
        query = public_query(category, term, provider)
        query['eligible'] = provider in {'gdelt', 'bing'} or (
            bool(brave_api_key) and brave_monthly_limit > repository.brave_news_usage())
        return query

    @app.post('/api/projects/{project_id}/monitoring/focus', status_code=202,
              response_model=PublicSearchStarted)
    def start_external_focus(request: Request, project_id: str, body: ExternalFocusStart):
        from project_watch.external_focus import CONSENT_VERSION, ExternalFocusStore

        if team_auth is not None:
            raise HTTPException(status_code=404, detail='Local feature only')
        require_project(request, project_id, 'manage')
        if body.consent_version != CONSENT_VERSION:
            raise HTTPException(status_code=409, detail='请重新查看公开查询范围后确认。')
        if body.provider == 'brave' and (not brave_api_key or
                brave_monthly_limit <= repository.brave_news_usage()):
            raise HTTPException(status_code=409, detail='请先配置 Brave News 密钥及可用额度。')
        plan = ExternalFocusStore(repository).create(
            project_id, category=body.category, term=body.term, provider=body.provider)
        if not plan.enabled:
            plan = repository.set_monitoring_plan_enabled(plan.id, True)
        job = repository.request_monitoring_run(plan.id, idempotency_key='external-focus:first-run:v1')
        return PublicSearchStarted(plan=MonitoringPlanView.model_validate(plan),
                                   job=MonitoringJobView.model_validate(job))

    @app.get(
        "/api/projects/{project_id}/monitoring/public-search",
        response_model=PublicSearchPreview,
    )
    def preview_public_search(
        request: Request, project_id: str, provider: Literal["gdelt", "brave"] = "gdelt"
    ) -> PublicSearchPreview:
        require_project(request, project_id, "read")
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            target = (
                brave_news_target(project.name)
                if provider == "brave"
                else gdelt_rss_target(project.name)
            )
        except ValueError:
            target = None
        return PublicSearchPreview(
            provider=provider,
            project_name=project.name,
            target=target,
            disclosure=BRAVE_DISCLOSURE if provider == "brave" else DISCLOSURE,
            eligible=target is not None
            and (
                provider == "gdelt"
                or (bool(brave_api_key) and brave_monthly_limit > repository.brave_news_usage())
            ),
            monthly_limit=brave_monthly_limit if provider == "brave" else None,
            monthly_used=repository.brave_news_usage() if provider == "brave" else None,
        )

    @app.post(
        "/api/projects/{project_id}/monitoring/public-search",
        response_model=PublicSearchStarted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_public_search(
        request: Request, project_id: str, body: PublicSearchStart
    ) -> PublicSearchStarted:
        require_project(request, project_id, "manage")
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if body.provider == "brave":
            if not brave_api_key or brave_monthly_limit <= repository.brave_news_usage():
                raise HTTPException(
                    status_code=409, detail="Brave News key or monthly budget unavailable"
                )
            target = brave_news_target(project.name)
            kind = "brave_news"
            initial_run_key = "public-search:brave:first-run:v1"
        else:
            target = gdelt_rss_target(project.name)
            kind = "rss"
            initial_run_key = INITIAL_RUN_KEY
        plan = repository.create_monitoring_plan(
            project_id, kind=kind, target=target, cadence_minutes=CADENCE_MINUTES
        )
        if not plan.enabled:
            plan = repository.set_monitoring_plan_enabled(plan.id, True)
        job = repository.request_monitoring_run(plan.id, idempotency_key=initial_run_key)
        return PublicSearchStarted(
            plan=MonitoringPlanView.model_validate(plan),
            job=MonitoringJobView.model_validate(job),
        )

    @app.post(
        "/api/projects/{project_id}/monitoring/plans",
        response_model=MonitoringPlanView,
        status_code=status.HTTP_201_CREATED,
    )
    def create_monitoring_plan(
        request: Request, project_id: str, body: MonitoringPlanCreate
    ) -> MonitoringPlanView:
        require_project(request, project_id, "manage")
        plan = repository.create_monitoring_plan(project_id, **body.model_dump())
        return MonitoringPlanView.model_validate(plan)

    @app.get("/api/projects/{project_id}/monitoring/plans", response_model=list[MonitoringPlanView])
    def list_monitoring_plans(request: Request, project_id: str) -> list[MonitoringPlanView]:
        require_project(request, project_id, "read")
        return [
            MonitoringPlanView.model_validate(plan)
            for plan in repository.list_monitoring_plans(project_id)
        ]

    @app.patch(
        "/api/projects/{project_id}/monitoring/plans/{plan_id}",
        response_model=MonitoringPlanView,
    )
    def patch_monitoring_plan(
        request: Request, project_id: str, plan_id: str, body: MonitoringPlanPatch
    ) -> MonitoringPlanView:
        require_project(request, project_id, "manage")
        require_project_plan(project_id, plan_id)
        return MonitoringPlanView.model_validate(
            repository.set_monitoring_plan_enabled(plan_id, body.enabled)
        )

    @app.post(
        "/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
        response_model=MonitoringJobView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def request_monitoring_run(
        request: Request, project_id: str, plan_id: str, body: MonitoringRunRequest
    ) -> MonitoringJobView:
        require_project(request, project_id, "manage")
        require_project_plan(project_id, plan_id)
        return MonitoringJobView.model_validate(
            repository.request_monitoring_run(plan_id, idempotency_key=body.idempotency_key)
        )

    @app.get(
        "/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
        response_model=list[SourceRunView],
    )
    def list_monitoring_runs(
        request: Request, project_id: str, plan_id: str
    ) -> list[SourceRunView]:
        require_project(request, project_id, "read")
        require_project_plan(project_id, plan_id)
        return [SourceRunView.model_validate(run) for run in repository.list_source_runs(plan_id)]

    @app.get("/api/projects/{project_id}/monitoring/jobs", response_model=list[MonitoringJobView])
    def list_monitoring_jobs(request: Request, project_id: str) -> list[MonitoringJobView]:
        require_project(request, project_id, "read")
        return [
            MonitoringJobView.model_validate(job)
            for job in repository.list_monitoring_jobs(project_id)
        ]

    @app.post(
        "/api/projects/{project_id}/monitoring/jobs/{job_id}/retry",
        response_model=MonitoringJobView,
    )
    def retry_monitoring_job(request: Request, project_id: str, job_id: str) -> MonitoringJobView:
        require_project(request, project_id, "manage")
        job = repository.get_monitoring_job(job_id)
        if job is None or job.project_id != project_id:
            raise HTTPException(status_code=404, detail="monitoring job not found")
        return MonitoringJobView.model_validate(repository.retry_failed_monitoring_job(job_id))

    @app.get("/api/projects/{project_id}/monitoring/signals", response_model=list[SignalView])
    def list_monitoring_signals(request: Request, project_id: str) -> list[SignalView]:
        require_project(request, project_id, "read")
        return [
            SignalView.model_validate(signal)
            for signal in deduplicate_project_signals(repository.list_signals(project_id))
        ]

    return app


def create_local_app() -> FastAPI:
    """Uvicorn factory; refuses an implicit path or accidental production connection."""

    configured = os.environ.get("PROJECT_WATCH_DB_PATH")
    if not configured:
        raise RuntimeError("PROJECT_WATCH_DB_PATH must name an explicit local SQLite file")
    from project_watch.dsh_executor import configured_local_model
    from project_watch.feeds import parse_fixed_feed_targets

    return create_app(
        Path(configured),
        start_worker=True,
        gdelt_proxy_url=os.environ.get("PROJECT_WATCH_GDELT_PROXY_URL"),
        fixed_feed_proxy_url=os.environ.get("PROJECT_WATCH_FIXED_FEED_PROXY_URL"),
        fixed_feed_targets=parse_fixed_feed_targets(os.environ.get("PROJECT_WATCH_FIXED_FEED_TARGETS")),
        brave_api_key=os.environ.get("PROJECT_WATCH_BRAVE_API_KEY"),
        brave_monthly_limit=int(os.environ.get("PROJECT_WATCH_BRAVE_MONTHLY_LIMIT", "0")),
        brave_proxy_url=os.environ.get("PROJECT_WATCH_BRAVE_PROXY_URL"),
        parser_image=os.environ.get("PROJECT_WATCH_PARSER_IMAGE"),
        parser_docker_host=os.environ.get("PROJECT_WATCH_PARSER_DOCKER_HOST"),
        proposal_model=configured_local_model(),
    )


def create_private_local_app() -> FastAPI:
    """Single-user private pilot; missing isolation must never fall back to preview."""
    if not all(os.environ.get(key) for key in (
        "PROJECT_WATCH_PARSER_IMAGE", "PROJECT_WATCH_PARSER_DOCKER_HOST",
    )):
        raise RuntimeError("private local intake requires a pinned parser image and Docker host")
    return create_local_app()


def create_team_app() -> FastAPI:
    """Uvicorn factory for explicit HTTPS team-alpha deployments only."""

    from project_watch.team_auth import TeamAuthStore

    configured = os.environ.get("PROJECT_WATCH_DB_PATH")
    origin = os.environ.get("PROJECT_WATCH_TEAM_ORIGIN")
    if not configured or not origin:
        raise RuntimeError("team mode requires explicit database path and HTTPS origin")
    gdelt_proxy_url = os.environ.get("PROJECT_WATCH_GDELT_PROXY_URL")
    if gdelt_proxy_url is not None:
        parse_loopback_proxy(gdelt_proxy_url)
    auth = TeamAuthStore(Path(configured))
    if not auth.is_bootstrapped():
        raise RuntimeError("bootstrap a team administrator before starting team mode")
    return create_app(
        Path(configured),
        start_worker=True,
        team_auth=auth,
        team_origin=origin,
        gdelt_proxy_url=gdelt_proxy_url,
        brave_api_key=os.environ.get("PROJECT_WATCH_BRAVE_API_KEY"),
        brave_monthly_limit=int(os.environ.get("PROJECT_WATCH_BRAVE_MONTHLY_LIMIT", "0")),
        brave_proxy_url=os.environ.get("PROJECT_WATCH_BRAVE_PROXY_URL"),
    )
