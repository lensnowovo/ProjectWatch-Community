"""Project-bound PostgreSQL MCP resource server with revocable bearer grants."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from project_watch.api import MetricSeriesResponse
from project_watch.evidence_pages import page_excerpts
from project_watch.metric_series import build_metric_series
from project_watch.postgres_core import PostgresCoreRepository
from project_watch.postgres_tasks import PostgresTaskService
from project_watch.postgres_team_auth import PostgresTeamAuthStore
from project_watch.repository import NotFoundError
from project_watch.tasks import EvidenceConflictError


def _validate_urls(resource_url: str, issuer_url: str) -> str:
    resource = urlsplit(resource_url)
    issuer = urlsplit(issuer_url)
    if (
        resource.scheme != "https"
        or resource.path != "/mcp"
        or not resource.netloc
        or resource.username
        or resource.password
        or resource.query
        or resource.fragment
        or issuer.scheme != "https"
        or not issuer.netloc
        or issuer.username
        or issuer.password
        or issuer.query
        or issuer.fragment
    ):
        raise ValueError("team MCP requires HTTPS URLs and a /mcp resource path")
    return resource.netloc


def build_postgres_mcp_server(
    repository: PostgresCoreRepository,
    team_auth: PostgresTeamAuthStore,
    project_id: str,
    *,
    resource_url: str,
    issuer_url: str,
):
    """Bind four bounded tools to one project; caller owns repository and auth pools."""
    try:
        from mcp.server import MCPServer
        from mcp.server.auth.middleware.auth_context import get_access_token
        from mcp.server.auth.provider import AccessToken, TokenVerifier
        from mcp.server.auth.settings import AuthSettings
        from mcp.server.mcpserver.exceptions import ToolError
        from pydantic import AnyHttpUrl
    except ImportError as exc:
        raise RuntimeError('Install the optional "mcp" extra to run the MCP bridge') from exc

    _validate_urls(resource_url, issuer_url)
    project = repository.get_project(project_id)
    if project is None:
        raise ValueError("bound project does not exist")
    tasks = PostgresTaskService(repository)

    class ProjectTokenVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            grant = team_auth.executor_grant(token)
            if grant is None or grant.project_id != project_id:
                return None
            scopes = ["watch:read"]
            if grant.permission == "propose":
                scopes.append("watch:propose")
            return AccessToken(
                token=token,
                client_id=grant.label,
                scopes=scopes,
                expires_at=int(grant.expires_at.timestamp()),
                resource=resource_url,
                subject=grant.id,
            )

    server = MCPServer(
        "Project Watch PostgreSQL",
        instructions=(
            "This server is bound to one project. Read evidence before proposing a candidate. "
            "Candidates require human review and are not confirmed institutional facts."
        ),
        token_verifier=ProjectTokenVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer_url),
            resource_server_url=AnyHttpUrl(resource_url),
            required_scopes=["watch:read"],
            validate_token_resource=True,
        ),
    )

    def require_executor(permission: str) -> str:
        access = get_access_token()
        if access is None:
            raise ToolError("authenticated HTTP executor required")
        grant = team_auth.executor_grant(access.token, permission=permission)
        if grant is None or grant.project_id != project_id:
            raise ToolError("executor grant expired or revoked")
        return grant.id

    @server.tool()
    def project_overview() -> dict[str, object]:
        """Show project identity and bounded source/review counts."""
        require_executor("read")
        sources = repository.list_sources(project_id)
        observations = repository.list_observations(project_id)
        return {
            "project_id": project.id,
            "name": project.name,
            "identity_status": project.identity_status,
            "source_count": len(sources),
            "sources": [
                {"id": row.id, "reference": row.reference, "content_sha256": row.content_sha256}
                for row in sources[:50]
            ],
            "sources_truncated": len(sources) > 50,
            "candidate_count": sum(row.status == "candidate" for row in observations),
            "confirmed_count": sum(row.status == "confirmed" for row in observations),
        }

    @server.tool()
    def source_excerpts(
        source_id: str, limit: int = 8, cursor: str | None = None
    ) -> dict[str, object]:
        """Read a bounded page of located excerpts, never original bytes or paths."""
        require_executor("read")
        source = repository.get_source(project_id, source_id)
        if source is None:
            raise ToolError("source not found in bound project")
        fragments = repository.list_fragments(project_id, source_id)
        try:
            page = page_excerpts(
                fragments, source_sha256=source.content_sha256, cursor=cursor, limit=limit
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "source_id": source.id,
            "content_sha256": source.content_sha256,
            **page,
        }

    @server.tool()
    def monitoring_status(limit: int = 20, include_metrics: bool = False) -> dict[str, object]:
        """Read source status and candidate signals without running a source."""
        require_executor("read")
        if not 1 <= limit <= 20:
            raise ToolError("limit must be between 1 and 20")
        plans = repository.list_monitoring_plans(project_id)
        signals = repository.list_signals(project_id)
        result = {
            "plans": [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "enabled": row.enabled,
                    "last_run_status": row.last_run_status,
                }
                for row in plans[:20]
            ],
            "plans_truncated": len(plans) > 20,
            "signals": [
                {
                    "id": row.id,
                    "title": row.title,
                    "url": row.url,
                    "freshness": row.freshness,
                    "status": row.status,
                    "first_seen_at": row.first_seen_at.isoformat(),
                }
                for row in signals[:limit]
            ],
            "signals_truncated": len(signals) > limit,
        }
        if include_metrics:
            result["metrics"] = MetricSeriesResponse.model_validate(
                build_metric_series(
                    repository.list_observations(project_id, status="confirmed"),
                    repository.list_sources(project_id),
                    points_per_series=4,
                )
            ).model_dump(mode="json")
        return result

    @server.tool()
    def propose_candidate(
        source_id: str,
        expected_source_sha256: str,
        source_locator: str,
        subject: str,
        statement: str,
        number: str | None = None,
        unit: str | None = None,
        period: str | None = None,
        scope: str | None = None,
        basis: str = "unknown",
    ) -> dict[str, object]:
        """Submit evidence-linked candidate; confirmation is never an executor tool."""
        grant_id = require_executor("propose")
        try:
            observation = tasks.propose_candidate(
                project_id,
                source_id=source_id,
                expected_source_sha256=expected_source_sha256,
                source_locator=source_locator,
                subject=subject,
                statement=statement,
                number=number,
                unit=unit,
                period=period,
                scope=scope,
                basis=basis,
                verified_actor_id=grant_id,
                submission_channel="team_mcp",
            )
        except (ValueError, NotFoundError, EvidenceConflictError) as exc:
            raise ToolError(str(exc)) from exc
        return {
            "id": observation.id,
            "project_id": observation.project_id,
            "source_id": observation.source_id,
            "source_locator": observation.source_locator,
            "status": observation.status,
            "version": observation.version,
            "number": str(observation.number) if observation.number is not None else None,
            "unit": observation.unit,
            "period": observation.period,
            "scope": observation.scope,
            "basis": observation.basis,
        }

    return server


def create_postgres_team_mcp_app():
    """ASGI factory for a dedicated, bootstrapped PostgreSQL team database."""
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError as exc:
        raise RuntimeError('Install the optional "mcp" extra to run the MCP bridge') from exc
    from starlette.responses import JSONResponse

    dsn = os.environ.get("PROJECT_WATCH_DATABASE_URL")
    project_id = os.environ.get("PROJECT_WATCH_PROJECT_ID")
    resource_url = os.environ.get("PROJECT_WATCH_MCP_RESOURCE_URL")
    issuer_url = os.environ.get("PROJECT_WATCH_MCP_ISSUER_URL")
    if not all((dsn, project_id, resource_url, issuer_url)):
        raise RuntimeError("team MCP requires database, project, resource URL and issuer URL")
    hostname = _validate_urls(resource_url, issuer_url)
    auth = PostgresTeamAuthStore(dsn)
    try:
        if not auth.is_bootstrapped():
            raise RuntimeError("team identities must be bootstrapped before MCP starts")
        repo = PostgresCoreRepository(dsn)
    except Exception:
        auth.close()
        raise
    try:
        server = build_postgres_mcp_server(
            repo, auth, project_id, resource_url=resource_url, issuer_url=issuer_url
        )
    except Exception:
        repo.close()
        auth.close()
        raise
    inner = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=[hostname], allowed_origins=[f"https://{hostname}"]
        )
    )

    async def https_only(scope, receive, send):
        if scope["type"] == "http" and scope.get("scheme") != "https":
            await JSONResponse({"detail": "HTTPS required"}, status_code=400)(scope, receive, send)
            return
        if scope["type"] == "lifespan":
            try:
                await inner(scope, receive, send)
            finally:
                repo.close()
                auth.close()
            return
        await inner(scope, receive, send)

    return https_only
