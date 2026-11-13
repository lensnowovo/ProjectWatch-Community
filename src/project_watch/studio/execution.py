"""Leased request journal: replay completed calls, hold ambiguous outcomes.

No exactly-once billing guarantee is possible after a provider accepted a call
but its response was lost. Such calls are held, not implicitly sent again.
Only hashes and bounded responses are local; headers/payload/URLs are not saved.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta

from project_watch.feeds import FeedError
from project_watch.studio import dispatch
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.runtime_health import CALL_GRACE_SECONDS, progress
from project_watch.studio.store import now


class JobYield(Exception):
    """A completed durable phase gives the single worker back to the queue."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_job_calls (
    job_id TEXT NOT NULL REFERENCES studio_jobs(id), call_key TEXT NOT NULL,
    kind TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL,
    response_status INTEGER, response_headers TEXT, response_body BLOB,
    created_at TEXT NOT NULL, PRIMARY KEY(job_id,call_key)
);
CREATE TABLE IF NOT EXISTS studio_job_checkpoints (
    job_id TEXT NOT NULL REFERENCES studio_jobs(id), stage TEXT NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY(job_id,stage)
);
CREATE TABLE IF NOT EXISTS studio_job_cache_hits (
    job_id TEXT NOT NULL REFERENCES studio_jobs(id), call_key TEXT NOT NULL,
    hits INTEGER NOT NULL, PRIMARY KEY(job_id,call_key)
);
CREATE TABLE IF NOT EXISTS studio_job_budget_links (
    job_id TEXT NOT NULL REFERENCES studio_jobs(id),
    origin_job_id TEXT NOT NULL REFERENCES studio_jobs(id), call_key TEXT NOT NULL,
    PRIMARY KEY(job_id,origin_job_id,call_key),
    FOREIGN KEY(origin_job_id,call_key) REFERENCES studio_job_calls(job_id,call_key)
);
CREATE TABLE IF NOT EXISTS studio_provider_backoff (
    provider TEXT PRIMARY KEY, retry_after TEXT NOT NULL, status INTEGER NOT NULL
);
"""


def _still_open(db, job_id, call_key):
    """The request is unfinished and its job is live or waiting to resume, not held."""
    return db.execute("SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
                      "WHERE c.job_id=? AND c.call_key=? AND c.status='started' "
                      "AND j.status IN ('running','queued')", (job_id, call_key)).fetchone() is not None


def waiting_on_request(store, job_id):
    """The job lost its lease but its own sent request is still being received."""
    with closing(store._connect()) as db:
        return db.execute("SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
                          "WHERE c.job_id=? AND c.status='started' AND j.status IN ('running','queued') LIMIT 1",
                          (job_id,)).fetchone() is not None


def _diagnostic_stage(prefix, key):
    """A retry under ``<key>:429:N`` keeps its own diagnostic; the first request keeps its old stage name."""
    return prefix + key[:40] + key[64:]


class JobExecution:
    def __init__(self, store, job, *, budgets=None, cancel_requested=None):
        self.store, self.job = store, job
        self.cancel_requested = cancel_requested or (lambda: False)
        # The legacy argument is accepted for callers, but values never limit work.
        self.budgets = dict.fromkeys(("search", "model", "open_data", "page", "site", "commercial"))
        self.allowed_call_kinds = None  # Capability boundary, independent of accounting.
        self.last_replay = False
        self.slice_started = time.monotonic()
        self.slice_seconds = 120

    @contextmanager
    def call_scope(self, kinds):
        """Restrict capabilities temporarily, without creating a request quota."""
        previous = self.allowed_call_kinds
        self.allowed_call_kinds = frozenset(kinds) if previous is None else previous & frozenset(kinds)
        try:
            yield
        finally:
            self.allowed_call_kinds = previous

    def yield_if_due(self):
        if time.monotonic() - self.slice_started >= self.slice_seconds:
            raise JobYield()

    def _progress(self, db):
        progress(db, self.job['id'])

    def _receipt_guard(self, db):
        # A stop prevents new work, not saving a response already received.
        # Ownership still fences a superseded writer.
        self._lease_guard(db)

    def _owns_transport(self, call_key):
        """Keep receiving while this request is still ours to finish.

        A lease that expired (e.g. while the computer slept) and a job requeued for
        recovery do not abandon a request already sent: nobody else may finish or
        resend a ``started`` row, so cancelling it would only turn a paid answer
        into an unknown. An explicit stop or a watchdog hold still ends the wait.
        """
        if self.cancel_requested():
            return False
        with closing(self.store._connect()) as db:
            return _still_open(db, self.job['id'], call_key)

    def _guard(self, db):
        if self.cancel_requested():
            raise HttpError('request_cancelled')
        self._lease_guard(db)

    def _lease_guard(self, db):
        row = db.execute("SELECT 1 FROM studio_jobs WHERE id=? AND status='running' AND lease_token=? AND lease_until>?",
                         (self.job["id"], self.job["lease_token"], now())).fetchone()
        if not row:
            raise RuntimeError("job lease expired or replaced")

    def checkpoint(self, stage, data):
        encoded = json.dumps(data)
        if not 1 <= len(stage) <= 100 or len(encoded) > 1_000_000:
            raise ValueError("checkpoint exceeds bounds")
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._guard(db)
            old = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                             (self.job['id'], stage)).fetchone()
            if not old or old[0] != encoded:
                self._progress(db)
            db.execute("INSERT INTO studio_job_checkpoints VALUES (?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data",
                       (self.job["id"], stage, encoded))

    def read_checkpoint(self, stage, *, db=None):
        if db is not None:
            return self._read_checkpoint(db, stage)
        with closing(self.store._connect()) as db:
            db.execute("BEGIN")
            return self._read_checkpoint(db, stage)

    def _read_checkpoint(self, db, stage):
        row = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?", (self.job["id"], stage)).fetchone()
        data = json.loads(row[0]) if row else None
        if isinstance(data, dict) and data.get("_chunked_checkpoint") == 1:
            # One read snapshot: a concurrent checkpoint replacement cannot
            # mix a new manifest with old parts (or vice versa).
            rows = db.execute("SELECT stage,data FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE ?",
                              (self.job["id"], data["prefix"] + "%")).fetchall()
            parts = {r[0]: json.loads(r[1]) for r in rows}
            try:
                encoded = "".join(parts[data["prefix"] + str(i)] for i in range(data["count"]))
            except (KeyError, TypeError) as exc:
                raise ValueError("checkpoint parts missing or corrupt") from exc
            if hashlib.sha256(encoded.encode()).hexdigest() != data["sha256"]:
                raise ValueError("checkpoint parts checksum mismatch")
            return json.loads(encoded)
        return data

    def checkpoint_chunked(self, stage, data):
        """Opt-in lossless large snapshot; each row retains the normal bound.

        All parts and manifest replace atomically under the job lease. This is
        durable storage, not permission to send unbounded model context.
        """
        encoded = json.dumps(data, ensure_ascii=False)
        if not 1 <= len(stage) <= 60 or len(encoded) > 16_000_000:
            raise ValueError("chunked checkpoint exceeds bounds")
        prefix = "cpchunk:" + hashlib.sha256(stage.encode()).hexdigest()[:32] + ":"
        parts = [encoded[i:i + 100_000] for i in range(0, len(encoded), 100_000)]
        manifest = json.dumps({"_chunked_checkpoint": 1, "prefix": prefix,
                               "count": len(parts), "sha256": hashlib.sha256(encoded.encode()).hexdigest()})
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._guard(db)
            old = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?",
                             (self.job["id"], stage)).fetchone()
            if old and old[0] == manifest:
                return
            db.execute("DELETE FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE ?",
                       (self.job["id"], prefix + "%"))
            db.executemany("INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
                           [(self.job["id"], prefix + str(i), json.dumps(part, ensure_ascii=False))
                            for i, part in enumerate(parts)])
            db.execute("INSERT INTO studio_job_checkpoints VALUES (?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data",
                       (self.job["id"], stage, manifest))
            self._progress(db)

    def call(self, kind, key, transport, *, provider="", read_only=False, public_read=False, timeout=300):
        if kind not in self.budgets or not key or len(key) > 200:
            raise ValueError("invalid request journal key")
        # Provider values are only hashes, not URLs, keys or private prompts.
        provider_hash = hashlib.sha256(provider.encode()).hexdigest()
        key = hashlib.sha256((kind + ":" + key).encode()).hexdigest()
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._guard(db)
            old = db.execute("SELECT * FROM studio_job_calls WHERE job_id=? AND call_key=?", (self.job["id"], key)).fetchone()
            logical_key = key
            attempt = 0
            while (old and ((old['retryable'] and old['status'] in ('not_sent', 'completed'))
                           or (old['status'] in ('unknown', dispatch.RESENT) and old['retryable'] == dispatch.CUT_RESEND)
                           or (old['status'] == 'completed' and old['response_status'] in (401, 402, 403, 429)))):
                attempt += 1
                key = f'{logical_key}:429:{attempt}'
                old = db.execute('SELECT * FROM studio_job_calls WHERE job_id=? AND call_key=?',
                                 (self.job['id'], key)).fetchone()
            if old:
                self.last_replay = True
                if old["status"] == "failed_read":
                    diagnostic = self._read_checkpoint(db, _diagnostic_stage('call_error:', key)) or {}
                    code = diagnostic.get('code', 'response_too_large')
                    if code not in ('response_too_large', 'public_read_failed'):
                        raise dispatch.NotSent('unknown_not_resent')
                    raise HttpError(code)
                if old["status"] != "completed":
                    raise dispatch.NotSent('unknown_not_resent')  # accounting, not a job-wide hold
                db.execute("INSERT INTO studio_job_cache_hits VALUES (?,?,1) ON CONFLICT(job_id,call_key) DO UPDATE SET hits=hits+1", (self.job["id"], key))
                return Response(old["response_status"], json.loads(old["response_headers"]), old["response_body"])
            # One rule for every job kind, no cross-job holds (see dispatch.py).
            if self.allowed_call_kinds is not None and kind not in self.allowed_call_kinds:
                raise HttpError("call_kind_not_permitted")
            dispatch.check(db, self.job, kind, logical_key, provider_hash)
            db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at,usage_version,request_limit) "
                       "VALUES (?,?,?,?,'started',?,1,?)", (self.job["id"], key, kind, provider_hash, now(), None))
            db.execute('UPDATE studio_job_calls SET deadline_at=? WHERE job_id=? AND call_key=?',
                       ('' if timeout is None else
                        (datetime.now(UTC) + timedelta(seconds=max(1, float(timeout)) + CALL_GRACE_SECONDS)).isoformat(),
                        self.job['id'], key))
        self.allowed_call_kinds = None  # Capability boundary, independent of accounting.
        self.last_replay = False
        started = time.monotonic()
        try:
            from project_watch.studio.http import TRANSPORT_OWNER
            owner_token = TRANSPORT_OWNER.set(lambda: self._owns_transport(key))
            try:
                response = transport()
            finally:
                TRANSPORT_OWNER.reset(owner_token)
            if len(response.body) > 8 * 1024 * 1024:
                raise HttpError("response_too_large")
        except Exception as exc:
            # A trusted public read can fail without an uncertain paid/write
            # outcome. HTTP method or a model-supplied URL alone is insufficient.
            transport_failure = (isinstance(exc, HttpError) and exc.code == 'connection_failed'
                                 and exc.failure_kind in ('timeout', 'transport_error')
                                 and exc.transport_phase in ('connect_or_headers', 'response_body'))
            # Guarded public connectors wrap socket/TLS and source-policy
            # failures in FeedError. That typed boundary still represents a
            # failed read; it must not turn an unavailable source into a global
            # hold. The explicit public_read capability below remains required.
            transport_failure = transport_failure or isinstance(exc, (TimeoutError, ConnectionError, FeedError))
            failed_read = (read_only and kind in ("open_data", "page", "site")
                           and ((isinstance(exc, HttpError) and exc.code == "response_too_large")
                                or (public_read and transport_failure)))
            not_sent = isinstance(exc, HttpError) and exc.code == "connection_not_sent"
            failure_code = 'public_read_failed' if transport_failure else 'response_too_large'
            with closing(self.store._connect()) as db, db:
                if failed_read:
                    db.execute("BEGIN IMMEDIATE")
                    self._receipt_guard(db)
                db.execute("UPDATE studio_job_calls SET status=?,elapsed_ms=?,finished_at=? WHERE job_id=? AND call_key=?",
                           ("not_sent" if not_sent else "failed_read" if failed_read else "unknown", int((time.monotonic() - started) * 1000), now(), self.job["id"], key))
                if not_sent:
                    db.execute("UPDATE studio_job_calls SET retryable=1 WHERE job_id=? AND call_key=?", (self.job["id"], key))
                    db.execute("INSERT INTO studio_provider_backoff VALUES (?,?,0) ON CONFLICT(provider) "
                               "DO UPDATE SET retry_after=excluded.retry_after,status=0",
                               (provider_hash, (datetime.now(UTC) + timedelta(seconds=dispatch.retry_delay(0, attempt))).isoformat()))
                phase = getattr(exc, 'transport_phase', 'unknown')
                failure = getattr(exc, 'failure_kind', 'timeout' if isinstance(exc, TimeoutError)
                                  else 'transport_error' if isinstance(exc, ConnectionError) else 'unknown')
                diagnostic = {'call_key': key,
                    'phase': phase if phase in ('connect_or_headers', 'response_body') else 'unknown',
                    'failure': failure if failure in ('timeout', 'transport_error') else 'unknown'}
                from project_watch.studio.http import safe_diagnostics
                diagnostic['transport'] = safe_diagnostics(getattr(exc, 'diagnostics', {}))
                if failed_read:
                    diagnostic['code'] = failure_code
                    diagnostic['policy'] = 'public_read' if public_read else 'oversized_read'
                resend = (kind == 'model' and not not_sent and not failed_read and phase == 'response_body'
                          and dispatch.cut_stream(diagnostic['transport']) and not self.cancel_requested())
                if resend:
                    # The provider took the request and its stream broke off. A model request
                    # changes nothing outside this job, so it is sent once more under a new key;
                    # a second break of the same logical request stays unknown and holds.
                    resend = not db.execute(
                        "SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id WHERE j.project_id=? "
                        "AND (c.call_key=? OR c.call_key GLOB ?||':429:*') AND c.status='unknown' AND c.retryable=? LIMIT 1",
                        (self.job['project_id'], logical_key, logical_key, dispatch.CUT_RESEND)).fetchone()
                    if resend:
                        db.execute("UPDATE studio_job_calls SET retryable=? WHERE job_id=? AND call_key=?",
                                   (dispatch.CUT_RESEND, self.job["id"], key))
                    diagnostic['auto_resend'] = bool(resend)
                # No exception text, URL, credentials or response body in diagnostics.
                db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?) '
                           'ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data',
                           (self.job['id'], _diagnostic_stage('call_error:', key), json.dumps(diagnostic)))
            if failed_read:
                raise HttpError(failure_code) from exc
            if resend:
                raise HttpError('stream_cut_resend', transport_phase='response_body',
                                failure_kind='transport_error', diagnostics=getattr(exc, 'diagnostics', None)) from exc
            raise
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            # A complete answer is recorded even if the lease lapsed meanwhile: the
            # row is still ``started``, so no other attempt could have resent or
            # filled it, and the resumed job replays it instead of paying again.
            # A job the watchdog already held as unknown stays unknown.
            if not _still_open(db, self.job['id'], key):
                raise RuntimeError("job lease expired or replaced")
            self._progress(db)
            safe_headers = {k.lower(): v[:500] for k, v in response.headers.items() if k.lower() in ("content-type", "content-encoding", "etag", "last-modified")}
            if response.diagnostics:
                from project_watch.studio.http import safe_diagnostics
                db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?) '
                           'ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data',
                           (self.job['id'], _diagnostic_stage('call_transport:', key), json.dumps(safe_diagnostics(response.diagnostics))))
            from project_watch.studio.usage import FIELDS, reported

            counts = reported(response) if kind == 'model' else dict.fromkeys(FIELDS)
            db.execute("UPDATE studio_job_calls SET status='completed',response_status=?,response_headers=?,response_body=?,"
                       'input_tokens=?,output_tokens=?,cache_read_tokens=?,cache_creation_tokens=?,elapsed_ms=?,finished_at=? WHERE job_id=? AND call_key=?',
                       (response.status, json.dumps(safe_headers), response.body, *(counts[f] for f in FIELDS),
                        int((time.monotonic() - started) * 1000), now(), self.job["id"], key))
            if key != logical_key:
                # The automatic resend of a cut stream came back: the earlier row stays
                # counted as spent but is no longer an unresolved outcome.
                db.execute("UPDATE studio_job_calls SET status=? WHERE job_id=? AND (call_key=? OR call_key GLOB ?||':429:*') "
                           "AND status='unknown' AND retryable=?",
                           (dispatch.RESENT, self.job['id'], logical_key, logical_key, dispatch.CUT_RESEND))
            if response.status in (401, 402, 403, 429, 500, 502, 503, 504, 529):
                db.execute("UPDATE studio_job_calls SET retryable=1 WHERE job_id=? AND call_key=?", (self.job["id"], key))
                db.execute("INSERT INTO studio_provider_backoff VALUES (?,?,?) ON CONFLICT(provider) DO UPDATE SET retry_after=excluded.retry_after,status=excluded.status",
                    (provider_hash, (datetime.now(UTC) + timedelta(seconds=dispatch.retry_delay(response.status, attempt, response.headers))).isoformat(), response.status))
        with closing(self.store._connect()) as db:
            self._lease_guard(db)  # saved; a superseded owner still stops here
        if self.cancel_requested():
            raise HttpError('request_cancelled')
        return response

    def http(self, original, classify, *, public_read=None):
        def send(method, url, headers, body, timeout):
            kind, provider = classify(url)
            digest = hashlib.sha256(json.dumps([method, url, sorted(headers.items())]).encode() + (body or b"")).hexdigest()
            return self.call(kind, digest, lambda: original.transport(method, url, headers, body, timeout),
                             provider=provider, read_only=method == "GET", timeout=timeout,
                             public_read=bool(public_read and public_read(method, url, headers, body)))
        return Http(send)

    def budget_used(self, kind, *, db=None):
        """Admission consumption, including adopted receipts; not new usage."""
        if db is None:
            with closing(self.store._connect()) as connection:
                return self.budget_used(kind, db=connection)
        return db.execute('SELECT count(*) FROM (SELECT job_id,call_key FROM studio_job_calls WHERE job_id=? AND kind=? '
            'UNION SELECT c.job_id,c.call_key FROM studio_job_budget_links l JOIN studio_job_calls c '
            'ON c.job_id=l.origin_job_id AND c.call_key=l.call_key WHERE l.job_id=? AND c.kind=?)',
            (self.job['id'], kind, self.job['id'], kind)).fetchone()[0]

    def usage(self):
        with closing(self.store._connect()) as db:
            return {r["kind"]: {"requests": r["requests"], "completed": r["completed"], "unknown": r["unknown"], "cache_hits": r["cache_hits"]}
                    for r in db.execute(f"SELECT c.kind,sum(c.status!='not_sent') requests,sum(c.status='completed') completed,sum({dispatch.UNRESOLVED}) unknown,"
                        "sum(COALESCE(h.hits,0)) cache_hits FROM studio_job_calls c LEFT JOIN studio_job_cache_hits h ON h.job_id=c.job_id AND h.call_key=c.call_key "
                        "WHERE c.job_id=? GROUP BY c.kind", (self.job["id"],))}
