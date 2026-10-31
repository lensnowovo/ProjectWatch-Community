"""Explicit, disabled-by-default public metadata grant on an existing journal.

No scheduler/API route, new job, model, material admission or recovery slot.
The caller must review the exact plan and supply its live runtime/pause guard.
One persistent project-wide START is never reusable, including after failure.
"""

import hashlib
import json
import uuid
from contextlib import closing, nullcontext, suppress
from datetime import UTC, datetime

from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.collection_pages import openalex_page, public_read_request
from project_watch.studio.europepmc import page as europepmc_page
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, urllib_transport
from project_watch.studio.recovery import UNRESOLVED
from project_watch.studio.store import now

START = "public_source_once_v1"
END = "public_source_once_result_v1"
ADAPTERS = ("openalex_affiliation", "europepmc_affiliation", "europepmc_patent")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _starts(db, pid):
    return db.execute(
        "SELECT c.job_id,c.data FROM studio_job_checkpoints c JOIN studio_jobs j ON j.id=c.job_id "
        "WHERE j.project_id=? AND c.stage=?",
        (pid, START),
    ).fetchall()


def guard_other_execution(db, pid):
    for row in _starts(db, pid):
        if not db.execute(
            "SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage=?", (row["job_id"], END)
        ).fetchone():
            raise HttpError("independent_source_active")


def _scope(db, job_id, allowed_calls=()):
    job = db.execute("SELECT * FROM studio_jobs WHERE id=?", (job_id,)).fetchone()
    if (
        not job
        or job["kind"] not in ("external", "external_followup")
        or job["status"] not in ("done", "failed")
    ):
        raise ValueError("terminal external original job required")
    pid = job["project_id"]
    row = db.execute(
        "SELECT legal_name,short_name,aliases,website,config_version,consent_at,status,execution_mode "
        "FROM studio_projects WHERE project_id=?",
        (pid,),
    ).fetchone()
    if (
        not row
        or row["status"] != "active"
        or not row["consent_at"]
        or row["execution_mode"] != "api"
    ):
        raise HttpError("project_paused")
    if db.execute(
        "SELECT 1 FROM studio_jobs WHERE project_id=? AND status IN ('running','queued')", (pid,)
    ).fetchone():
        raise HttpError("project_has_work")
    if db.execute(
        "SELECT 1 FROM studio_provider_backoff WHERE retry_after>? LIMIT 1", (now(),)
    ).fetchone():
        raise HttpError("provider_backoff")
    identity = dict(
        db.execute("SELECT name,identity_status FROM projects WHERE id=?", (pid,)).fetchone()
    )
    decisions = {}
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_collection_source_decisions'"
    ).fetchone():
        decisions = {
            r["adapter"]: json.loads(r["record"])
            for r in db.execute(
                "SELECT adapter,record FROM studio_collection_source_decisions d WHERE project_id=? "
                "AND version=(SELECT max(version) FROM studio_collection_source_decisions h "
                "WHERE h.project_id=d.project_id AND h.adapter=d.adapter) ORDER BY adapter",
                (pid,),
            )
        }
    unresolved = [
        dict(r)
        for r in db.execute(
            f"SELECT c.* FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id WHERE j.project_id=? AND ({UNRESOLVED}) "
            "ORDER BY c.job_id,c.call_key",
            (pid,),
        )
    ]
    unresolved = [
        r
        for r in unresolved
        if not (
            r["job_id"] == job_id and r["status"] == "started" and r["call_key"] in allowed_calls
        )
    ]
    if any(r["kind"] != "model" for r in unresolved):
        raise HttpError("request_outcome_unknown")
    # Existing model uncertainty is frozen, not cleared or borrowed. Persisted
    # holds, prior admission windows and independent analysis are not bypassed.
    holds = [
        list(r)
        for r in db.execute(
            "SELECT c.job_id,c.stage,c.data FROM studio_job_checkpoints c "
            "JOIN studio_jobs j ON j.id=c.job_id WHERE j.project_id=? AND c.stage IN "
            "('dsh_hold','material_round_caps_v1','event_insight_round_v1','material_handoff_v1') ORDER BY c.job_id,c.stage",
            (pid,),
        )
    ]
    holds += [
        list(r)
        for r in db.execute(
            "SELECT c.job_id,c.stage,c.data FROM studio_job_checkpoints c "
            "JOIN studio_jobs j ON j.id=c.job_id WHERE j.project_id=? AND "
            "(c.stage LIKE 'dsh_request:%' OR c.stage LIKE 'dsh_continuation:%') "
            "ORDER BY c.job_id,c.stage",
            (pid,),
        )
    ]
    from project_watch.studio.dsh_closeout import active_hold

    if any(stage == "dsh_hold" and active_hold(json.loads(data)) for _, stage, data in holds):
        raise HttpError("request_outcome_unknown")
    recoveries = [
        list(r)
        for r in db.execute(
            "SELECT r.* FROM studio_call_recoveries r JOIN studio_jobs j "
            "ON j.id=r.job_id WHERE j.project_id=? ORDER BY r.rowid",
            (pid,),
        )
    ]
    # Include all settled and uncertain calls project-wide; response hashes avoid
    # copying raw business material into a grant.
    calls = [
        dict(r)
        for r in db.execute(
            "SELECT c.* FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
            "WHERE j.project_id=? ORDER BY c.job_id,c.call_key",
            (pid,),
        )
    ]
    for call in calls + unresolved:
        call["response_body"] = hashlib.sha256(call["response_body"] or b"").hexdigest()
    return dict(
        job=dict(job),
        project={**dict(row), **identity},
        source_decisions=decisions,
        unknown_sha256=digest(unresolved),
        recoveries_sha256=digest(recoveries),
        restrictions_sha256=digest(holds),
        calls=calls,
    )


