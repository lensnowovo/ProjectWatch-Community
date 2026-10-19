"""Local durable reading consent and at-most-once model dispatch reservations.

No network, prompt storage, business fact writes or implicit authorizations here.
The worker must bind its exact input to input_digest; the proxy reserves before
egress. A lost response is uncertain, never automatically retried or refunded.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal

from project_watch.model_gateway import BudgetGate, BudgetPolicy, GateRejected
from project_watch.model_provider import ModelProviderConfig

PURPOSE = "company_reading"
MAX_ATTEMPTS = 3
LEASE_SECONDS = 300  # Longer than the bounded 90-second DSH turn.


class ReadingDenied(GateRejected):
    """Fail closed without exposing material or provider credentials."""


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ReadingDenied("invalid version digest")
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ReadingLimits:
    max_requests: int = 20
    max_request_bytes: int = 150_000
    max_output_tokens: int = 768
    max_cost_cny: str | None = None
    input_cny_per_million: str | None = None
    output_cny_per_million: str | None = None

    def __post_init__(self):
        for value, ceiling in ((self.max_requests, 1000), (self.max_request_bytes, 500_000),
                               (self.max_output_tokens, 8192)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid reading limit")
        self.policy("validation")

    def policy(self, model):
        try:
            prices = tuple(Decimal(v) if v is not None else None for v in
                           (self.max_cost_cny, self.input_cny_per_million, self.output_cny_per_million))
            return BudgetPolicy(model, self.max_requests, self.max_request_bytes,
                                self.max_output_tokens, *prices)
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ValueError("invalid reading budget") from exc


def ensure_reading_schema(db):
    """Additive local SQLite upgrade; no change to Postgres/team schema."""
    db.execute("""CREATE TABLE IF NOT EXISTS reading_authorizations (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        config_id TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL,
        rule_version TEXT NOT NULL, source_hashes_json TEXT NOT NULL,
        include_future INTEGER NOT NULL CHECK (include_future IN (0,1)),
        limits_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','paused','revoked')),
        actor TEXT NOT NULL, created_at REAL NOT NULL, recipient_json TEXT NOT NULL,
        request_key TEXT NOT NULL, UNIQUE(project_id,request_key)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS reading_authorization_events (
        id INTEGER PRIMARY KEY, authorization_id TEXT NOT NULL REFERENCES reading_authorizations(id),
        status TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS reading_jobs (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        authorization_id TEXT NOT NULL REFERENCES reading_authorizations(id),
        job_key TEXT NOT NULL UNIQUE, sources_json TEXT NOT NULL, input_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','running','dispatched','succeeded','failed','uncertain')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
        lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS reading_dispatches (
        job_id TEXT PRIMARY KEY REFERENCES reading_jobs(id),
        authorization_id TEXT NOT NULL REFERENCES reading_authorizations(id),
        request_bytes INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        reserved_cny TEXT NOT NULL, dispatched_at REAL NOT NULL
    )""")
    for table in ("reading_authorization_events", "reading_dispatches"):
        for op in ("UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()}
                BEFORE {op} ON {table}
                BEGIN SELECT RAISE(ABORT, 'reading ledger is append only'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS reading_authorizations_immutable_scope
        BEFORE UPDATE OF project_id,config_id,model,purpose,rule_version,source_hashes_json,
                         include_future,limits_json,actor,created_at,recipient_json,request_key ON reading_authorizations
        BEGIN SELECT RAISE(ABORT, 'create a new authorization to change scope'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS reading_authorizations_no_reactivation
        BEFORE UPDATE OF status ON reading_authorizations WHEN OLD.status='revoked'
        BEGIN SELECT RAISE(ABORT, 'revoked authorization is final'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS reading_jobs_immutable_input
        BEFORE UPDATE OF project_id,job_key,sources_json,input_digest,created_at ON reading_jobs
        BEGIN SELECT RAISE(ABORT, 'reading input is immutable'); END""")
    db.execute("CREATE INDEX IF NOT EXISTS reading_jobs_pending ON reading_jobs(state,created_at)")


class ReadingJobs:
    def __init__(self, repository):
        self.repository = repository
        # Explicitly local-only. Team repository does not expose this connection.
        with closing(repository._connect()) as db, db:
            ensure_reading_schema(db)

    @staticmethod
    def _grant(db, grant_id, *, active=True):
        row = db.execute("SELECT * FROM reading_authorizations WHERE id=?", (grant_id,)).fetchone()
        if row is None or (active and row["status"] != "active"):
            raise ReadingDenied("reading authorization is not active")
        return dict(row)

    @staticmethod
    def _sources(db, grant, sources):
        if not isinstance(sources, dict) or not 1 <= len(sources) <= 1000:
            raise ReadingDenied("invalid material scope")
        allowed = set(json.loads(grant["source_hashes_json"]))
        for source_id, sha in sources.items():
            _digest(sha)
            source = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if (source is None or source["project_id"] != grant["project_id"]
                    or source["kind"] != "document" or source["content_sha256"] != sha
                    or (not grant["include_future"] and sha not in allowed)):
                raise ReadingDenied("material scope or version changed")

    def authorize(self, project_id, *, config_id, model, rule_version, source_hashes,
                  include_future, limits, actor, recipient, request_key=None, purpose=PURPOSE):
        if purpose not in {PURPOSE, 'monitor_strategy'}:
            raise ReadingDenied('unsupported authorization purpose')
        _digest(config_id)
        if (type(include_future) is not bool or not isinstance(source_hashes, list)
                or len(source_hashes) > 1000 or (not source_hashes and not include_future)
                or not isinstance(limits, ReadingLimits)):
            raise ReadingDenied("explicit material scope and limits required")
        for value, length in ((model, 160), (rule_version, 100), (actor, 128)):
            if not isinstance(value, str) or not value.strip() or len(value) > length:
                raise ReadingDenied("invalid authorization identity")
        hashes = sorted({_digest(v) for v in source_hashes})
        config = ModelProviderConfig(**recipient)
        if config.model != model:
            raise ReadingDenied("recipient model mismatch")
        request_key = str(uuid.uuid4()) if request_key is None else request_key
        if not isinstance(request_key, str) or not 16 <= len(request_key) <= 100:
            raise ReadingDenied("invalid consent request key")
        grant_id, now = str(uuid.uuid4()), time.time()
        with closing(self.repository._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise ReadingDenied("project not found")
            known = {r[0] for r in db.execute(
                "SELECT content_sha256 FROM sources WHERE project_id=? AND kind='document'", (project_id,))}
            if not set(hashes) <= known:
                raise ReadingDenied("material scope not in project")
            snapshot = dict(config_id=config_id, model=model, purpose=purpose, rule_version=rule_version,
                            source_hashes_json=_json(hashes), include_future=int(include_future),
                            limits_json=_json(asdict(limits)), actor=actor, recipient_json=_json(asdict(config)))
            existing = db.execute("SELECT * FROM reading_authorizations WHERE project_id=? AND request_key=?",
                                  (project_id, request_key)).fetchone()
            if existing:
                if any(existing[k] != v for k, v in snapshot.items()):
                    raise ReadingDenied("consent request key already used with different scope")
                grant_id = existing["id"]  # Never reset status or counters on a retried POST.
            else:
                db.execute("""INSERT INTO reading_authorizations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (grant_id, project_id, config_id, model, purpose, rule_version, _json(hashes),
                            int(include_future), _json(asdict(limits)), "active", actor, now, _json(asdict(config)), request_key))
                db.execute("""INSERT INTO reading_authorization_events
                              (authorization_id,status,actor,created_at) VALUES(?,?,?,?)""",
                           (grant_id, "active", actor, now))
        return self.status(grant_id)

    def set_status(self, grant_id, status, *, actor):
        if status not in {"active", "paused", "revoked"} or not actor or len(actor) > 128:
            raise ReadingDenied("invalid authorization change")
        with closing(self.repository._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            grant = self._grant(db, grant_id, active=False)
            if grant["status"] == "revoked":
                raise ReadingDenied("authorization revoked")
            if grant["status"] == status:
                return
            db.execute("UPDATE reading_authorizations SET status=? WHERE id=?", (status, grant_id))
            db.execute("""INSERT INTO reading_authorization_events
                          (authorization_id,status,actor,created_at) VALUES(?,?,?,?)""",
                       (grant_id, status, actor, time.time()))

    def status(self, grant_id):
        with closing(self.repository._connect()) as db:
            grant = self._grant(db, grant_id, active=False)
            rows = db.execute("SELECT reserved_cny FROM reading_dispatches WHERE authorization_id=?",
                              (grant_id,)).fetchall()
        grant["limits"] = json.loads(grant.pop("limits_json"))
        grant["source_hashes"] = json.loads(grant.pop("source_hashes_json"))
        grant["recipient"] = json.loads(grant.pop("recipient_json"))
        grant["include_future"] = bool(grant["include_future"])
        grant["requests_used"] = len(rows)
        grant["reserved_cny"] = str(sum((Decimal(r[0]) for r in rows), Decimal(0)))
        return grant

    def list_authorizations(self, project_id):
        with closing(self.repository._connect()) as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM reading_authorizations WHERE project_id=? ORDER BY created_at DESC,id",
                (project_id,))]
        return [self.status(grant_id) for grant_id in ids]

    def enqueue(self, grant_id, *, sources, input_digest):
        _digest(input_digest)
        with closing(self.repository._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            grant = self._grant(db, grant_id)
            self._sources(db, grant, sources)
            # Excludes grant_id: renewing consent must not rebill an identical job.
            key = hashlib.sha256(_json([grant["project_id"], sources, input_digest, grant["config_id"],
                                       grant["purpose"], grant["rule_version"]]).encode()).hexdigest()
            existing = db.execute("SELECT * FROM reading_jobs WHERE job_key=?", (key,)).fetchone()
            if existing:
                # A new explicit grant may resume a never-sent job, not a charged one.
                if existing["state"] == "queued" and not db.execute(
                    "SELECT 1 FROM reading_dispatches WHERE job_id=?", (existing["id"],)).fetchone():
                    db.execute("UPDATE reading_jobs SET authorization_id=? WHERE id=?",
                               (grant_id, existing["id"]))
                job_id = existing["id"]
            else:
                job_id, now = str(uuid.uuid4()), time.time()
                db.execute("""INSERT INTO reading_jobs
                    (id,project_id,authorization_id,job_key,sources_json,input_digest,state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'queued',?,?)""",
                    (job_id, grant["project_id"], grant_id, key, _json(sources), input_digest, now, now))
        return self.get(job_id)

    def get(self, job_id):
        with closing(self.repository._connect()) as db:
            row = db.execute("SELECT * FROM reading_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ReadingDenied("reading job not found")
        result = dict(row)
        result["sources"] = json.loads(result.pop("sources_json"))
        return result

    def claim(self, job_id, *, now=None):
        now = time.time() if now is None else now
        with closing(self.repository._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reading_jobs WHERE id=?", (job_id,)).fetchone()
            if (row is None or row["state"] != "queued" or row["attempts"] >= MAX_ATTEMPTS
                    or (row["lease_until"] is not None and row["lease_until"] > now)):
                return None
            grant = self._grant(db, row["authorization_id"], active=False)
            if grant["status"] != "active":
                return None
            token = secrets.token_hex(24)
            db.execute("""UPDATE reading_jobs SET state='running',attempts=attempts+1,
                lease_token=?,lease_until=?,updated_at=? WHERE id=?""",
                (token, now + LEASE_SECONDS, now, job_id))
        return self.get(job_id)

    def release_unsent(self, job_id, lease_token):
        """Bounded delayed retry only when no egress reservation exists."""
        with closing(self.repository._connect()) as db, db:
            db.execute("""UPDATE reading_jobs SET
                state=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
                lease_token=NULL,lease_until=?,updated_at=?
                WHERE id=? AND state='running' AND lease_token=?
                AND NOT EXISTS(SELECT 1 FROM reading_dispatches WHERE job_id=reading_jobs.id)""",
                (MAX_ATTEMPTS, time.time() + 15, time.time(), job_id, lease_token))

    def before_send(self, job_id, lease_token, *, config_id, precondition=None):
        """One-use callback for BudgetProxy, called immediately before HTTP egress."""
        def reserve(body):
            with closing(self.repository._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                job = db.execute("SELECT * FROM reading_jobs WHERE id=?", (job_id,)).fetchone()
                if (job is None or job["state"] != "running" or job["lease_token"] != lease_token
                        or job["lease_until"] < time.time()):
                    raise ReadingDenied("reading lease no longer valid")
                grant = self._grant(db, job["authorization_id"])
                if grant["config_id"] != config_id:
                    raise ReadingDenied("model configuration changed")
                self._sources(db, grant, json.loads(job["sources_json"]))
                if precondition is not None:
                    precondition(db)
                limits = ReadingLimits(**json.loads(grant["limits_json"]))
                gate = BudgetGate(limits.policy(grant["model"]))
                try:
                    gate.admit(body)
                except GateRejected as exc:
                    raise ReadingDenied("reading request or budget rejected") from exc
                reserved = Decimal(gate.receipt()["estimated_reserved_cny"] or "0")
                previous = db.execute("SELECT reserved_cny FROM reading_dispatches WHERE authorization_id=?",
                                      (grant["id"],)).fetchall()
                total = sum((Decimal(r[0]) for r in previous), Decimal(0)) + reserved
                if (len(previous) >= limits.max_requests or
                        (limits.max_cost_cny is not None and total > Decimal(limits.max_cost_cny))):
                    raise ReadingDenied("reading budget exhausted")
                db.execute("INSERT INTO reading_dispatches VALUES(?,?,?,?,?,?)",
                           (job_id, grant["id"], len(body), json.loads(body)["max_tokens"], str(reserved), time.time()))
                db.execute("UPDATE reading_jobs SET state='dispatched',updated_at=? WHERE id=?",
                           (time.time(), job_id))
        return reserve

    def finish(self, job_id, lease_token, *, succeeded):
        if type(succeeded) is not bool:
            raise ReadingDenied("invalid completion")
        with closing(self.repository._connect()) as db, db:
            # Pre-send failures are known not to incur provider usage. Dispatched
            # failures remain uncertain and keep their reservation, even on HTTP errors.
            row = db.execute("SELECT * FROM reading_jobs WHERE id=?", (job_id,)).fetchone()
            if (row is None or row["lease_token"] != lease_token
                    or row["state"] not in {"running", "dispatched"}
                    or (succeeded and row["state"] != "dispatched")):
                raise ReadingDenied("reading job no longer owned")
            state = "succeeded" if succeeded else ("uncertain" if row["state"] == "dispatched" else "failed")
            changed = db.execute("""UPDATE reading_jobs SET state=?,updated_at=?,lease_until=NULL
                WHERE id=? AND state=? AND lease_token=?""",
                (state, time.time(), job_id, row["state"], lease_token)).rowcount
            if changed != 1:
                raise ReadingDenied("reading job no longer owned")

    def recover(self, *, now=None):
        now = time.time() if now is None else now
        with closing(self.repository._connect()) as db, db:
            db.execute("""UPDATE reading_jobs SET state=CASE
                WHEN state='dispatched' THEN 'uncertain'
                WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
                lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE state IN ('running','dispatched') AND lease_until<?""",
                (MAX_ATTEMPTS, now, now))
