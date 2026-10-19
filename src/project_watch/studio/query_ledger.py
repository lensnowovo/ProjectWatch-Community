"""Persistent reuse of completed public searches within one project only.

Provider request journals remain the billing authority. Uses below count cache
operations, not paid HTTP requests. No commercial or model responses enter here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta

from project_watch.studio.http import HttpError
from project_watch.studio.sources import SearchCache

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_query_cache (
    project_id TEXT NOT NULL REFERENCES projects(id), query_key TEXT NOT NULL,
    provider TEXT NOT NULL, account_fingerprint TEXT NOT NULL, query TEXT NOT NULL,
    response_data TEXT NOT NULL, completed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    PRIMARY KEY(project_id,query_key)
);
CREATE TABLE IF NOT EXISTS studio_query_uses (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), query_key TEXT NOT NULL,
    purpose TEXT NOT NULL, job_id TEXT NOT NULL, outcome TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS studio_query_uses_lookup ON studio_query_uses(project_id,query_key,created_at);
"""


class ProjectQueryCache:
    def __init__(self, store, project_id: str, *, purpose: str, job_id: str = "",
                 guard=None, ttl: float = 3600, front: SearchCache | None = None):
        self.store, self.project_id = store, project_id
        self.purpose, self.job_id, self.guard = purpose, job_id, guard
        self.ttl, self.front = ttl, front or SearchCache()

    def _record(self, db, key, outcome):
        db.execute("INSERT INTO studio_query_uses VALUES (?,?,?,?,?,?,?)",
                   (str(uuid.uuid4()), self.project_id, key, self.purpose[:100], self.job_id,
                    outcome, datetime.now(UTC).isoformat()))

    def _check(self, db):
        if self.guard:
            self.guard(db)

    def get(self, signature: tuple, fetch) -> tuple[list[dict], bool]:
        if (len(signature) != 5 or signature[0] not in ("bocha", "tavily", "brave")):
            raise ValueError("only public search signatures are accepted")
        key = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()
        # Share process locks but never share results between different projects.
        lock = self.front.locks[hash((self.project_id, key)) % len(self.front.locks)]
        with lock:
            with closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._check(db)
                row = db.execute("SELECT * FROM studio_query_cache WHERE project_id=? AND query_key=?",
                                 (self.project_id, key)).fetchone()
                if self.ttl > 0 and row and row["expires_at"] > datetime.now(UTC).isoformat():
                    data = json.loads(row["response_data"])
                    self._record(db, key, "cached")
                    return data, True
                previous = db.execute("SELECT outcome FROM studio_query_uses WHERE project_id=? AND query_key=? "
                                      "ORDER BY created_at DESC,rowid DESC LIMIT 1", (self.project_id, key)).fetchone()
                if previous and previous["outcome"] in ("unknown", "held"):
                    unknown = db.execute("SELECT id FROM studio_query_uses WHERE project_id=? AND query_key=? AND outcome='unknown' "
                                         'ORDER BY rowid DESC LIMIT 1', (self.project_id, key)).fetchone()
                    authorized = unknown and db.execute('SELECT 1 FROM studio_query_recoveries WHERE project_id=? '
                        'AND job_id=? AND query_key=? AND unknown_use_id=?',
                        (self.project_id, self.job_id, key, unknown['id'])).fetchone()
                    if not authorized:
                        self._record(db, key, "held")
                        raise HttpError("request_outcome_unknown")
            try:
                data = fetch()
                if not isinstance(data, list) or any(not isinstance(h, dict) for h in data):
                    raise HttpError("invalid_search_response")
                encoded = json.dumps(data, ensure_ascii=False)
                if len(encoded.encode()) > 2_000_000:
                    raise HttpError("response_too_large")
            except BaseException as exc:
                unknown = (not isinstance(exc, HttpError) or exc.code in
                           ("request_outcome_unknown", "connection_failed"))
                with closing(self.store._connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    self._check(db)
                    self._record(db, key, "unknown" if unknown else "failed")
                raise
            with closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._check(db)
                completed = datetime.now(UTC)
                db.execute("INSERT INTO studio_query_cache VALUES (?,?,?,?,?,?,?,?) "
                           "ON CONFLICT(project_id,query_key) DO UPDATE SET response_data=excluded.response_data,"
                           "completed_at=excluded.completed_at,expires_at=excluded.expires_at",
                           (self.project_id, key, signature[0], signature[1], signature[2], encoded,
                            completed.isoformat(), (completed + timedelta(seconds=self.ttl)).isoformat()))
                self._record(db, key, "completed")
            return copy.deepcopy(data), False