def _page(http, source, query):
    if source == "openalex_affiliation":
        result = openalex_page(http, query, mode="affiliation", per_page=20)
    elif source in ADAPTERS:
        result = europepmc_page(http, query, mode=source[10:], per_page=20)
    else:
        raise ValueError("public metadata source not allowed")
    for item in result["items"]:
        extra = item["extra"]
        if source == "openalex_affiliation":
            extra["source_identity"] = "openalex:" + extra["openalex_id"]
        extra["source_version"] = digest(extra["source_record"])
    return result


def _request(source, query):
    captured = []

    class Captured(Exception):
        pass

    def capture(method, url, headers, body, timeout):
        if not public_read_request(method, url, headers, body):
            raise ValueError("fixed public GET required")
        captured.append(dict(method=method, url=url, headers=headers, timeout=timeout))
        raise Captured()

    with suppress(Captured):
        _page(Http(capture), source, query)
    if len(captured) != 1:
        raise ValueError("one public GET required")
    wire = captured[0]
    key = hashlib.sha256(
        json.dumps([wire["method"], wire["url"], sorted(wire["headers"].items())]).encode()
    ).hexdigest()
    return dict(
        source=source,
        query=query,
        wire=wire,
        key=key,
        journal_key=hashlib.sha256(("open_data:" + key).encode()).hexdigest(),
    )


