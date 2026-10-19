"""Explicit PostgreSQL team API slice; no implicit migration or public deployment.

The local SQLite preview keeps its own factory. This app reuses static browser
assets but exposes only routes backed by the PostgreSQL repository. MCP remains
an independent, project-bound transport.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from psycopg.errors import UniqueViolation

from project_watch.analysis.competitive_comparison import (
    compare_competitive_metrics,
    project_competitive_metrics,
)
from project_watch.analysis.financial_drafts import financial_review_drafts
from project_watch.analysis.financial_mapping import map_financial_fragments
from project_watch.analysis.financial_projection import screen_confirmed_financials
from project_watch.analysis.monitoring_projection import build_monitoring_projection
from project_watch.analysis.operating_mapping import map_operating_fragments
from project_watch.api import (
    AuditEventView,
    BatchReviewRequest,
    CandidateProposalCreate,
    CompetitivePeerCreate,
    CompetitivePeerReview,
    CompetitivePeerView,
    ExecutorTokenIssue,
    ExecutorTokenIssued,
    ExecutorTokenView,
    FileUploadView,
    FinancialMappingIssueView,
    FinancialMappingView,
    FinancialReviewDraftView,
    FragmentView,
    HoldingSnapshotCreate,
    HoldingSnapshotReview,
    HoldingSnapshotView,
    IdentityReopenCreate,
    IdentityReviewView,
    IdentityVerificationCreate,
    IntakeView,
    MetricSeriesResponse,
    MonitoringJobView,
    MonitoringPlanCreate,
    MonitoringPlanPatch,
    MonitoringPlanView,
    MonitoringRunRequest,
    ObservationCreate,
    ObservationView,
    OperatingMappingIssueView,
    OperatingMappingView,
    ProjectCapabilitiesView,
    ProjectCreate,
    ProjectIdentityState,
    ProjectView,
    PublicSearchPreview,
    PublicSearchStart,
    PublicSearchStarted,
    ReviewImpactResolution,
    ReviewImpactView,
    ReviewRequest,
    ReviewView,
    SignalView,
    SourceComparisonView,
    SourceCreate,
    SourceRunView,
    SourceView,
    SummaryView,
    TaskCreate,
    TaskView,
    TeamLogin,
    TeamMemberCreate,
    TeamMemberView,
    TeamPasswordReset,
    TeamRolePatch,
    TeamSecurityEventView,
    TeamSessionView,
    TeamUserCreate,
    TeamUserView,
    WatchItemChangeCreate,
    WatchItemChangeReview,
    WatchItemChangeView,
    WatchItemCreate,
    WatchItemReview,
    WatchItemView,
    watch_change_views,
    watch_item_views,
)
from project_watch.blob_store import BlobStore
from project_watch.brave_news import (
    DISCLOSURE as BRAVE_DISCLOSURE,
)
from project_watch.brave_news import (
    BraveNewsAdapter,
    brave_news_target,
    validate_brave_news_configuration,
)
from project_watch.feeds import configured_public_feed_adapter
from project_watch.metric_series import build_metric_series
from project_watch.monitoring import MonitoringAdapter
from project_watch.parser_container import (
    validate_parser_docker_host,
    validate_parser_image,
    validate_parser_runtime,
)
from project_watch.postgres_core import PostgresCoreRepository
from project_watch.postgres_tasks import PostgresTaskService
from project_watch.postgres_team_auth import PostgresTeamAuthStore
from project_watch.postgres_workers import PostgresIntakeWorker, PostgresMonitoringScheduler
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
    IdentityConflictError,
    IntakeConflictError,
    NotFoundError,
    ReviewConflictError,
)
from project_watch.signal_projection import deduplicate_project_signals
from project_watch.source_comparison import compare_fragments
from project_watch.static_files import NoStoreStaticFiles
from project_watch.tasks import EvidenceConflictError
from project_watch.team_auth import AuthenticationError, AuthorizationError, TeamSession
from project_watch.team_http import (
    SESSION_COOKIE,
    LoginAttemptLimiter,
    protect_request,
    validate_team_origin,
)


def create_postgres_team_app(
    dsn: str,
    *,
    object_root: Path | str,
    team_origin: str,
    start_worker: bool = False,
    parser_image: str | None = None,
    parser_docker_host: str | None = None,
    monitoring_adapters: dict[str, MonitoringAdapter] | None = None,
    gdelt_proxy_url: str | None = None,
    brave_api_key: str | None = None,
    brave_monthly_limit: int = 0,
    brave_proxy_url: str | None = None,
) -> FastAPI:
    """Construct a protected API from an explicitly bootstrapped dedicated DB."""
    if start_worker and (parser_image is None or parser_docker_host is None):
        raise ValueError("team intake worker requires a pinned parser image and local Docker host")
    if (parser_image is None) != (parser_docker_host is None):
        raise ValueError("parser image and local Docker host must be configured together")
    if parser_image is not None and parser_docker_host is not None:
        validate_parser_image(parser_image)
        validate_parser_docker_host(parser_docker_host)
        if start_worker:
            validate_parser_runtime(parser_image, parser_docker_host)
    default_feed_adapter = configured_public_feed_adapter(gdelt_proxy_url)
    validate_brave_news_configuration(brave_api_key, brave_monthly_limit, brave_proxy_url)
    origin = validate_team_origin(team_origin)
    auth = PostgresTeamAuthStore(dsn)
    try:
        if not auth.is_bootstrapped():
            raise RuntimeError("bootstrap a team administrator before starting team mode")
        repo = PostgresCoreRepository(dsn)
    except Exception:
        auth.close()
        raise
    try:
        objects = BlobStore(object_root)
    except Exception:
        repo.close()
        auth.close()
        raise
    task_service = PostgresTaskService(repo)
    brave_adapter = BraveNewsAdapter(
        brave_api_key,
        monthly_limit=brave_monthly_limit,
        reserve=repo.reserve_brave_news_request,
        proxy_url=brave_proxy_url,
    )
    intake_worker = PostgresIntakeWorker(
        repo, objects, parser_image=parser_image, docker_host=parser_docker_host
    )
    monitoring_worker = PostgresMonitoringScheduler(
        repo,
        monitoring_adapters
        if monitoring_adapters is not None
        else {
            "rss": default_feed_adapter,
            "brave_news": brave_adapter,
        },
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            intake_worker.start()
            monitoring_worker.start()
        try:
            yield
        finally:
            if start_worker:
                monitoring_worker.stop()
                intake_worker.stop()
            repo.close()
            auth.close()

    app = FastAPI(
        title="Project Watch — PostgreSQL team API alpha",
        version="0.1.0a0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.intake_worker = intake_worker
    app.state.monitoring_scheduler = monitoring_worker
    app.state.login_limiter = LoginAttemptLimiter()
    static_root = Path(__file__).parent / "static"
    app.mount("/static", NoStoreStaticFiles(directory=static_root), name="static")

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        return await protect_request(request, call_next, team_auth=auth, team_origin=origin)

    @app.exception_handler(NotFoundError)
    async def not_found(_request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ReviewConflictError)
    async def review_conflict(_request, exc: ReviewConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(IntakeConflictError)
    async def intake_conflict(_request, exc: IntakeConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(EvidenceConflictError)
    async def evidence_conflict(_request, exc: EvidenceConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid_value(_request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    def session(request: Request) -> TeamSession:
        return request.state.team_session

    def require_project(request: Request, project_id: str, permission: str) -> None:
        try:
            auth.require(session(request).user_id, project_id, permission)
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

    def require_admin(request: Request) -> None:
        if not session(request).global_admin:
            raise HTTPException(status_code=403, detail="admin required")

    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(static_root / "index.html")

    @app.post("/api/auth/login", response_model=TeamSessionView)
    def login(body: TeamLogin, request: Request, response: Response) -> TeamSessionView:
        if not app.state.login_limiter.allow(
            request.client.host if request.client else None, body.username
        ):
            raise HTTPException(
                status_code=429,
                detail="too many login attempts",
                headers={"Retry-After": "60"},
            )
        try:
            token, csrf = auth.login(body.username, body.password)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail="invalid credentials") from exc
        verified = auth.session(token)
        assert verified is not None
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=8 * 3600,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store"
        return TeamSessionView(
            user_id=verified.user_id,
            username=verified.username,
            global_admin=verified.global_admin,
            csrf_token=csrf,
        )

    @app.get("/api/auth/session", response_model=TeamSessionView)
    def get_session(request: Request, response: Response) -> TeamSessionView:
        verified = session(request)
        response.headers["Cache-Control"] = "no-store"
        return TeamSessionView(
            user_id=verified.user_id,
            username=verified.username,
            global_admin=verified.global_admin,
            csrf_token=auth.csrf_for(request.state.team_token),
        )

    @app.post("/api/auth/logout", status_code=204, response_model=None)
    def logout(request: Request, response: Response) -> None:
        auth.logout(request.state.team_token)
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, samesite="strict")

    @app.post("/api/team/users", response_model=TeamUserView, status_code=201)
    def create_user(request: Request, body: TeamUserCreate) -> TeamUserView:
        require_admin(request)
        try:
            created = auth.create_user(
                body.username, body.password, actor_user_id=session(request).user_id
            )
        except UniqueViolation as exc:
            raise HTTPException(status_code=409, detail="username already exists") from exc
        return TeamUserView.model_validate(created)

    @app.post("/api/team/users/{user_id}/reset-password", status_code=204, response_model=None)
    def reset_password(
        request: Request, response: Response, user_id: str, body: TeamPasswordReset
    ) -> None:
        require_admin(request)
        try:
            auth.reset_password(user_id, body.password, actor_user_id=session(request).user_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="user not found") from exc
        response.headers["Cache-Control"] = "no-store"

    @app.get("/api/team/security-events", response_model=list[TeamSecurityEventView])
    def security_events(request: Request) -> list[TeamSecurityEventView]:
        require_admin(request)
        return [TeamSecurityEventView.model_validate(row) for row in auth.list_security_events()]

    @app.post(
        "/api/projects/{project_id}/executor-tokens",
        response_model=ExecutorTokenIssued,
        status_code=201,
    )
    def issue_executor_token(
        request: Request, response: Response, project_id: str, body: ExecutorTokenIssue
    ) -> ExecutorTokenIssued:
        require_project(request, project_id, "manage")
        try:
            secret, grant = auth.issue_executor_token(
                project_id,
                session(request).user_id,
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

    @app.get("/api/projects/{project_id}/executor-tokens", response_model=list[ExecutorTokenView])
    def list_executor_tokens(request: Request, project_id: str) -> list[ExecutorTokenView]:
        require_project(request, project_id, "manage")
        return [
            ExecutorTokenView.model_validate(row) for row in auth.list_executor_tokens(project_id)
        ]

    @app.delete(
        "/api/projects/{project_id}/executor-tokens/{token_id}",
        status_code=204,
        response_model=None,
    )
    def revoke_executor_token(request: Request, project_id: str, token_id: str) -> None:
        require_project(request, project_id, "manage")
        try:
            revoked = auth.revoke_executor_token(
                project_id, token_id, actor_user_id=session(request).user_id
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        if not revoked:
            raise HTTPException(status_code=404, detail="executor token not found")

    @app.put(
        "/api/projects/{project_id}/members/{user_id}",
        status_code=204,
        response_model=None,
    )
    def grant_member(request: Request, project_id: str, user_id: str, body: TeamRolePatch) -> None:
        require_project(request, project_id, "manage")
        try:
            auth.grant_project_role(
                project_id, user_id, body.role, actor_user_id=session(request).user_id
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc

    @app.get("/api/projects/{project_id}/members", response_model=list[TeamMemberView])
    def list_members(
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
            for member in auth.list_project_members(
                project_id, limit=limit, after_username=after_username
            )
        ]

    @app.post(
        "/api/projects/{project_id}/members",
        response_model=TeamMemberView,
        status_code=201,
    )
    def assign_member(
        request: Request, response: Response, project_id: str, body: TeamMemberCreate
    ) -> TeamMemberView:
        require_project(request, project_id, "manage")
        user = auth.find_active_user(body.username)
        if user is None:
            raise HTTPException(status_code=404, detail="active team account not found")
        try:
            auth.grant_project_role(
                project_id, user.id, body.role, actor_user_id=session(request).user_id
            )
        except (AuthorizationError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="active team account not found") from exc
        response.headers["Cache-Control"] = "no-store"
        return TeamMemberView(user_id=user.id, username=user.username, role=body.role, active=True)

    @app.delete(
        "/api/projects/{project_id}/members/{user_id}",
        status_code=204,
        response_model=None,
    )
    def revoke_member(request: Request, project_id: str, user_id: str) -> None:
        require_project(request, project_id, "manage")
        try:
            auth.revoke_project_role(project_id, user_id, actor_user_id=session(request).user_id)
        except AuthorizationError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc

    @app.post("/api/projects", response_model=ProjectView, status_code=201)
    def create_project(request: Request, body: ProjectCreate) -> ProjectView:
        require_admin(request)
        return ProjectView.model_validate(
            repo.create_project(body.name, verified_actor_id=session(request).user_id)
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
        if after_created_at is not None:
            try:
                parsed_cursor = datetime.fromisoformat(after_created_at.replace("Z", "+00:00"))
                if parsed_cursor.tzinfo is None:
                    raise ValueError("timezone required")
                normalized = parsed_cursor.astimezone(UTC).isoformat()
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid project cursor") from exc
            after = (normalized, after_id)
        else:
            after = None
        return [
            ProjectView.model_validate(row)
            for row in repo.list_visible_projects_page(
                session(request).user_id, after=after, limit=limit
            )
        ]

    @app.get("/api/projects/{project_id}", response_model=ProjectView)
    def get_project(request: Request, project_id: str) -> ProjectView:
        require_project(request, project_id, "read")
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return ProjectView.model_validate(project)

    @app.get("/api/projects/{project_id}/identity", response_model=ProjectIdentityState)
    def project_identity(
        request: Request, response: Response, project_id: str
    ) -> ProjectIdentityState:
        require_project(request, project_id, "read")
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        response.headers["Cache-Control"] = "no-store"

        def named_review(row):
            user = auth.get_user(row.actor_ref)
            return IdentityReviewView.model_validate(row).model_copy(
                update={"actor_name": user.username if user else None}
            )

        return ProjectIdentityState(
            status=project.identity_status,
            history=[named_review(row) for row in repo.list_identity_reviews(project_id)],
        )

    @app.post(
        "/api/projects/{project_id}/identity",
        response_model=IdentityReviewView,
        status_code=201,
    )
    def verify_project_identity(
        request: Request,
        response: Response,
        project_id: str,
        body: IdentityVerificationCreate,
    ) -> IdentityReviewView:
        require_project(request, project_id, "manage")
        try:
            review = repo.verify_project_identity(
                project_id,
                **body.model_dump(),
                actor_user_id=session(request).user_id,
            )
        except IdentityConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return IdentityReviewView.model_validate(review).model_copy(
            update={"actor_name": session(request).username}
        )

    @app.post(
        "/api/projects/{project_id}/identity/reopen",
        response_model=IdentityReviewView,
        status_code=201,
    )
    def reopen_project_identity(
        request: Request,
        response: Response,
        project_id: str,
        body: IdentityReopenCreate,
    ) -> IdentityReviewView:
        require_project(request, project_id, "manage")
        try:
            review = repo.reopen_project_identity(
                project_id,
                note=body.note,
                expected_version=body.expected_version,
                actor_user_id=session(request).user_id,
            )
        except IdentityConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return IdentityReviewView.model_validate(review).model_copy(
            update={"actor_name": session(request).username}
        )

    @app.get("/api/projects/{project_id}/capabilities", response_model=ProjectCapabilitiesView)
    def project_capabilities(request: Request, project_id: str) -> ProjectCapabilitiesView:
        require_project(request, project_id, "read")
        if repo.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        allowed = {"read": True}
        for permission in ("propose", "review", "manage"):
            try:
                auth.require(session(request).user_id, project_id, permission)
                allowed[permission] = True
            except AuthorizationError:
                allowed[permission] = False
        return ProjectCapabilitiesView(**allowed)

    @app.get("/api/projects/{project_id}/summary", response_model=SummaryView)
    def summary(request: Request, project_id: str) -> SummaryView:
        require_project(request, project_id, "read")
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return SummaryView(
            project=ProjectView.model_validate(project),
            source_count=len(repo.list_sources(project_id)),
            candidate_count=len(repo.list_observations(project_id, status="candidate")),
            confirmed_count=len(repo.list_observations(project_id, status="confirmed")),
        )

    @app.post("/api/projects/{project_id}/sources", response_model=SourceView, status_code=201)
    def add_source(request: Request, project_id: str, body: SourceCreate) -> SourceView:
        require_project(request, project_id, "propose")
        return SourceView.model_validate(repo.add_source(project_id, **body.model_dump()))

    @app.get("/api/projects/{project_id}/sources", response_model=list[SourceView])
    def sources(request: Request, project_id: str) -> list[SourceView]:
        require_project(request, project_id, "read")
        return [SourceView.model_validate(row) for row in repo.list_sources(project_id)]

    @app.post(
        "/api/projects/{project_id}/files",
        response_model=FileUploadView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def upload_file(
        request: Request, project_id: str, file: UploadFile = File(...)
    ) -> FileUploadView:
        require_project(request, project_id, "propose")
        if repo.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        stored = objects.put_stream(file.filename or "", file.file)
        source = repo.add_source(
            project_id,
            kind="document",
            reference=stored.display_name,
            content_sha256=stored.sha256,
        )
        job = repo.enqueue_intake(project_id, source.id)
        return FileUploadView(
            source=SourceView.model_validate(source), job=IntakeView.model_validate(job)
        )

    @app.get("/api/projects/{project_id}/intake", response_model=list[IntakeView])
    def intake(request: Request, project_id: str) -> list[IntakeView]:
        require_project(request, project_id, "read")
        return [IntakeView.model_validate(row) for row in repo.list_intake(project_id)]

    @app.post("/api/projects/{project_id}/intake/{job_id}/retry", response_model=IntakeView)
    def retry_intake(request: Request, project_id: str, job_id: str) -> IntakeView:
        require_project(request, project_id, "propose")
        return IntakeView.model_validate(repo.retry_failed_intake(project_id, job_id))

    @app.get(
        "/api/projects/{project_id}/sources/{source_id}/fragments",
        response_model=list[FragmentView],
    )
    def fragments(request: Request, project_id: str, source_id: str) -> list[FragmentView]:
        require_project(request, project_id, "read")
        return [
            FragmentView.model_validate(row) for row in repo.list_fragments(project_id, source_id)
        ]

    @app.get(
        "/api/projects/{project_id}/sources/{source_id}/financial-mapping",
        response_model=FinancialMappingView,
    )
    def financial_mapping(
        request: Request, project_id: str, source_id: str
    ) -> FinancialMappingView:
        require_project(request, project_id, "read")
        source = repo.get_source(project_id, source_id)
        if source is None or source.kind != "document":
            raise HTTPException(status_code=404, detail="document source not found")
        job = next(
            (item for item in repo.list_intake(project_id) if item.source_id == source_id),
            None,
        )
        if job is None or job.status != "completed":
            raise HTTPException(status_code=409, detail="document extraction is not complete")
        fragments = repo.list_fragments(project_id, source_id)
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
        source = repo.get_source(project_id, source_id)
        if source is None or source.kind != "document":
            raise HTTPException(status_code=404, detail="document source not found")
        job = next(
            (item for item in repo.list_intake(project_id) if item.source_id == source_id),
            None,
        )
        if job is None or job.status != "completed":
            raise HTTPException(status_code=409, detail="document extraction is not complete")
        result = map_operating_fragments(
            source.reference, repo.list_fragments(project_id, source_id)
        )
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
        before = repo.get_source(project_id, before_source_id)
        after = repo.get_source(project_id, source_id)
        if before is None or after is None or before.kind != "document" or after.kind != "document":
            raise HTTPException(status_code=404, detail="document source not found")
        completed = {
            job.source_id for job in repo.list_intake(project_id) if job.status == "completed"
        }
        if before.id not in completed or after.id not in completed:
            raise HTTPException(status_code=409, detail="both documents must finish extraction")
        return SourceComparisonView(
            before_source_id=before.id,
            after_source_id=after.id,
            before_sha256=before.content_sha256,
            after_sha256=after.content_sha256,
            **compare_fragments(
                repo.list_fragments(project_id, before.id),
                repo.list_fragments(project_id, after.id),
            ),
        )

    @app.post(
        "/api/projects/{project_id}/observations",
        response_model=ObservationView,
        status_code=201,
    )
    def add_observation(
        request: Request, project_id: str, body: ObservationCreate
    ) -> ObservationView:
        require_project(request, project_id, "propose")
        return ObservationView.model_validate(
            repo.add_observation(
                project_id, **body.model_dump(), verified_actor_id=session(request).user_id
            )
        )

    @app.post(
        "/api/projects/{project_id}/candidate-proposals",
        response_model=ObservationView,
        status_code=201,
    )
    def propose_candidate(
        request: Request, project_id: str, body: CandidateProposalCreate
    ) -> ObservationView:
        require_project(request, project_id, "propose")
        return ObservationView.model_validate(
            task_service.propose_candidate(
                project_id, **body.model_dump(), verified_actor_id=session(request).user_id
            )
        )

    @app.get("/api/projects/{project_id}/observations", response_model=list[ObservationView])
    def observations(
        request: Request,
        project_id: str,
        status_filter: Literal["candidate", "confirmed", "rejected"] | None = None,
    ) -> list[ObservationView]:
        require_project(request, project_id, "read")
        return [
            ObservationView.model_validate(row)
            for row in repo.list_observations(project_id, status=status_filter)
        ]

    @app.get("/api/projects/{project_id}/watch-items", response_model=list[WatchItemView])
    def list_watch_items(request: Request, project_id: str) -> list[WatchItemView]:
        require_project(request, project_id, "read")
        return watch_item_views(
            repo.list_watch_items(project_id),
            repo.list_observations(project_id),
            repo.list_sources(project_id),
        )

    @app.post(
        "/api/projects/{project_id}/watch-items", response_model=WatchItemView, status_code=201
    )
    def propose_watch_item(
        request: Request, project_id: str, body: WatchItemCreate
    ) -> WatchItemView:
        require_project(request, project_id, "propose")
        item = repo.propose_watch_item(
            project_id, **body.model_dump(), verified_actor_id=session(request).user_id
        )
        return watch_item_views(
            [item], repo.list_observations(project_id), repo.list_sources(project_id)
        )[0]

    @app.post(
        "/api/projects/{project_id}/watch-items/{item_id}/review", response_model=WatchItemView
    )
    def review_watch_item(
        request: Request, project_id: str, item_id: str, body: WatchItemReview
    ) -> WatchItemView:
        require_project(request, project_id, "review")
        item = repo.review_watch_item(
            project_id,
            item_id,
            action=body.action,
            expected_version=body.expected_version,
            verified_reviewer_id=session(request).user_id,
            note=body.note,
        )
        return watch_item_views(
            [item], repo.list_observations(project_id), repo.list_sources(project_id)
        )[0]

    @app.get(
        "/api/projects/{project_id}/watch-item-changes",
        response_model=list[WatchItemChangeView],
    )
    def list_watch_item_changes(request: Request, project_id: str) -> list[WatchItemChangeView]:
        require_project(request, project_id, "read")
        return watch_change_views(repo.list_watch_item_changes(project_id))

    @app.post(
        "/api/projects/{project_id}/watch-items/{item_id}/changes",
        response_model=WatchItemChangeView,
        status_code=201,
    )
    def propose_watch_item_change(
        request: Request, project_id: str, item_id: str, body: WatchItemChangeCreate
    ) -> WatchItemChangeView:
        require_project(request, project_id, "propose")
        change = repo.propose_watch_item_change(
            project_id,
            item_id,
            **body.model_dump(),
            verified_actor_id=session(request).user_id,
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
        item = repo.review_watch_item_change(
            project_id,
            item_id,
            change_id,
            action=body.action,
            expected_version=body.expected_version,
            verified_reviewer_id=session(request).user_id,
            note=body.note,
        )
        return watch_item_views(
            [item], repo.list_observations(project_id), repo.list_sources(project_id)
        )[0]

    @app.get(
        "/api/projects/{project_id}/competitive-peers", response_model=list[CompetitivePeerView]
    )
    def list_competitive_peers(request: Request, project_id: str) -> list[CompetitivePeerView]:
        require_project(request, project_id, "read")
        return [visible_peer(request, row) for row in repo.list_competitive_peers(project_id)]

    @app.get("/api/projects/{project_id}/holding-snapshots", response_model=list[HoldingSnapshotView])
    def list_holding_snapshots(request: Request, project_id: str) -> list[HoldingSnapshotView]:
        require_project(request, project_id, "read")
        return [HoldingSnapshotView.model_validate(row) for row in repo.list_holding_snapshots(project_id)]

    @app.post(
        "/api/projects/{project_id}/holding-snapshots",
        response_model=HoldingSnapshotView,
        status_code=201,
    )
    def propose_holding_snapshot(
        request: Request, project_id: str, body: HoldingSnapshotCreate
    ) -> HoldingSnapshotView:
        require_project(request, project_id, "propose")
        return HoldingSnapshotView.model_validate(
            repo.propose_holding_snapshot(
                project_id, **body.model_dump(), verified_actor_id=session(request).user_id,
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
        return HoldingSnapshotView.model_validate(
            repo.review_holding_snapshot(
                project_id, snapshot_id, action=body.action,
                expected_version=body.expected_version,
                verified_reviewer_id=session(request).user_id, note=body.note,
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
        return CompetitivePeerView.model_validate(
            repo.propose_competitive_peer(
                project_id,
                name=body.name,
                linked_project_id=body.linked_project_id,
                verified_actor_id=session(request).user_id,
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
        return CompetitivePeerView.model_validate(
            repo.review_competitive_peer(
                project_id,
                peer_id,
                action=body.action,
                expected_version=body.expected_version,
                verified_reviewer_id=session(request).user_id,
                note=body.note,
            )
        )

    @app.get("/api/projects/{project_id}/competitive-position")
    def competitive_position(request: Request, project_id: str) -> dict[str, object]:
        require_project(request, project_id, "read")
        peers = repo.list_competitive_peers(project_id)
        approved = [peer for peer in peers if peer.status == "confirmed"]
        metrics = project_competitive_metrics(
            project_id,
            project_id,
            repo.list_observations(project_id),
            repo.list_sources(project_id),
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
                    repo.list_observations(peer.linked_project_id),
                    repo.list_sources(peer.linked_project_id),
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
                repo.list_observations(project_id, status="confirmed"),
                repo.list_sources(project_id),
            )
        )

    @app.get("/api/projects/{project_id}/financial-screen")
    def financial_screen(request: Request, project_id: str) -> dict[str, object]:
        require_project(request, project_id, "read")
        return screen_confirmed_financials(
            repo.list_observations(project_id), repo.list_sources(project_id)
        )

    @app.get("/api/projects/{project_id}/monitoring/domains")
    def monitoring_domains(request: Request, project_id: str) -> dict[str, object]:
        require_project(request, project_id, "read")
        return build_monitoring_projection(
            project_id,
            repo.list_observations(project_id),
            repo.list_sources(project_id),
            repo.list_monitoring_plans(project_id),
            runs=repo.list_latest_source_runs(project_id),
            holding_snapshots=repo.list_holding_snapshots(project_id),
        )

    @app.get("/api/projects/{project_id}/report/current")
    def current_report(request: Request, response: Response, project_id: str) -> dict[str, object]:
        require_project(request, project_id, "read")
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        response.headers["Cache-Control"] = "no-store"
        brief = build_current_brief(
            project,
            repo.list_observations(project_id),
            repo.list_sources(project_id),
            repo.list_monitoring_plans(project_id),
            repo.list_signals(project_id),
            repo.list_review_impacts(project_id),
            runs=repo.list_latest_source_runs(project_id),
            watch_items=repo.list_watch_items(project_id),
            watch_changes=repo.list_watch_item_changes(project_id),
            holding_snapshots=repo.list_holding_snapshots(project_id),
        )
        attach_review_proof(
            brief,
            repo.review_decisions_for_report(project_id, brief_observation_ids(brief)),
            reviewer_authentication="authenticated_team",
        )
        return attach_locator_status(
            brief,
            repo.report_locator_status(project_id, brief_source_locators(brief)),
        )

    @app.get("/api/projects/{project_id}/review-impacts", response_model=list[ReviewImpactView])
    def review_impacts(request: Request, project_id: str) -> list[ReviewImpactView]:
        require_project(request, project_id, "read")
        return [
            ReviewImpactView.model_validate(row) for row in repo.list_review_impacts(project_id)
        ]

    @app.post(
        "/api/projects/{project_id}/review-impacts/{impact_id}/resolve",
        response_model=ReviewImpactView,
    )
    def resolve_review_impact(
        request: Request, project_id: str, impact_id: str, body: ReviewImpactResolution
    ) -> ReviewImpactView:
        require_project(request, project_id, "review")
        return ReviewImpactView.model_validate(
            repo.resolve_review_impact(
                project_id,
                impact_id,
                resolution=body.resolution,
                note=body.note,
                verified_reviewer_id=session(request).user_id,
            )
        )

    @app.post(
        "/api/projects/{project_id}/observations/{observation_id}/confirm",
        response_model=ObservationView,
    )
    def confirm_observation(
        request: Request, project_id: str, observation_id: str, body: ReviewRequest
    ) -> ObservationView:
        require_project(request, project_id, "review")
        return ObservationView.model_validate(
            repo.confirm_observation(
                project_id,
                observation_id,
                verified_reviewer_id=session(request).user_id,
                expected_version=body.expected_version,
            )
        )

    @app.post(
        "/api/projects/{project_id}/observations/{observation_id}/reject",
        response_model=ObservationView,
    )
    def reject_observation(
        request: Request, project_id: str, observation_id: str, body: ReviewRequest
    ) -> ObservationView:
        require_project(request, project_id, "review")
        return ObservationView.model_validate(
            repo.reject_observation(
                project_id,
                observation_id,
                verified_reviewer_id=session(request).user_id,
                expected_version=body.expected_version,
            )
        )

    @app.post(
        "/api/projects/{project_id}/observations/batch-review",
        response_model=list[ObservationView],
    )
    def batch_review_observations(
        request: Request, project_id: str, body: BatchReviewRequest
    ) -> list[ObservationView]:
        require_project(request, project_id, "review")
        reviewed = repo.review_observations_batch(
            project_id,
            tuple((item.observation_id, item.expected_version, item.action) for item in body.items),
            verified_reviewer_id=session(request).user_id,
        )
        return [ObservationView.model_validate(row) for row in reviewed]

    @app.get(
        "/api/projects/{project_id}/observations/{observation_id}/reviews",
        response_model=list[ReviewView],
    )
    def reviews(request: Request, project_id: str, observation_id: str) -> list[ReviewView]:
        require_project(request, project_id, "read")
        return [
            ReviewView.model_validate(row)
            for row in repo.review_history(project_id, observation_id)
        ]

    @app.post("/api/projects/{project_id}/tasks", response_model=TaskView, status_code=201)
    def create_task(request: Request, project_id: str, body: TaskCreate) -> TaskView:
        require_project(request, project_id, "propose")
        return TaskView.model_validate(
            task_service.run_coverage(
                project_id, source_ids=body.source_ids, idempotency_key=body.idempotency_key
            )
        )

    @app.get("/api/projects/{project_id}/tasks", response_model=list[TaskView])
    def tasks(request: Request, project_id: str) -> list[TaskView]:
        require_project(request, project_id, "read")
        return [TaskView.model_validate(row) for row in repo.list_execution_tasks(project_id)]

    @app.get("/api/projects/{project_id}/audit", response_model=list[AuditEventView])
    def audit(request: Request, project_id: str) -> list[AuditEventView]:
        require_project(request, project_id, "read")
        return [AuditEventView.model_validate(row) for row in repo.list_audit_events(project_id)]

    @app.post(
        "/api/projects/{project_id}/monitoring/plans",
        response_model=MonitoringPlanView,
        status_code=201,
    )
    def create_monitoring_plan(
        request: Request, project_id: str, body: MonitoringPlanCreate
    ) -> MonitoringPlanView:
        require_project(request, project_id, "manage")
        return MonitoringPlanView.model_validate(
            repo.create_monitoring_plan(project_id, **body.model_dump())
        )

    @app.get(
        "/api/projects/{project_id}/monitoring/public-search",
        response_model=PublicSearchPreview,
    )
    def preview_public_search(
        request: Request, project_id: str, provider: Literal["gdelt", "brave"] = "gdelt"
    ) -> PublicSearchPreview:
        require_project(request, project_id, "read")
        project = repo.get_project(project_id)
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
                or (bool(brave_api_key) and brave_monthly_limit > repo.brave_news_usage())
            ),
            monthly_limit=brave_monthly_limit if provider == "brave" else None,
            monthly_used=repo.brave_news_usage() if provider == "brave" else None,
        )

    @app.post(
        "/api/projects/{project_id}/monitoring/public-search",
        response_model=PublicSearchStarted,
        status_code=202,
    )
    def start_public_search(
        request: Request, project_id: str, body: PublicSearchStart
    ) -> PublicSearchStarted:
        require_project(request, project_id, "manage")
        project = repo.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if body.provider == "brave":
            if not brave_api_key or brave_monthly_limit <= repo.brave_news_usage():
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
        plan = repo.create_monitoring_plan(
            project_id, kind=kind, target=target, cadence_minutes=CADENCE_MINUTES
        )
        if not plan.enabled:
            plan = repo.set_monitoring_plan_enabled(project_id, plan.id, True)
        job = repo.request_monitoring_run(project_id, plan.id, idempotency_key=initial_run_key)
        return PublicSearchStarted(
            plan=MonitoringPlanView.model_validate(plan),
            job=MonitoringJobView.model_validate(job),
        )

    @app.get("/api/projects/{project_id}/monitoring/plans", response_model=list[MonitoringPlanView])
    def list_monitoring_plans(request: Request, project_id: str) -> list[MonitoringPlanView]:
        require_project(request, project_id, "read")
        return [
            MonitoringPlanView.model_validate(row) for row in repo.list_monitoring_plans(project_id)
        ]

    @app.patch(
        "/api/projects/{project_id}/monitoring/plans/{plan_id}",
        response_model=MonitoringPlanView,
    )
    def patch_monitoring_plan(
        request: Request, project_id: str, plan_id: str, body: MonitoringPlanPatch
    ) -> MonitoringPlanView:
        require_project(request, project_id, "manage")
        return MonitoringPlanView.model_validate(
            repo.set_monitoring_plan_enabled(project_id, plan_id, body.enabled)
        )

    @app.post(
        "/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
        response_model=MonitoringJobView,
        status_code=202,
    )
    def request_monitoring_run(
        request: Request, project_id: str, plan_id: str, body: MonitoringRunRequest
    ) -> MonitoringJobView:
        require_project(request, project_id, "manage")
        return MonitoringJobView.model_validate(
            repo.request_monitoring_run(project_id, plan_id, idempotency_key=body.idempotency_key)
        )

    @app.get(
        "/api/projects/{project_id}/monitoring/plans/{plan_id}/runs",
        response_model=list[SourceRunView],
    )
    def list_monitoring_runs(
        request: Request, project_id: str, plan_id: str
    ) -> list[SourceRunView]:
        require_project(request, project_id, "read")
        if repo.get_monitoring_plan(project_id, plan_id) is None:
            raise HTTPException(status_code=404, detail="monitoring plan not found")
        return [
            SourceRunView.model_validate(row) for row in repo.list_source_runs(project_id, plan_id)
        ]

    @app.get("/api/projects/{project_id}/monitoring/jobs", response_model=list[MonitoringJobView])
    def list_monitoring_jobs(request: Request, project_id: str) -> list[MonitoringJobView]:
        require_project(request, project_id, "read")
        return [
            MonitoringJobView.model_validate(row) for row in repo.list_monitoring_jobs(project_id)
        ]

    @app.post(
        "/api/projects/{project_id}/monitoring/jobs/{job_id}/retry",
        response_model=MonitoringJobView,
    )
    def retry_monitoring_job(request: Request, project_id: str, job_id: str) -> MonitoringJobView:
        require_project(request, project_id, "manage")
        return MonitoringJobView.model_validate(
            repo.retry_failed_monitoring_job(project_id, job_id)
        )

    @app.get("/api/projects/{project_id}/monitoring/signals", response_model=list[SignalView])
    def list_monitoring_signals(request: Request, project_id: str) -> list[SignalView]:
        require_project(request, project_id, "read")
        return [
            SignalView.model_validate(row)
            for row in deduplicate_project_signals(repo.list_signals(project_id))
        ]

    return app


def create_postgres_team_app_from_env() -> FastAPI:
    """Single-process ASGI entrypoint; never runs schema migration implicitly."""
    dsn = os.environ.get("PROJECT_WATCH_DATABASE_URL")
    object_root = os.environ.get("PROJECT_WATCH_OBJECT_ROOT")
    origin = os.environ.get("PROJECT_WATCH_TEAM_ORIGIN")
    parser_image = os.environ.get("PROJECT_WATCH_PARSER_IMAGE")
    parser_docker_host = os.environ.get("PROJECT_WATCH_PARSER_DOCKER_HOST")
    gdelt_proxy_url = os.environ.get("PROJECT_WATCH_GDELT_PROXY_URL")
    brave_api_key = os.environ.get("PROJECT_WATCH_BRAVE_API_KEY")
    brave_monthly_limit = int(os.environ.get("PROJECT_WATCH_BRAVE_MONTHLY_LIMIT", "0"))
    brave_proxy_url = os.environ.get("PROJECT_WATCH_BRAVE_PROXY_URL")
    if not all((dsn, object_root, origin)):
        raise RuntimeError("PostgreSQL team API requires database, object root and HTTPS origin")
    if not all((parser_image, parser_docker_host)):
        raise RuntimeError("team intake requires a pinned parser image and local Docker host")
    if not Path(object_root).is_absolute():
        raise ValueError("PostgreSQL object root must be an absolute path")
    return create_postgres_team_app(
        dsn,
        object_root=object_root,
        team_origin=origin,
        start_worker=True,
        parser_image=parser_image,
        parser_docker_host=parser_docker_host,
        gdelt_proxy_url=gdelt_proxy_url,
        brave_api_key=brave_api_key,
        brave_monthly_limit=brave_monthly_limit,
        brave_proxy_url=brave_proxy_url,
    )
