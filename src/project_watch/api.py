"""Local development API over the independent project-watch domain service."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

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
)
from project_watch.report import (
    attach_locator_status,
    attach_review_proof,
    brief_observation_ids,
    brief_source_locators,
    build_current_brief,
)
from project_watch.repository import (
    IntakeConflictError,
    NotFoundError,
    ProjectRepository,
    ReviewConflictError,
)
from project_watch.request_guard import protect_request
from project_watch.scheduler import MonitoringScheduler
from project_watch.signal_projection import deduplicate_project_signals
from project_watch.source_comparison import compare_fragments
from project_watch.static_files import NoStoreStaticFiles
from project_watch.tasks import EvidenceConflictError, TaskService
from project_watch.watch_items import watch_item_snapshot


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
    parser_image: str | None = None,
    parser_docker_host: str | None = None,
    proposal_model=None,
    isolated_intake: bool = False,
) -> FastAPI:
    """Create the local app."""


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
    # Personal/local intake accepts whole research documents. Container
    # admission quotas remain explicit at the untrusted upload boundary.
    from project_watch.file_validation import DEFAULT_MAX_BYTES

    blob_store = BlobStore(
        Path(db_path).parent / "objects",
        max_bytes=DEFAULT_MAX_BYTES if parser_image else None,
    )
    worker = IntakeWorker(
        repository, blob_store, isolated=isolated_intake,
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
    source_router = SourceFallbackStore(repository)
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
        title="Project Watch — development preview",
        version="0.1.0a0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.intake_worker = worker
    app.state.monitoring_scheduler = monitoring_scheduler
    from project_watch.manual_model_api import attach_manual_model_routes
    from project_watch.model_settings import ModelSettings, attach_model_settings
    from project_watch.reading_authorization_api import attach_reading_authorization
    from project_watch.workspace_api import attach_workspace_routes

    model_settings = ModelSettings(proposal_model)
    attach_model_settings(app, model_settings)
    attach_workspace_routes(app, repository)
    attach_manual_model_routes(app, repository, model_settings)
    attach_reading_authorization(app, repository, model_settings)
    from project_watch.reading_worker import ReadingWorker
    reading_worker = ReadingWorker(repository, app.state.reading_jobs, model_settings)
    app.state.reading_worker = reading_worker
    app.state.reading_worker_enabled = start_worker and reading_worker is not None
    from project_watch.monitor_autopilot import MonitorAutopilot
    from project_watch.monitor_autopilot_api import attach_monitor_autopilot
    monitor_autopilot = MonitorAutopilot(repository, model_settings)
    app.state.monitor_autopilot = monitor_autopilot
    if reading_worker is not None:
        reading_worker.monitor_autopilot = monitor_autopilot
        monitor_autopilot.stopping = reading_worker._stop.is_set
    attach_monitor_autopilot(app, repository, model_settings, monitor_autopilot)
    from project_watch.external_analysis_jobs import ExternalAnalysisStore, ExternalAnalysisWorker
    external_store = ExternalAnalysisStore(repository)
    external_worker = ExternalAnalysisWorker(external_store, model_settings)
    app.state.external_analysis_store = external_store
    app.state.external_analysis_worker = external_worker
    if reading_worker is not None:
        reading_worker.external_worker = external_worker
    from project_watch.external_analysis_api import attach_external_analysis
    attach_external_analysis(app, repository, model_settings, store=external_store, worker=external_worker)
    from project_watch.monitor_activation_api import attach_monitor_activation
    attach_source_fallback(app, repository, source_router)
    attach_monitor_activation(app, repository, brave_available=lambda: bool(brave_api_key))
    static_root = Path(__file__).parent / "static"
    app.mount("/static", NoStoreStaticFiles(directory=static_root), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        return await protect_request(request, call_next)

    @app.get("/", include_in_schema=False)
    def web_preview() -> FileResponse:
        return FileResponse(static_root / "index.html", media_type="text/html")

    @app.get("/api/runtime")
    def runtime_info() -> dict:
        return {
            "mode": "local",
            "parser": "container" if parser_image else "subprocess" if (
                isolated_intake) else "in_process",
            "private_local_intake": bool(parser_image),
            "authenticated_review": False,
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

    @app.post("/api/projects", response_model=ProjectView, status_code=status.HTTP_201_CREATED)
    def create_project(request: Request, body: ProjectCreate) -> ProjectView:
        return ProjectView.model_validate(
            repository.create_project(body.name, actor_id=None)
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
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return ProjectView.model_validate(project)

    @app.get("/api/projects/{project_id}/identity", response_model=ProjectIdentityState)
    def project_identity(
        request: Request, response: Response, project_id: str
    ) -> ProjectIdentityState:
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        response.headers["Cache-Control"] = "no-store"

        return ProjectIdentityState(
            status=project.identity_status,
            history=[IdentityReviewView.model_validate(row) for row in repository.list_identity_reviews(project_id)],
        )


    @app.get("/api/projects/{project_id}/capabilities", response_model=ProjectCapabilitiesView)
    def project_capabilities(request: Request, project_id: str) -> ProjectCapabilitiesView:
        if repository.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return ProjectCapabilitiesView(read=True, propose=True, review=True, manage=True)

    @app.post(
        "/api/projects/{project_id}/sources",
        response_model=SourceView,
        status_code=status.HTTP_201_CREATED,
    )
    def add_source(request: Request, project_id: str, body: SourceCreate) -> SourceView:
        source = repository.add_source(project_id, **body.model_dump())
        return SourceView.model_validate(source)

    @app.get("/api/projects/{project_id}/sources", response_model=list[SourceView])
    def list_sources(request: Request, project_id: str) -> list[SourceView]:
        return [SourceView.model_validate(row) for row in repository.list_sources(project_id)]

    @app.post(
        "/api/projects/{project_id}/files",
        response_model=FileUploadView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def upload_file(
        request: Request, project_id: str, file: UploadFile = File(...)
    ) -> FileUploadView:
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
        return [IntakeView.model_validate(row) for row in repository.list_intake(project_id)]

    @app.get("/api/sources/{source_id}/fragments", response_model=list[FragmentView])
    def list_fragments(request: Request, source_id: str) -> list[FragmentView]:
        source = repository.get_source(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        return [FragmentView.model_validate(row) for row in repository.list_fragments(source_id)]

    @app.get(
        "/api/projects/{project_id}/sources/{source_id}/fragments",
        response_model=list[FragmentView],
    )
    def project_fragments(request: Request, project_id: str, source_id: str) -> list[FragmentView]:
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
        return IntakeView.model_validate(repository.retry_failed_intake(job_id))

    @app.post("/api/projects/{project_id}/intake/{job_id}/retry", response_model=IntakeView)
    def project_retry_intake(request: Request, project_id: str, job_id: str) -> IntakeView:
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
        observation = repository.add_observation(
            project_id,
            **body.model_dump(),
            submission_channel="preview_api",
            actor_id=None,
        )
        return ObservationView.model_validate(observation)

    @app.get("/api/projects/{project_id}/observations", response_model=list[ObservationView])
    def list_observations(
        request: Request,
        project_id: str,
        status_filter: Literal["candidate", "confirmed", "rejected"] | None = None,
    ) -> list[ObservationView]:
        return [
            ObservationView.model_validate(row)
            for row in repository.list_observations(project_id, status=status_filter)
        ]

    @app.get("/api/projects/{project_id}/watch-items", response_model=list[WatchItemView])
    def list_watch_items(request: Request, project_id: str) -> list[WatchItemView]:
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
        item = repository.propose_watch_item(
            project_id, **body.model_dump(), actor_id=None
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
        reviewer = body.reviewer
        if not reviewer:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        item = repository.review_watch_item(
            project_id,
            item_id,
            action=body.action,
            expected_version=body.expected_version,
            reviewer=reviewer,
            reviewer_verified=False,
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
        return watch_change_views(repository.list_watch_item_changes(project_id))

    @app.post(
        "/api/projects/{project_id}/watch-items/{item_id}/changes",
        response_model=WatchItemChangeView,
        status_code=201,
    )
    def propose_watch_item_change(
        request: Request, project_id: str, item_id: str, body: WatchItemChangeCreate
    ) -> WatchItemChangeView:
        change = repository.propose_watch_item_change(
            project_id,
            item_id,
            **body.model_dump(),
            actor_id=None,
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
        reviewer = body.reviewer
        if not reviewer:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        item = repository.review_watch_item_change(
            project_id,
            item_id,
            change_id,
            action=body.action,
            expected_version=body.expected_version,
            reviewer=reviewer,
            reviewer_verified=False,
            note=body.note,
        )
        return watch_item_views(
            [item], repository.list_observations(project_id), repository.list_sources(project_id)
        )[0]

    @app.get(
        "/api/projects/{project_id}/competitive-peers", response_model=list[CompetitivePeerView]
    )
    def list_competitive_peers(request: Request, project_id: str) -> list[CompetitivePeerView]:
        return [CompetitivePeerView.model_validate(row) for row in repository.list_competitive_peers(project_id)]

    @app.get("/api/projects/{project_id}/holding-snapshots", response_model=list[HoldingSnapshotView])
    def list_holding_snapshots(request: Request, project_id: str) -> list[HoldingSnapshotView]:
        return [HoldingSnapshotView.model_validate(row) for row in repository.list_holding_snapshots(project_id)]

    @app.post(
        "/api/projects/{project_id}/holding-snapshots",
        response_model=HoldingSnapshotView,
        status_code=201,
    )
    def propose_holding_snapshot(
        request: Request, project_id: str, body: HoldingSnapshotCreate
    ) -> HoldingSnapshotView:
        return HoldingSnapshotView.model_validate(
            repository.propose_holding_snapshot(
                project_id, **body.model_dump(), actor_id=None,
            )
        )

    @app.post(
        "/api/projects/{project_id}/holding-snapshots/{snapshot_id}/review",
        response_model=HoldingSnapshotView,
    )
    def review_holding_snapshot(
        request: Request, project_id: str, snapshot_id: str, body: HoldingSnapshotReview
    ) -> HoldingSnapshotView:
        reviewer = body.reviewer
        if not reviewer:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        return HoldingSnapshotView.model_validate(
            repository.review_holding_snapshot(
                project_id, snapshot_id, action=body.action,
                expected_version=body.expected_version, reviewer=reviewer,
                reviewer_verified=False, note=body.note,
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
        return CompetitivePeerView.model_validate(
            repository.propose_competitive_peer(
                project_id,
                name=body.name,
                linked_project_id=body.linked_project_id,
                actor_id=None,
            )
        )

    @app.post(
        "/api/projects/{project_id}/competitive-peers/{peer_id}/review",
        response_model=CompetitivePeerView,
    )
    def review_competitive_peer(
        request: Request, project_id: str, peer_id: str, body: CompetitivePeerReview
    ) -> CompetitivePeerView:
        reviewer = body.reviewer
        if not reviewer:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        return CompetitivePeerView.model_validate(
            repository.review_competitive_peer(
                project_id,
                peer_id,
                action=body.action,
                expected_version=body.expected_version,
                reviewer=reviewer,
                reviewer_verified=False,
                note=body.note,
            )
        )

    @app.get("/api/projects/{project_id}/competitive-position")
    def competitive_position(request: Request, project_id: str) -> dict[str, object]:
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
            "peers": [CompetitivePeerView.model_validate(peer).model_dump(mode="json") for peer in peers],
            "peer_access": peer_access,
            "comparison": compare_competitive_metrics(
                project_id, [peer.id for peer in approved], metrics
            ),
        }

    @app.get("/api/projects/{project_id}/metric-series", response_model=MetricSeriesResponse)
    def metric_series(request: Request, project_id: str) -> MetricSeriesResponse:
        return MetricSeriesResponse.model_validate(
            build_metric_series(
                repository.list_observations(project_id, status="confirmed"),
                repository.list_sources(project_id),
            )
        )

    @app.get("/api/projects/{project_id}/financial-screen")
    def financial_screen(request: Request, project_id: str) -> dict[str, object]:
        return screen_confirmed_financials(
            repository.list_observations(project_id), repository.list_sources(project_id)
        )

    @app.get("/api/projects/{project_id}/monitoring/domains")
    def monitoring_domains(request: Request, project_id: str) -> dict[str, object]:
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
            reviewer_authentication="unverified_preview",
        )
        return attach_locator_status(
            brief,
            repository.report_locator_status(project_id, brief_source_locators(brief)),
        )

    @app.get("/api/projects/{project_id}/review-impacts", response_model=list[ReviewImpactView])
    def list_review_impacts(request: Request, project_id: str) -> list[ReviewImpactView]:
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
        reviewer = body.reviewer
        if reviewer is None:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        row = repository.resolve_review_impact(
            project_id,
            impact_id,
            resolution=body.resolution,
            note=body.note,
            reviewer=reviewer,
            reviewer_verified=False,
        )
        return ReviewImpactView.model_validate(row)

    @app.post("/api/observations/{observation_id}/confirm", response_model=ObservationView)
    def confirm_observation(
        request: Request, observation_id: str, body: ReviewRequest
    ) -> ObservationView:
        existing = repository.get_observation(observation_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="observation not found")
        reviewer = body.reviewer
        if reviewer is None:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        observation = repository.confirm_observation(
            observation_id,
            reviewer=reviewer,
            expected_version=body.expected_version,
            reviewer_verified=False,
        )
        return ObservationView.model_validate(observation)

    @app.post(
        "/api/projects/{project_id}/observations/{observation_id}/confirm",
        response_model=ObservationView,
    )
    def project_confirm_observation(
        request: Request, project_id: str, observation_id: str, body: ReviewRequest
    ) -> ObservationView:
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
        reviewer = body.reviewer
        if reviewer is None:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        observation = repository.reject_observation(
            observation_id,
            reviewer=reviewer,
            expected_version=body.expected_version,
            reviewer_verified=False,
        )
        return ObservationView.model_validate(observation)

    @app.post(
        "/api/projects/{project_id}/observations/{observation_id}/reject",
        response_model=ObservationView,
    )
    def project_reject_observation(
        request: Request, project_id: str, observation_id: str, body: ReviewRequest
    ) -> ObservationView:
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
        reviewer = body.reviewer
        if reviewer is None:
            raise HTTPException(status_code=422, detail="reviewer required in preview mode")
        reviewed = repository.review_observations_batch(
            project_id,
            tuple((item.observation_id, item.expected_version, item.action) for item in body.items),
            reviewer=reviewer,
            reviewer_verified=False,
        )
        return [ObservationView.model_validate(row) for row in reviewed]

    @app.get("/api/observations/{observation_id}/reviews", response_model=list[ReviewView])
    def review_history(request: Request, observation_id: str) -> list[ReviewView]:
        existing = repository.get_observation(observation_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="observation not found")
        return [ReviewView.model_validate(row) for row in repository.review_history(observation_id)]

    @app.get("/api/projects/{project_id}/summary", response_model=SummaryView)
    def get_summary(request: Request, project_id: str) -> SummaryView:
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
        task = task_service.run_coverage(
            project_id, source_ids=body.source_ids, idempotency_key=body.idempotency_key
        )
        return TaskView.model_validate(task)

    @app.get("/api/projects/{project_id}/tasks/{task_id}", response_model=TaskView)
    def get_task(request: Request, project_id: str, task_id: str) -> TaskView:
        task = repository.get_execution_task(project_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskView.model_validate(task)

    @app.get("/api/projects/{project_id}/tasks", response_model=list[TaskView])
    def list_tasks(request: Request, project_id: str) -> list[TaskView]:
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
        observation = task_service.propose_candidate(
            project_id,
            **body.model_dump(),
            submission_channel="http",
            actor_id=None,
        )
        return ObservationView.model_validate(observation)

    @app.get("/api/projects/{project_id}/audit", response_model=list[AuditEventView])
    def list_audit_events(request: Request, project_id: str) -> list[AuditEventView]:
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

        return external_reading(repository, project_id)

    @app.get('/api/projects/{project_id}/monitoring/focus')
    def preview_external_focus(request: Request, project_id: str, category: str, term: str,
                               provider: Literal['gdelt', 'brave', 'bing'] = 'gdelt'):
        from project_watch.external_focus import public_query

        query = public_query(category, term, provider)
        query['eligible'] = provider in {'gdelt', 'bing'} or (
            bool(brave_api_key))
        return query

    @app.post('/api/projects/{project_id}/monitoring/focus', status_code=202,
              response_model=PublicSearchStarted)
    def start_external_focus(request: Request, project_id: str, body: ExternalFocusStart):
        from project_watch.external_focus import CONSENT_VERSION, ExternalFocusStore

        if body.consent_version != CONSENT_VERSION:
            raise HTTPException(status_code=409, detail='请重新查看公开查询范围后确认。')
        if body.provider == 'brave' and (not brave_api_key):
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
                or (bool(brave_api_key))
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
        project = repository.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if body.provider == "brave":
            if not brave_api_key:
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
        plan = repository.create_monitoring_plan(project_id, **body.model_dump())
        return MonitoringPlanView.model_validate(plan)

    @app.get("/api/projects/{project_id}/monitoring/plans", response_model=list[MonitoringPlanView])
    def list_monitoring_plans(request: Request, project_id: str) -> list[MonitoringPlanView]:
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
        require_project_plan(project_id, plan_id)
        return [SourceRunView.model_validate(run) for run in repository.list_source_runs(plan_id)]

    @app.get("/api/projects/{project_id}/monitoring/jobs", response_model=list[MonitoringJobView])
    def list_monitoring_jobs(request: Request, project_id: str) -> list[MonitoringJobView]:
        return [
            MonitoringJobView.model_validate(job)
            for job in repository.list_monitoring_jobs(project_id)
        ]

    @app.post(
        "/api/projects/{project_id}/monitoring/jobs/{job_id}/retry",
        response_model=MonitoringJobView,
    )
    def retry_monitoring_job(request: Request, project_id: str, job_id: str) -> MonitoringJobView:
        job = repository.get_monitoring_job(job_id)
        if job is None or job.project_id != project_id:
            raise HTTPException(status_code=404, detail="monitoring job not found")
        return MonitoringJobView.model_validate(repository.retry_failed_monitoring_job(job_id))

    @app.get("/api/projects/{project_id}/monitoring/signals", response_model=list[SignalView])
    def list_monitoring_signals(request: Request, project_id: str) -> list[SignalView]:
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
    from project_watch.feeds import parse_fixed_feed_targets
    from project_watch.model_executor import configured_local_model

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

