"""Consent-bound compiler discovers durable completed intake, never depends on a tab."""

import logging
import threading
from contextlib import closing, suppress
from decimal import Decimal

from project_watch.company_state import (
    READING_OUTPUT_TOKENS,
    READING_RULE_VERSION,
    CompanyState,
    _current_packet,
    input_packets,
    packet_digest,
)
from project_watch.reading_jobs import ReadingDenied

_log = logging.getLogger(__name__)


class ReadingWorker:
    def __init__(self, repository, ledger, settings):
        self.repo, self.ledger, self.settings = repository, ledger, settings
        self.company = CompanyState(repository)
        self._stop = threading.Event()
        self._thread = None
        self.external_worker = None
        self.monitor_autopilot = None

    def process_one(self):
        self.ledger.recover()
        model = self.settings.get()
        if model is None or not hasattr(model, "compile_reading") or not hasattr(model, "authorization_id"):
            return None
        if model.disclosure().get("remaining_runs", 0) <= 0:
            return None
        config_id = model.authorization_id()
        with closing(self.repo._connect()) as db:
            grant_ids = [r[0] for r in db.execute("""SELECT id FROM reading_authorizations
                WHERE status='active' AND purpose='company_reading' AND config_id=? AND rule_version=? ORDER BY created_at DESC,id""",
                (config_id, READING_RULE_VERSION))]
        for grant_id in grant_ids:
            grant = self.ledger.status(grant_id)
            limits = grant["limits"]
            if (grant["requests_used"] >= limits["max_requests"]
                    or limits["max_output_tokens"] < READING_OUTPUT_TOKENS
                    or (limits["max_cost_cny"] is not None and Decimal(grant["reserved_cny"]) >= Decimal(limits["max_cost_cny"]))):
                continue
            readable = {j.source_id for j in self.repo.list_intake(grant["project_id"])
                        if j.status in {"completed", "needs_ocr"}}
            for source in self.repo.list_sources(grant["project_id"]):
                if (source.id not in readable or source.kind != "document"
                        or (not grant["include_future"] and source.content_sha256 not in grant["source_hashes"])):
                    continue
                for packet in input_packets(self.repo, source):
                    if self._stop.is_set():
                        return None
                    try:
                        job = self.ledger.enqueue(grant_id, sources={source.id: source.content_sha256},
                                                  input_digest=packet_digest(packet))
                        claimed = self.ledger.claim(job["id"])
                    except ReadingDenied:
                        continue
                    if claimed is None:
                        continue
                    token = claimed["lease_token"]
                    reserve = self.ledger.before_send(job["id"], token, config_id=config_id)

                    def before_send(body, packet=packet, reserve=reserve):
                        selected = self.settings.get()
                        if (self._stop.is_set() or selected is None or not hasattr(selected, "authorization_id")
                                or selected.authorization_id() != config_id):
                            raise ReadingDenied("active receiver changed")
                        with closing(self.repo._connect()) as db:
                            if not _current_packet(db, packet):
                                raise ReadingDenied("material changed before egress")
                        reserve(body)

                    try:
                        output = model.compile_reading(packet, before_send=before_send)
                        self.company.publish(job["id"], token, packet, output)
                    except Exception:
                        # No raw model text, exception details, prompts or keys in logs.
                        if self.ledger.get(job["id"])["state"] == "running":
                            self.ledger.release_unsent(job["id"], token)
                        else:
                            with suppress(ReadingDenied):
                                self.ledger.finish(job["id"], token, succeeded=False)
                        _log.warning("reading_compile_not_completed", extra={"job_id": job["id"]})
                    return self.ledger.get(job["id"])
        return None

    def status(self, project_id):
        """Read-only status for a quiet reading indicator, never dispatch on GET."""
        model = self.settings.get()
        config_id = model.authorization_id() if model and hasattr(model, "authorization_id") else None
        grants = [g for g in self.ledger.list_authorizations(project_id) if g['purpose'] == 'company_reading']
        matching = [g for g in grants if g["config_id"] == config_id and g["rule_version"] == READING_RULE_VERSION]
        active = [g for g in matching if g["status"] == "active"]
        usable = [g for g in active if g["requests_used"] < g["limits"]["max_requests"]
                  and (g["limits"]["max_cost_cny"] is None
                       or Decimal(g["reserved_cny"]) < Decimal(g["limits"]["max_cost_cny"]))]
        reason = "ready"
        if config_id is None:
            reason = "model_missing"
        elif not matching:
            reason = "consent_required"
        elif not active:
            reason = "paused"
        elif not usable:
            reason = "budget_reached"
        elif model.disclosure().get("remaining_runs", 0) <= 0:
            reason = "model_limit"
        with closing(self.repo._connect()) as db:
            counts = dict(db.execute("""SELECT j.state,count(*) FROM reading_jobs j
                JOIN reading_authorizations a ON a.id=j.authorization_id
                WHERE j.project_id=? AND a.purpose='company_reading' GROUP BY j.state""", (project_id,)))
        return {"reason": reason, "poll": reason == "ready", "counts": counts}

    def _run(self):
        while not self._stop.is_set():
            try:
                processed = self.monitor_autopilot.process_one() if self.monitor_autopilot is not None else None
                if processed is None:
                    processed = self.process_one()
                if processed is None and self.external_worker is not None and not self._stop.is_set():
                    processed = self.external_worker.process_one()
            except Exception:
                _log.warning("reading_worker_unavailable")
                processed = None
            # Scan persisted intake completions; restart recovers missed arrivals.
            self._stop.wait(0.2 if processed else 3)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if self.external_worker is not None:
            self.external_worker.start()
        self._thread = threading.Thread(target=self._run, name="project-watch-reading", daemon=True)
        self._thread.start()

    def request_stop(self):
        self._stop.set()
        if self.external_worker is not None:
            self.external_worker.stop()

    def stop(self):
        self.request_stop()
        if self._thread:
            self._thread.join(timeout=5)
