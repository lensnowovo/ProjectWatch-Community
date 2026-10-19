"""Leased request journal: replay completed calls, hold ambiguous outcomes.

No exactly-once billing guarantee is possible after a provider accepted a call
but its response was lost. Such calls are held, not implicitly sent again.
Only hashes and bounded responses are local; headers/payload/URLs are not saved.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

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
CREATE TABLE IF NOT EXISTS studio_provider_backoff (
    provider TEXT PRIMARY KEY, retry_after TEXT NOT NULL, status INTEGER NOT NULL
);
"""


class JobExecution:
    def __init__(self, store, job, *, budgets=None):
        self.store, self.job = store, job
        # Model work scales with the authorized material, not an arbitrary
        # per-job count. Explicit acceptance-batch budgets still take precedence.
        self.budgets = {"search": 64, "model": None, "open_data": 128, "page": 30,
                        # Official-site checks: a few candidate homepages, robots files and up to MAX_PAGES pages.
                        "site": 16, **(budgets or {})}
        self.last_replay = False
        self.slice_started = time.monotonic()
        self.slice_seconds = 120

    def yield_if_due(self):
        if time.monotonic() - self.slice_started >= self.slice_seconds:
            raise JobYield()

    def _guard(self, db):
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
                progress(db, self.job['id'])
            db.execute("INSERT INTO studio_job_checkpoints VALUES (?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data",
                       (self.job["id"], stage, encoded))

    def read_checkpoint(self, stage):
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?", (self.job["id"], stage)).fetchone()
            return json.loads(row[0]) if row else None

    def call(self, kind, key, transport, *, provider="", read_only=False, timeout=300):
        if kind not in self.budgets or not key or len(key) > 200:
            raise ValueError("invalid request journal key")
        # Provider values are only hashes, not URLs, keys or private prompts.
        provider_hash = hashlib.sha256(provider.encode()).hexdigest()
        key = hashlib.sha256((kind + ":" + key).encode()).hexdigest()
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._guard(db)
            recovery = db.execute('SELECT next_key FROM studio_call_recoveries WHERE job_id=? AND base_key=? '
                                  'ORDER BY rowid DESC LIMIT 1', (self.job['id'], key)).fetchone()
            if recovery:
                key = recovery['next_key']
            old = db.execute("SELECT * FROM studio_job_calls WHERE job_id=? AND call_key=?", (self.job["id"], key)).fetchone()
            if old:
                self.last_replay = True
                if old["status"] == "failed_read":
                    raise HttpError("response_too_large")
                if old["status"] != "completed":
                    raise HttpError("request_outcome_unknown")
                db.execute("INSERT INTO studio_job_cache_hits VALUES (?,?,1) ON CONFLICT(job_id,call_key) DO UPDATE SET hits=hits+1", (self.job["id"], key))
                return Response(old["response_status"], json.loads(old["response_headers"]), old["response_body"])
            # A pause is a dispatch boundary, not an uncertain transport outcome.
            # Already-received replies above remain replayable. Explicitly
            # uploaded internal materials retain their existing processing path.
            project = db.execute('SELECT status FROM studio_projects WHERE project_id=?',
                                 (self.job['project_id'],)).fetchone()
            if self.job['kind'] != 'internal' and project and project[0] == 'paused':
                raise HttpError('project_paused')
            backoff = db.execute("SELECT retry_after FROM studio_provider_backoff WHERE provider=?", (provider_hash,)).fetchone()
            if backoff and backoff[0] > now():
                raise HttpError("provider_backoff")
            count = db.execute("SELECT count(*) FROM studio_job_calls WHERE job_id=? AND kind=?", (self.job["id"], kind)).fetchone()[0]
            if self.budgets[kind] is not None and count >= self.budgets[kind]:
                raise HttpError("request_budget")
            db.execute("INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at,usage_version,request_limit) VALUES (?,?,?,?,'started',?,1,?)",
                       (self.job["id"], key, kind, provider_hash, now(), self.budgets[kind]))
            db.execute('UPDATE studio_job_calls SET deadline_at=? WHERE job_id=? AND call_key=?',
                       ((datetime.now(UTC) + timedelta(seconds=max(1, float(timeout)) + CALL_GRACE_SECONDS)).isoformat(),
                        self.job['id'], key))
            if recovery:
                db.execute('UPDATE studio_call_recoveries SET consumed_at=? WHERE job_id=? AND next_key=?',
                           (now(), self.job['id'], key))
        self.last_replay = False
        started = time.monotonic()
        try:
            response = transport()
            if len(response.body) > 8 * 1024 * 1024:
                raise HttpError("response_too_large")
        except Exception as exc:
            # A completed oversized GET of public data is a known source failure.
            # Never generalize this to paid model/search calls or uncertain timeouts.
            failed_read = (read_only and kind in ("open_data", "page", "site")
                           and isinstance(exc, HttpError) and exc.code == "response_too_large")
            with closing(self.store._connect()) as db, db:
                if failed_read:
                    db.execute("BEGIN IMMEDIATE")
                    self._guard(db)
                db.execute("UPDATE studio_job_calls SET status=?,elapsed_ms=?,finished_at=? WHERE job_id=? AND call_key=?",
                           ("failed_read" if failed_read else "unknown", int((time.monotonic() - started) * 1000), now(), self.job["id"], key))
            if failed_read:
                raise HttpError("response_too_large") from exc
            raise
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._guard(db)
            progress(db, self.job['id'])
            safe_headers = {k.lower(): v[:500] for k, v in response.headers.items() if k.lower() in ("content-type", "content-encoding", "etag", "last-modified")}
            from project_watch.studio.usage import FIELDS, reported

            counts = reported(response) if kind == 'model' else dict.fromkeys(FIELDS)
            db.execute("UPDATE studio_job_calls SET status='completed',response_status=?,response_headers=?,response_body=?,"
                       'input_tokens=?,output_tokens=?,cache_read_tokens=?,cache_creation_tokens=?,elapsed_ms=?,finished_at=? WHERE job_id=? AND call_key=?',
                       (response.status, json.dumps(safe_headers), response.body, *(counts[f] for f in FIELDS),
                        int((time.monotonic() - started) * 1000), now(), self.job["id"], key))
            if response.status in (401, 403, 429):
                db.execute("INSERT INTO studio_provider_backoff VALUES (?,?,?) ON CONFLICT(provider) DO UPDATE SET retry_after=excluded.retry_after,status=excluded.status",
                    (provider_hash, (datetime.now(UTC) + timedelta(minutes=30 if response.status == 429 else 60)).isoformat(), response.status))
        return response

    def http(self, original, classify):
        def send(method, url, headers, body, timeout):
            kind, provider = classify(url)
            digest = hashlib.sha256(json.dumps([method, url, sorted(headers.items())]).encode() + (body or b"")).hexdigest()
            return self.call(kind, digest, lambda: original.transport(method, url, headers, body, timeout),
                             provider=provider, read_only=method == "GET", timeout=timeout)
        return Http(send)

    def usage(self):
        from project_watch.studio.recovery import UNRESOLVED

        with closing(self.store._connect()) as db:
            return {r["kind"]: {"requests": r["requests"], "completed": r["completed"], "unknown": r["unknown"], "cache_hits": r["cache_hits"]}
                    for r in db.execute(f"SELECT c.kind,count(*) requests,sum(c.status='completed') completed,sum({UNRESOLVED}) unknown,"
                        "sum(COALESCE(h.hits,0)) cache_hits FROM studio_job_calls c LEFT JOIN studio_job_cache_hits h ON h.job_id=c.job_id AND h.call_key=c.call_key "
                        "WHERE c.job_id=? GROUP BY c.kind", (self.job["id"],))}
