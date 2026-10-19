"""Single-node durable file-intake worker for the development preview."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from project_watch.blob_store import BlobStore
from project_watch.domain import IntakeJob
from project_watch.extraction_process import (
    MAX_PARSER_OUTPUT_BYTES,
    PARSER_TIMEOUT_SECONDS,
    PDF_PARSER_TIMEOUT_SECONDS,
    extract_document_subprocess,
)
from project_watch.extractors import ExtractionError, extract_document
from project_watch.financial_intake import map_financial_fragments
from project_watch.native_intake import analyze_document
from project_watch.operating_intake import map_operating_fragments
from project_watch.parser_container import (
    extract_document_container,
    validate_parser_docker_host,
    validate_parser_image,
)
from project_watch.repository import ProjectRepository

_log = logging.getLogger(__name__)


class IntakeWorker:
    def __init__(
        self, repository: ProjectRepository, blob_store: BlobStore, *, isolated: bool = False,
        parser_image: str | None = None, docker_host: str | None = None,
        derive_candidates: bool = True,
    ) -> None:
        if (parser_image is None) != (docker_host is None):
            raise ValueError("parser image and local Docker host must be configured together")
        self.repository = repository
        self.blob_store = blob_store
        self.isolated = isolated
        self.derive_candidates = derive_candidates
        self.parser_image = validate_parser_image(parser_image) if parser_image else None
        self.docker_host = validate_parser_docker_host(docker_host) if docker_host else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = False
        self._phase = 'idle'
        self._progress_at = time.monotonic()

    def _progress(self):
        self._progress_at = time.monotonic()

    def process_one(self) -> IntakeJob | None:
        if self._stop.is_set():
            return None
        try:
            return self._process_one()
        finally:
            self._active, self._phase = False, 'idle'

    def _process_one(self) -> IntakeJob | None:
        job = self.repository.claim_next_intake()
        if job is None:
            return None
        self._active, self._phase = True, 'parsing'
        self._progress()
        source = self.repository.get_source(job.source_id)
        if source is None:
            return self.repository.fail_intake(job.id, error_code="source_missing")
        try:
            stored_path = self.blob_store.path_for_hash(source.content_sha256)
            if self.parser_image is not None and self.docker_host is not None:
                result = extract_document_container(
                    stored_path, suffix=Path(source.reference).suffix,
                    image=self.parser_image, docker_host=self.docker_host,
                )
            else:
                suffix = Path(source.reference).suffix
                if self.isolated and self.blob_store.max_bytes is not None:
                    result = extract_document_subprocess(
                        stored_path, suffix=suffix,
                        timeout_seconds=(PDF_PARSER_TIMEOUT_SECONDS if suffix.lower() == ".pdf"
                                         else PARSER_TIMEOUT_SECONDS),
                        max_output_bytes=MAX_PARSER_OUTPUT_BYTES,
                    )
                else:
                    if self.isolated:
                        result = extract_document_subprocess(stored_path, suffix=suffix,
                                                            cancel=self._stop, on_progress=self._progress)
                    else:
                        result = extract_document(stored_path, suffix=suffix)
            if self._stop.is_set():
                raise ExtractionError('parser_cancelled')
            self._phase = 'indexing'
            self._progress()
            if not self.derive_candidates:
                return self.repository.complete_intake(
                    job.id, result.fragments, needs_ocr=result.needs_ocr,
                )
            analysis = analyze_document(source.reference, result.fragments)
            financial = map_financial_fragments(source.reference, result.fragments)
            operating = map_operating_fragments(source.reference, result.fragments)
            return self.repository.complete_intake(
                job.id,
                result.fragments,
                needs_ocr=result.needs_ocr,
                native_candidates=analysis.candidates,
                native_financial_candidates=financial.candidates,
                native_operating_candidates=operating.candidates,
                native_topic=analysis.primary_topic,
                native_topic_basis=analysis.topic_basis,
            )
        except ExtractionError as exc:
            return self.repository.fail_intake(job.id, error_code=exc.error_code)
        except Exception:
            _log.exception("intake_worker_internal_error", extra={"job_id": job.id})
            return self.repository.fail_intake(job.id, error_code="internal_error")

    def _run(self) -> None:
        self.repository.recover_interrupted_jobs()
        while not self._stop.is_set():
            try:
                processed = self.process_one()
            except Exception:
                _log.exception("intake_worker_loop_error")
                processed = None
            if processed is None:
                self._stop.wait(0.5)

    def runtime_state(self) -> str:
        """Process-local liveness only; reading never starts or recovers work."""
        if self._thread is None or not self._thread.is_alive():
            return 'stopped'
        return 'stopping' if self._stop.is_set() else 'running'

    def health(self) -> dict:
        age = max(0, time.monotonic() - self._progress_at) if self._active else None
        stalled = bool(self._active and age >= 900)
        return {'state': 'stalled' if stalled else self.runtime_state(),
                'phase': self._phase, 'active': self._active,
                'progress_age_seconds': round(age, 1) if age is not None else None,
                'restart_required': stalled}

    def request_stop(self) -> None:
        self._stop.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="project-watch-intake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=5)
