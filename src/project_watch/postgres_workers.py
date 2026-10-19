"""Single-process PostgreSQL intake and monitoring workers.

Start only after explicit schema verification. Interrupted intake recovery is
deliberately manual because its v1 queue has no automatic lease.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from project_watch.analysis.financial_mapping import map_financial_fragments
from project_watch.analysis.operating_mapping import map_operating_fragments
from project_watch.blob_store import BlobStore
from project_watch.domain import IntakeJob, MonitoringJob
from project_watch.extraction_process import extract_document_subprocess
from project_watch.extractors import ExtractionError
from project_watch.monitoring import (
    AdapterResult,
    MonitoringAdapter,
    items_to_signals,
    select_project_items,
)
from project_watch.native_intake import analyze_document
from project_watch.parser_container import (
    extract_document_container,
    validate_parser_docker_host,
    validate_parser_image,
)
from project_watch.postgres_core import PostgresCoreRepository
from project_watch.repository import IntakeConflictError

_log = logging.getLogger(__name__)


class PostgresIntakeWorker:
    def __init__(
        self,
        repository: PostgresCoreRepository,
        blob_store: BlobStore,
        *,
        parser_image: str | None = None,
        docker_host: str | None = None,
    ) -> None:
        if (parser_image is None) != (docker_host is None):
            raise ValueError("parser image and local Docker host must be configured together")
        self.repository = repository
        self.blob_store = blob_store
        self.parser_image = validate_parser_image(parser_image) if parser_image else None
        self.docker_host = validate_parser_docker_host(docker_host) if docker_host else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def process_one(self) -> IntakeJob | None:
        job = self.repository.claim_next_intake()
        if job is None:
            return None
        source = self.repository.get_source(job.project_id, job.source_id)
        if source is None:
            return self.repository.fail_intake(
                job.project_id,
                job.id,
                expected_attempt=job.attempts,
                error_code="source_missing",
            )
        try:
            path = self.blob_store.path_for_hash(source.content_sha256)
            suffix = Path(source.reference).suffix
            if self.parser_image is not None and self.docker_host is not None:
                result = extract_document_container(
                    path, suffix=suffix, image=self.parser_image, docker_host=self.docker_host
                )
            else:
                result = extract_document_subprocess(path, suffix=suffix)
            analysis = analyze_document(source.reference, result.fragments)
            financial = map_financial_fragments(source.reference, result.fragments)
            operating = map_operating_fragments(source.reference, result.fragments)
            return self.repository.complete_intake(
                job.project_id,
                job.id,
                result.fragments,
                expected_attempt=job.attempts,
                needs_ocr=result.needs_ocr,
                native_candidates=analysis.candidates,
                native_financial_candidates=financial.candidates,
                native_operating_candidates=operating.candidates,
                native_topic=analysis.primary_topic,
                native_topic_basis=analysis.topic_basis,
            )
        except ExtractionError as exc:
            return self.repository.fail_intake(
                job.project_id,
                job.id,
                expected_attempt=job.attempts,
                error_code=exc.error_code,
            )
        except IntakeConflictError:
            # An operator may have recovered the job after the old worker stopped.
            return None
        except Exception:
            _log.exception("postgres_intake_worker_error", extra={"job_id": job.id})
            try:
                return self.repository.fail_intake(
                    job.project_id,
                    job.id,
                    expected_attempt=job.attempts,
                    error_code="internal_error",
                )
            except IntakeConflictError:
                return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = self.process_one()
            except Exception:
                _log.exception("postgres_intake_worker_loop_error")
                processed = None
            if processed is None:
                self._stop.wait(0.5)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="project-watch-pg-intake", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


class PostgresMonitoringScheduler:
    def __init__(
        self, repository: PostgresCoreRepository, adapters: dict[str, MonitoringAdapter]
    ) -> None:
        self.repository = repository
        self.adapters = adapters
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def process_one(self, *, now: datetime | None = None) -> MonitoringJob | None:
        job = self.repository.claim_next_monitoring_job(now=now)
        if job is None:
            return None
        try:
            fetched_count = 0
            plan = self.repository.get_monitoring_plan(job.project_id, job.plan_id)
            if plan is None or (job.trigger == "scheduled" and not plan.enabled):
                status, error_code, signals = "not_run", None, ()
            else:
                adapter = self.adapters.get(plan.kind)
                if adapter is None:
                    status, error_code, signals = "not_run", None, ()
                else:
                    try:
                        result = adapter.fetch(plan.target)
                    except Exception:
                        result = AdapterResult(unavailable_error="adapter_error")
                    if result.unavailable_error:
                        status, error_code, signals = (
                            "rate_limited"
                            if result.unavailable_error in {"rate_limited", "quota_exhausted"}
                            else "unreachable",
                            result.unavailable_error,
                            (),
                        )
                    elif not result.items:
                        status, error_code, signals = "no_results", None, ()
                    else:
                        fetched_count = len(result.items)
                        project = self.repository.get_project(job.project_id)
                        status, error_code, signals = (
                            "completed",
                            None,
                            items_to_signals(select_project_items(result.items, project.name)),
                        )
            finished, _receipt = self.repository.finish_monitoring_job(
                job.project_id,
                job.id,
                expected_attempt=job.attempts,
                status=status,
                error_code=error_code,
                signals=signals,
                fetched_count=fetched_count,
            )
            return finished
        except IntakeConflictError:
            return None
        except Exception:
            _log.exception("postgres_monitoring_worker_error", extra={"job_id": job.id})
            try:
                return self.repository.fail_monitoring_job(
                    job.project_id,
                    job.id,
                    expected_attempt=job.attempts,
                    error_code="internal_error",
                )
            except IntakeConflictError:
                return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.repository.schedule_due_monitoring()
                processed = self.process_one()
            except Exception:
                _log.exception("postgres_monitoring_scheduler_loop_error")
                processed = None
            if processed is None:
                self._stop.wait(2)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="project-watch-pg-monitoring", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=7)