def plan(store, job_id, queries):
    """Read-only; no implicit names, grant, budget, transport or source fetch."""
    if not isinstance(queries, list) or not 1 <= len(queries) <= 4:
        raise ValueError("one to four explicit public queries required")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        scope = _scope(db, job_id)
        if _starts(db, scope["job"]["project_id"]):
            raise ValueError("project source grant already consumed")
        project = scope["project"]
        names = {
            project["legal_name"],
            project["short_name"],
            *json.loads(project["aliases"] or "[]"),
        }
        requests = []
        for query in queries:
            if (
                set(query) != {"source", "query"}
                or not query["query"]
                or query["query"] not in names
            ):
                raise ValueError("confirmed exact public name required")
            decision = scope["source_decisions"].get(query["source"])
            if decision and decision["selection"] != "enabled":
                raise ValueError("source excluded by owner decision")
            requests.append(_request(**query))
        if len({r["key"] for r in requests}) != len(requests):
            raise ValueError("duplicate query")
        if any(c["call_key"] in {r["journal_key"] for r in requests} for c in scope["calls"]):
            raise ValueError("request previously attempted in this project")
        # JobExecution supports explicit recovery aliases. This independent
        # path must never borrow or consume one, even before a call row exists.
        pid = scope["job"]["project_id"]
        for request in requests:
            key = request["journal_key"]
            if (
                db.execute(
                    "SELECT 1 FROM studio_call_recoveries r JOIN studio_jobs j ON j.id=r.job_id "
                    "WHERE j.project_id=? AND (r.base_key=? OR r.next_key=?) LIMIT 1",
                    (pid, key, key),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM studio_job_checkpoints c JOIN studio_jobs j ON j.id=c.job_id "
                    "WHERE j.project_id=? AND c.stage IN (?,?) LIMIT 1",
                    (pid, "dsh_request:" + key, "dsh_continuation:" + key),
                ).fetchone()
            ):
                raise ValueError("source query has a recovery identity")
        used = sum(c["kind"] == "open_data" for c in scope["calls"])
        return dict(
            version=1,
            scope=scope,
            requests=requests,
            maximum_new_open_data=len(requests),
            project_open_data_ceiling=used + len(requests),
            model=0,
            search=0,
            page=0,
            site=0,
            material_admission=False,
            downstream=False,
        )


class _SourceExecution(JobExecution):
    def __init__(self, store, frozen, token, deadline, runtime_guard):
        super().__init__(
            store,
            frozen["scope"]["job"],
            budgets=dict(
                model=0, search=0, page=0, site=0, open_data=frozen["project_open_data_ceiling"]
            ),
        )
        self.frozen, self.token, self.deadline = frozen, token, deadline
        self.allowed_call_kinds = frozenset({"open_data"})
        self.runtime_guard = runtime_guard

    def _progress(self, db):
        pass  # Preserve the terminal job row; this grant has its own receipts.

    def _receipt_guard(self, db):
        # An arrived public reply is still a settled receipt after a pause or
        # scope change. This grants no ingestion; that rechecks the full guard.
        start = self.read_checkpoint(START, db=db)
        if (
            not start
            or start.get("token") != self.token
            or self.read_checkpoint(END, db=db) is not None
        ):
            raise HttpError("independent_source_closed")

    def _guard(self, db):
        self.runtime_guard()
        if datetime.now(UTC) >= self.deadline:
            raise HttpError("independent_source_expired")
        self._receipt_guard(db)
        current = _scope(db, self.job["id"], {r["journal_key"] for r in self.frozen["requests"]})
        original = self.frozen["scope"]
        if any(
            current[k] != original[k]
            for k in (
                "job",
                "project",
                "unknown_sha256",
                "recoveries_sha256",
                "restrictions_sha256",
                "source_decisions",
            )
        ):
            raise HttpError("independent_source_changed")
        old = {(c["job_id"], c["call_key"]): c for c in original["calls"]}
        new = {(c["job_id"], c["call_key"]): c for c in current["calls"]}
        if any(new.get(k) != c for k, c in old.items()):
            raise HttpError("independent_source_changed")
        allowed = {(self.job["id"], r["journal_key"]) for r in self.frozen["requests"]}
        if set(new) - set(old) - allowed:
            raise HttpError("independent_source_changed")

    def budget_used(self, kind, *, db=None):
        with closing(self.store._connect()) if db is None else nullcontext(db) as connection:
            return connection.execute(
                "SELECT count(*) FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
                "WHERE j.project_id=? AND c.kind=?",
                (self.job["project_id"], kind),
            ).fetchone()[0]


