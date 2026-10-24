"""Local Studio foundation without mounting classic analysis APIs/workers.

Repository schema and document originals remain compatible with a full install.
This is not a team/authenticated server and is not a public-distribution clearance.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from project_watch.blob_store import BlobStore
from project_watch.evidence_proposals import EvidenceConflictError
from project_watch.intake_worker import IntakeWorker
from project_watch.repository import (
    IntakeConflictError,
    NotFoundError,
    ProjectRepository,
    ReviewConflictError,
)
from project_watch.team_http import protect_request


def create_local_base(db_path: Path, *, start_worker: bool) -> FastAPI:
    repository = ProjectRepository(db_path)
    worker = IntakeWorker(repository, BlobStore(db_path.parent / 'objects', max_bytes=None),
                          isolated=True, derive_candidates=False)

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            if start_worker:
                worker.stop()

    app = FastAPI(title='ProjectWatch — local Studio', lifespan=lifespan)
    app.state.intake_worker = worker
    app.state.monitoring_scheduler = None
    app.state.reading_worker = None
    app.state.classic_available = False

    @app.middleware('http')
    async def security_headers(request: Request, call_next):
        return await protect_request(request, call_next, team_auth=None, team_origin=None)

    async def not_found(_request, exc):
        return JSONResponse(status_code=404, content={'detail': str(exc)})

    async def conflict(_request, exc):
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    async def invalid(_request, exc):
        return JSONResponse(status_code=422, content={'detail': str(exc)})

    app.add_exception_handler(NotFoundError, not_found)
    for error in (ReviewConflictError, EvidenceConflictError, IntakeConflictError):
        app.add_exception_handler(error, conflict)
    app.add_exception_handler(ValueError, invalid)

    @app.get('/api/runtime')
    def runtime_info():
        return {'mode': 'local', 'parser': 'subprocess', 'private_local_intake': False,
                'authenticated_review': False, 'classic_available': False}

    @app.get('/api/sources/{source_id}/fragments')
    def fragments(source_id: str):
        if repository.get_source(source_id) is None:
            raise HTTPException(404, '资料不存在')
        return jsonable_encoder(repository.list_fragments(source_id))

    return app
