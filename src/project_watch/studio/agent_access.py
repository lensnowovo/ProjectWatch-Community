"""Local Studio Agent endpoints, backed by the same application as the web UI.

Local owner access retains Studio's loopback/Host/Origin trust model. Project
grants constrain compliant Agent clients, not a hostile program running as the
same OS user (which already controls the local service/data). Not a team server.
"""

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from project_watch.studio.agent_contracts import (
    GrantInput,
    ObjectMutation,
    SourceAuthorization,
    StatusInput,
    TextSubmission,
)
from project_watch.studio.application import (
    AccessDenied,
    ProjectMissing,
    StudioApplication,
    VersionConflict,
)
from project_watch.studio.concerns import ConcernFeedback
from project_watch.studio.monitoring_contracts import LocatedRef
from project_watch.studio.questions import Assessment
from project_watch.studio.relationship_contracts import PeerSelection, RelationshipCorrection


def attach_agent_routes(app: FastAPI, service: StudioApplication) -> None:
    @app.middleware("http")
    async def restrict_bearer_surface(request: Request, call_next):
        # A project token cannot be forwarded to legacy settings or mutation
        # routes to bypass its narrower scope. Missing-header local owner calls
        # remain the existing trusted single-user interface, not remote auth.
        if request.headers.get("authorization") and not request.url.path.startswith("/api/studio/agent/"):
            return JSONResponse({"detail": "Agent credentials only apply to scoped Agent routes"}, status_code=403,
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return await call_next(request)

    @app.exception_handler(AccessDenied)
    async def denied(_request: Request, exc: AccessDenied):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(VersionConflict)
    async def conflict(_request: Request, exc: VersionConflict):
        return JSONResponse({"detail": str(exc), "code": "version_conflict"}, status_code=409)

    @app.exception_handler(ProjectMissing)
    async def missing(_request: Request, _exc: ProjectMissing):
        return JSONResponse({"detail": "not found in project"}, status_code=404)

    def scoped(request: Request, project_id: str, permission: str = "read") -> str:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "project Agent grant required")
        service.check_instance(request.headers.get("x-projectwatch-instance", ""))
        token = authorization.removeprefix("Bearer ")
        service.authorize(token, project_id, permission)
        return token

    from project_watch.studio.agent_connections import attach

    attach(app, service, service.materials.settings_store.path.parent,
           app.state.studio_pipeline.monitoring_plan, scoped)

    @app.get("/api/studio/agent/runtime")
    def manifest(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service.manifest()

    @app.post("/api/studio/projects/{project_id}/agent-grants", status_code=201)
    def issue(project_id: str, body: GrantInput, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return service.issue_grant(project_id, body.permission, body.label, hours=body.hours,
                                   allow_internal=body.allow_internal)

    @app.get("/api/studio/projects/{project_id}/agent-grants")
    def grants(project_id: str):
        return service.list_grants(project_id)

    @app.delete("/api/studio/projects/{project_id}/agent-grants/{grant_id}", status_code=204)
    def revoke(project_id: str, grant_id: str):
        service.revoke_grant(project_id, grant_id)

    @app.get("/api/studio/agent/projects/{project_id}")
    def project(project_id: str, request: Request):
        scoped(request, project_id)
        return service.project(project_id)

    @app.get("/api/studio/agent/projects/{project_id}/search")
    def search(project_id: str, request: Request, q: str = Query(default="", max_length=200)):
        scoped(request, project_id)
        return service.search(q, [project_id])

    @app.patch("/api/studio/agent/projects/{project_id}/status")
    def status(project_id: str, body: StatusInput, request: Request):
        token = scoped(request, project_id, "write")
        return service.change_status(project_id, body.status, expected_version=body.expected_version,
                                     idempotency_key=body.idempotency_key, actor="", token=token)

    def question_ledger():
        from project_watch.studio.materials import model_binding
        from project_watch.studio.monitor import MonitorStore
        from project_watch.studio.questions import QuestionLedger

        return QuestionLedger(service.store, MonitorStore(service.store),
                              binding_provider=lambda: model_binding(service.materials.settings_store.load().model))

    @app.get("/api/studio/agent/projects/{project_id}/questions")
    def agent_questions(project_id: str, request: Request):
        """Project question pack restricted to the grant's disclosure scope."""
        internal = evidence_scope(request, project_id)
        from project_watch.studio.questions import STATUSES

        return {"questions": question_ledger().view(project_id, include_internal=internal), "statuses": STATUSES}

    @app.post("/api/studio/agent/projects/{project_id}/questions/{watch_id}/assessments", status_code=201)
    def agent_assessment(project_id: str, watch_id: str, body: Assessment, request: Request):
        token = scoped(request, project_id, "write")
        internal = service.authorize(token, project_id, "write")["allow_internal"]
        if not body.idempotency_key:
            raise HTTPException(422, "idempotency_key is required for Agent judgements")
        try:
            ledger = question_ledger()
            with service.store.write_scope(lambda db: service._authorize(db, token, project_id, "write")):
                return ledger.record(project_id, watch_id, "agent", body, include_internal=internal)
        except KeyError as exc:
            raise HTTPException(404, "question not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.get("/api/studio/agent/projects/{project_id}/operations/{operation_id}")
    def receipt(project_id: str, operation_id: str, request: Request):
        scoped(request, project_id)
        return service.receipt(project_id, operation_id)

    def evidence_scope(request: Request, project_id: str) -> bool:
        token = scoped(request, project_id)
        return service.authorize(token, project_id, "read")["allow_internal"]

    from project_watch.studio import agent_results
    from project_watch.studio.agent_result_contracts import MonitoringResults

    @app.post('/api/studio/agent/projects/{project_id}/results', status_code=201)
    def submit_results(project_id: str, body: MonitoringResults, request: Request):
        token = scoped(request, project_id, 'write')
        try:
            return agent_results.submit(service, project_id, body, token)
        except ValueError as exc:
            if isinstance(exc, (AccessDenied, VersionConflict, ProjectMissing)):
                raise
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/studio/agent/projects/{project_id}/results')
    def result_history(project_id: str, request: Request):
        internal = evidence_scope(request, project_id)
        return {**agent_results.history(service.store, project_id, include_internal=internal),
                'instance_id':service.instance_id,'schema_version':1,'project_id':project_id}

    @app.get('/api/studio/agent/projects/{project_id}/results/current')
    def current_result(project_id: str, request: Request):
        internal = evidence_scope(request, project_id)
        return {'record':agent_results.current(service.store, project_id, include_internal=internal),
                'instance_id':service.instance_id,'schema_version':1,'project_id':project_id}

    def submit_source(project_id, filename, stream, expected_version, idempotency_key, token):
        try:
            return service.materials.submit(project_id, filename, stream, expected_version=expected_version,
                                            idempotency_key=idempotency_key, token=token)
        except (AccessDenied, VersionConflict):
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/studio/agent/projects/{project_id}/sources", status_code=202)
    def submit_file(project_id: str, request: Request, file: UploadFile = File(...),
                    expected_version: int = Form(..., ge=1), idempotency_key: str = Form(..., min_length=1, max_length=100)):
        token = scoped(request, project_id, "write")
        return submit_source(project_id, file.filename or "", file.file, expected_version, idempotency_key, token)

    @app.post("/api/studio/agent/projects/{project_id}/text-sources", status_code=202)
    def submit_text(project_id: str, request: Request, body: TextSubmission):
        import io

        token = scoped(request, project_id, "write")
        if not body.filename.lower().endswith(".txt"):
            raise HTTPException(422, "text submission requires a .txt filename")
        return submit_source(project_id, body.filename, io.BytesIO(body.text.encode("utf-8")),
                             body.expected_version, body.idempotency_key, token)

    @app.get("/api/studio/agent/projects/{project_id}/sources/{source_id}")
    def source_status(project_id: str, source_id: str, request: Request):
        if not evidence_scope(request, project_id):
            raise AccessDenied("source status requires explicit internal read scope")
        return service.materials.status(project_id, source_id)

    @app.post("/api/studio/projects/{project_id}/sources/{source_id}/authorize-analysis")
    def authorize_source(project_id: str, source_id: str, body: SourceAuthorization):
        try:
            return service.materials.authorize_analysis(project_id, source_id, body.expected_version, body.idempotency_key)
        except (AccessDenied, VersionConflict):
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/studio/agent/projects/{project_id}/evidence")
    def evidence(project_id: str, request: Request, limit: int = Query(20, ge=1, le=100),
                 cursor: str | None = Query(None, max_length=1000)):
        internal = evidence_scope(request, project_id)
        try:
            return {**service.evidence.page(project_id, limit=limit, cursor=cursor, include_internal=internal),
                    "instance_id": service.instance_id}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/studio/projects/{project_id}/evidence/{version_id}/parts/{part_index}")
    def owner_excerpt(project_id: str, version_id: str, part_index: int, response: Response,
                      start: int = Query(0, ge=0), end: int = Query(..., ge=1)):
        service.project(project_id)
        response.headers['Cache-Control'] = 'no-store'
        if not 0 < end - start <= 2000:
            raise HTTPException(422, 'excerpt must contain 1 to 2000 characters')
        try:
            record = service.evidence.get(project_id, version_id, include_internal=True)
            if not 0 <= part_index < len(record['parts']):
                raise KeyError
            part = record['parts'][part_index]
            if end > len(part['text']):
                raise ValueError('excerpt is outside the stored part')
            return {'project_id': project_id, 'schema_version': 1, 'evidence_version_id': version_id,
                    'part_index': part_index, 'start': start, 'end': end, 'quote': part['text'][start:end],
                    'locator': str(part['locator']), 'read_scope': record['read_scope']}
        except KeyError as exc:
            raise HTTPException(404, 'evidence part not found in project') from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/studio/agent/projects/{project_id}/evidence/{version_id}/parts/{part_index}")
    def part(project_id: str, version_id: str, part_index: int, request: Request):
        internal = evidence_scope(request, project_id)
        try:
            record = service.evidence.get(project_id, version_id, include_internal=internal)
            if not 0 <= part_index < len(record["parts"]):
                raise KeyError
            return {"schema_version": 1, "instance_id": service.instance_id, "project_id": project_id,
                    "evidence_version_id": version_id, "part_index": part_index, "part": record["parts"][part_index],
                    "read_scope": record["read_scope"], "origin": record["origin"]}
        except KeyError as exc:
            raise HTTPException(404, "evidence part not found in permitted project scope") from exc

    @app.post("/api/studio/agent/projects/{project_id}/evidence/resolve")
    def resolve(project_id: str, body: LocatedRef, request: Request):
        internal = evidence_scope(request, project_id)
        try:
            return {**service.evidence.resolve(project_id, body, include_internal=internal),
                    "instance_id": service.instance_id}
        except KeyError as exc:
            raise HTTPException(404, "evidence not found in permitted project scope") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def maintain(project_id: str, body: ObjectMutation, token: str | None = None):
        try:
            return service.maintain_object(project_id, body, token=token)
        except KeyError as exc:
            raise HTTPException(404, "object/evidence not found in permitted project") from exc
        except (VersionConflict, AccessDenied):
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/studio/projects/{project_id}/monitor-objects")
    def owner_object(project_id: str, body: ObjectMutation):
        return maintain(project_id, body)

    @app.post("/api/studio/agent/projects/{project_id}/objects")
    def agent_object(project_id: str, body: ObjectMutation, request: Request):
        return maintain(project_id, body, scoped(request, project_id, "write"))

    @app.get("/api/studio/agent/projects/{project_id}/objects")
    def objects(project_id: str, request: Request, limit: int = Query(20, ge=1, le=100),
                after: str = Query("", max_length=100)):
        internal = evidence_scope(request, project_id)
        # Metadata listing does not dump every identity's evidence quotes.
        rows = service.objects.page(project_id, include_internal=internal, limit=limit + 1, after=after)
        return {"instance_id": service.instance_id, "project_id": project_id, "schema_version": 1,
                "items": [{k: v for k, v in r.items() if k != "refs"} for r in rows[:limit]],
                "next_after": rows[limit - 1]["id"] if len(rows) > limit else None}

    @app.get("/api/studio/agent/projects/{project_id}/objects/{object_id}")
    def object_detail(project_id: str, object_id: str, request: Request):
        internal = evidence_scope(request, project_id)
        try:
            return {**service.objects.get(project_id, object_id, include_internal=internal), "instance_id": service.instance_id}
        except KeyError as exc:
            raise HTTPException(404, "object not found in permitted project") from exc

    @app.get("/api/studio/agent/projects/{project_id}/objects/{object_id}/facts")
    def facts(project_id: str, object_id: str, request: Request):
        internal = evidence_scope(request, project_id)
        try:
            rows = service.objects.facts(project_id, object_id, include_internal=internal)
        except KeyError as exc:
            raise HTTPException(404, "object not found in permitted project") from exc
        return {"instance_id": service.instance_id, "project_id": project_id, "schema_version": 1,
                "items": [{**r, "refs": [{k: v for k, v in ref.items() if k != "quote"} for ref in r["refs"]]}
                          for r in rows], "truncated": len(rows) == 500}

    @app.get("/api/studio/agent/projects/{project_id}/changes")
    def changes(project_id: str, request: Request, after: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                through: int | None = Query(None, ge=0)):
        internal = evidence_scope(request, project_id)
        try:
            return {**service.evidence.dependencies.changes(project_id, after=after, limit=limit, through=through,
                                                            include_internal=internal), "instance_id": service.instance_id}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def correct_relationships(project_id, body, token=None):
        try:
            return service.relationships.correct(service, project_id, body, token=token)
        except KeyError as exc:
            raise HTTPException(404, "relationship identity not found in permitted project") from exc
        except (AccessDenied, VersionConflict):
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/studio/projects/{project_id}/relationships/corrections")
    def owner_correct_relationships(project_id: str, body: RelationshipCorrection):
        return correct_relationships(project_id, body)

    @app.post("/api/studio/projects/{project_id}/competitors/selection")
    def owner_select_peer(project_id: str, body: PeerSelection):
        return correct_relationships(project_id, body)

    @app.post("/api/studio/agent/projects/{project_id}/relationships/corrections")
    def agent_correct_relationships(project_id: str, body: RelationshipCorrection, request: Request):
        return correct_relationships(project_id, body, scoped(request, project_id, "write"))

    @app.get("/api/studio/agent/projects/{project_id}/relationships")
    def relationships(project_id: str, request: Request, limit: int = Query(20, ge=1, le=100), after: str = Query("", max_length=100)):
        internal = evidence_scope(request, project_id)
        rows = [r for r in service.relationships.page(project_id, include_internal=internal) if r["scope_key"] > after]
        rows.sort(key=lambda r: r["scope_key"])
        def disclose(record):
            return {k: v for k, v in record.items() if internal or k not in ("reason", "rationale", "actor")}
        return {"instance_id": service.instance_id, "schema_version": 1, "project_id": project_id,
                "items": [disclose(r) for r in rows[:limit]], "next_after": rows[limit-1]["scope_key"] if len(rows) > limit else None,
                "overrides": [disclose(r) for r in service.relationships.overrides(project_id, include_internal=internal)[:limit]],
                "truncated": len(rows) == 500}

    @app.get("/api/studio/projects/{project_id}/health")
    def owner_health(project_id: str):
        service.project(project_id)
        return {**service.health.read(project_id), "instance_id": service.instance_id}

    @app.get('/api/studio/projects/{project_id}/observations')
    def owner_observations(project_id: str, response: Response, limit: int = Query(50, ge=1, le=100), after: str = Query('', max_length=100)):
        from project_watch.studio.observations import read

        response.headers['Cache-Control'] = 'no-store'
        return read(service, project_id, include_internal=True, limit=limit, after=after)

    @app.get('/api/studio/agent/projects/{project_id}/observations')
    def agent_observations(project_id: str, request: Request, response: Response, limit: int = Query(50, ge=1, le=100), after: str = Query('', max_length=100)):
        from project_watch.studio.observations import read

        internal = evidence_scope(request, project_id)
        response.headers['Cache-Control'] = 'no-store'
        return {**read(service, project_id, include_internal=internal, limit=limit, after=after), 'instance_id': service.instance_id}

    @app.get("/api/studio/projects/{project_id}/material-package")
    def owner_package(project_id: str, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service.material_package(project_id, include_internal=True)

    @app.get('/api/studio/projects/{project_id}/event-states')
    def owner_event_states(project_id: str, response: Response):
        from project_watch.studio.event_state import timeline

        service.project(project_id)
        response.headers['Cache-Control'] = 'no-store'
        return {**timeline(service, project_id, include_internal=True), 'instance_id': service.instance_id}

    @app.get('/api/studio/projects/{project_id}/milestones')
    def owner_milestones(project_id: str, response: Response):
        from project_watch.studio.milestones import read
        service.project(project_id)
        response.headers['Cache-Control'] = 'no-store'
        return {**read(service, project_id, include_internal=True), 'instance_id': service.instance_id}

    @app.get('/api/studio/agent/projects/{project_id}/milestones')
    def agent_milestones(project_id: str, request: Request, response: Response):
        from project_watch.studio.milestones import read
        internal = evidence_scope(request, project_id)
        response.headers['Cache-Control'] = 'no-store'
        return {**read(service, project_id, include_internal=internal), 'instance_id': service.instance_id}

    @app.get('/api/studio/projects/{project_id}/concerns')
    def owner_concerns(project_id: str, response: Response):
        service.project(project_id)
        response.headers['Cache-Control'] = 'no-store'
        return {**service.concerns.read(project_id), 'instance_id': service.instance_id}

    @app.get('/api/studio/agent/projects/{project_id}/concerns')
    def agent_concerns(project_id: str, request: Request, response: Response):
        internal = evidence_scope(request, project_id)
        response.headers['Cache-Control'] = 'no-store'
        return {**service.concerns.read(project_id, include_internal=internal), 'instance_id': service.instance_id}

    @app.post('/api/studio/projects/{project_id}/concern-feedback')
    def owner_concern_feedback(project_id: str, body: ConcernFeedback):
        return service.concerns.feedback(project_id, body)

    @app.post('/api/studio/agent/projects/{project_id}/concern-feedback')
    def agent_concern_feedback(project_id: str, request: Request, body: ConcernFeedback):
        token = scoped(request, project_id, 'write')
        return service.concerns.feedback(project_id, body, token=token)

    @app.get('/api/studio/agent/projects/{project_id}/event-states')
    def agent_event_states(project_id: str, request: Request, response: Response):
        from project_watch.studio.event_state import timeline

        internal = evidence_scope(request, project_id)
        response.headers['Cache-Control'] = 'no-store'
        return {**timeline(service, project_id, include_internal=internal), 'instance_id': service.instance_id}

    @app.get("/api/studio/agent/projects/{project_id}/material-package")
    def agent_package(project_id: str, request: Request, response: Response):
        internal = evidence_scope(request, project_id)
        response.headers['Cache-Control'] = 'no-store'
        return service.material_package(project_id, include_internal=internal)

    @app.get("/api/studio/agent/projects/{project_id}/health")
    def agent_health(project_id: str, request: Request):
        internal = evidence_scope(request, project_id)
        service.project(project_id)
        return {**service.health.read(project_id, include_internal=internal), "instance_id": service.instance_id}