def run(store, frozen, grant, *, runtime_guard, transport=None, enabled=False):
    """A reviewed host supplies its drain/pause guard and explicit enablement.

    Transport must refuse redirects, retries and methods other than the exact
    frozen request. The live host must also supply its drain/pause check. Neither
    is created by importing this module or by ordinary workers.
    """
    if enabled is not True or not callable(runtime_guard):
        raise ValueError("independent public source path disabled")
    current_plan = plan(
        store,
        frozen["scope"]["job"]["id"],
        [{k: r[k] for k in ("source", "query")} for r in frozen["requests"]],
    )
    if current_plan != frozen:
        raise ValueError("source plan changed")
    deadline = datetime.fromisoformat(grant["deadline"])
    if (
        grant.get("approved") is not True
        or grant.get("plan_sha256") != digest(frozen)
        or type(grant.get("maximum_new_open_data")) is not int
        or type(grant.get("project_open_data_ceiling")) is not int
        or grant.get("maximum_new_open_data") != frozen["maximum_new_open_data"]
        or grant.get("project_open_data_ceiling") != frozen["project_open_data_ceiling"]
        or deadline.tzinfo is None
        or not 0 < (deadline - datetime.now(UTC)).total_seconds() <= 3600
    ):
        raise ValueError("exact finite source grant required")
    runtime_guard()
    # Test injection is the same trusted-transport boundary as ordinary Http.
    # The production transport refuses ALL redirects and performs no retries.
    transport = transport or urllib_transport(allow_redirects=False)
    token = uuid.uuid4().hex
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        scope = _scope(db, frozen["scope"]["job"]["id"])
        if scope != frozen["scope"] or _starts(db, scope["job"]["project_id"]):
            raise ValueError("source plan changed or consumed")
        db.execute(
            "INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
            (scope["job"]["id"], START, json.dumps(dict(token=token, grant=grant, plan=frozen))),
        )
    execution = _SourceExecution(store, frozen, token, deadline, runtime_guard)
    outcome = dict(status="failed", saved_count=0, source_calls=0, model_calls=0, sources=[])
    try:
        for request in frozen["requests"]:

            def send(method, url, headers, body, timeout, request=request):
                if (
                    body is not None
                    or dict(method=method, url=url, headers=headers, timeout=timeout)
                    != request["wire"]
                ):
                    raise HttpError("independent_source_request_mismatch")
                return execution.call(
                    "open_data",
                    request["key"],
                    lambda: transport(method, url, headers, body, timeout),
                    provider=request["source"],
                    read_only=True,
                    public_read=True,
                    timeout=timeout,
                )

            page = _page(Http(send), request["source"], request["query"])
            outcome["source_calls"] += 1
            at = now()
            unit = dict(
                id=request["key"],
                category="patent" if request["source"].endswith("_patent") else "paper",
                target_id="project:" + execution.job["project_id"],
                window_end=at,
            )
            result = CollectionIngest(execution, admit_materials=False)(unit, page["items"])
            outcome["saved_count"] += result["saved_count"]
            outcome["sources"].append(
                dict(
                    source=request["source"],
                    saved_count=result["saved_count"],
                    truncated=page["truncated"],
                    total_reported=page["total_reported"],
                    fetched_pages=1,
                )
            )
        outcome["status"] = "completed"
        return outcome
    except Exception as exc:
        outcome["code"] = exc.code if isinstance(exc, HttpError) else type(exc).__name__
        raise
    finally:
        with closing(store._connect()) as db, db:
            # An END records completion/failure, never deletes START or resets a
            # clock. A crash without END stays fenced for explicit inspection.
            keys = {r["journal_key"] for r in frozen["requests"]}
            outcome["source_calls"] = sum(
                r[0] in keys
                for r in db.execute(
                    "SELECT call_key FROM studio_job_calls WHERE job_id=?", (execution.job["id"],)
                )
            )
            db.execute(
                "INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
                (execution.job["id"], END, json.dumps(outcome)),
            )
