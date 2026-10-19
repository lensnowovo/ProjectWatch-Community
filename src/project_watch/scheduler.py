"""Single-node durable monitoring queue and scheduled-run worker."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from project_watch.domain import MonitoringJob
from project_watch.monitoring import MonitoringRunner
from project_watch.repository import ProjectRepository

_log = logging.getLogger(__name__)


class MonitoringScheduler:
    def __init__(self, repository: ProjectRepository, runner: MonitoringRunner) -> None:
        self.repository = repository
        self.runner = runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def process_one(self, *, now: datetime | None = None) -> MonitoringJob | None:
        job = self.repository.claim_next_monitoring_job(now=now)
        if job is None:
            return None
        try:
            plan = self.repository.get_monitoring_plan(job.plan_id)
            if plan is None:
                return self.repository.fail_monitoring_job(job.id, error_code="plan_missing")
            if job.trigger == "scheduled" and not plan.enabled:
                run = self.repository.record_source_run(
                    job.plan_id,
                    status="not_run",
                    error_code=None,
                    idempotency_key=job.idempotency_key,
                )
            else:
                run = self.runner.run(job.plan_id, idempotency_key=job.idempotency_key)
            return self.repository.complete_monitoring_job(job.id, run_id=run.id)
        except Exception:
            _log.exception("monitoring_job_internal_error", extra={"job_id": job.id})
            return self.repository.fail_monitoring_job(job.id, error_code="internal_error")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.repository.schedule_due_monitoring()
                processed = self.process_one()
            except Exception:
                _log.exception("monitoring_scheduler_loop_error")
                processed = None
            if processed is None:
                self._stop.wait(2)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="project-watch-monitoring", daemon=True
        )
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=7)
