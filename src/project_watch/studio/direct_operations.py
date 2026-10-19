"""Durable, owner-only receipts for interactive operations outside the job queue.

Input/configuration hashes, never API keys or raw prompts, identify an operation.
Returned results are private local data. An ambiguous attempt stays held until
the owner explicitly accepts possible duplicate cost; elapsed time is not consent.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import closing
from contextvars import ContextVar
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from project_watch.studio.http import Http, HttpError, Response

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_direct_operations (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
    valid_until TEXT, result_json TEXT, requests INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0, unknown INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS studio_direct_operations_fingerprint
ON studio_direct_operations(fingerprint, created_at);
CREATE TABLE IF NOT EXISTS studio_direct_parents (
    operation_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_direct_calls (
    operation_id TEXT NOT NULL, call_key TEXT NOT NULL, status TEXT NOT NULL,
    response_status INTEGER, response_headers TEXT, response_body BLOB,
    replayed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(operation_id, call_key)
);
"""


class OperationBlocked(RuntimeError):
    def __init__(self, operation_id, status):
        self.operation_id, self.status = operation_id, status
        super().__init__('这项操作仍在处理，请稍后查看。' if status == 'running' else
                         '上次操作未完成，已停止重复请求。请检查服务记录后确认重试；可能重复计费。')


class DirectOperations:
    def __init__(self, store):
        self.store = store
        self.current = ContextVar('projectwatch_direct_operation', default=None)

    def recover_interrupted(self):
        """Only call after obtaining the exclusive runtime lock, never on reads."""
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE studio_direct_operations SET status='unknown',finished_at=?,"
                       "unknown=unknown+pending,pending=0 WHERE status='running'",
                       (datetime.now(UTC).isoformat(),))

    def run(self, kind, inputs, settings, callback, *, ttl_seconds=60, commit=None):
        """Prepare outside a transaction; optional local writes commit with the receipt.

        commit(result, db) must use the provided connection, with no network I/O.
        Unknown preparation never publishes local business changes.
        """
        config = asdict(settings)
        # Unrelated notification/refresh preferences must not bypass a held call.
        config = {k: config[k] for k in ('model', 'search', 'registry', 'proxy_url')}
        fingerprint = hashlib.sha256(json.dumps([kind, inputs, config], sort_keys=True,
                                                ensure_ascii=False).encode()).hexdigest()
        at = datetime.now(UTC)
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM studio_direct_operations WHERE fingerprint=? '
                                  'ORDER BY rowid DESC LIMIT 1', (fingerprint,)).fetchone()
            if previous:
                if previous['status'] == 'done' and previous['valid_until'] > at.isoformat():
                    return json.loads(previous['result_json']), previous['id'], True
                if previous['status'] in ('running', 'unknown', 'failed'):
                    raise OperationBlocked(previous['id'], previous['status'])
                if previous['status'] == 'retry_allowed':
                    db.execute("UPDATE studio_direct_operations SET status='superseded' WHERE id=?", (previous['id'],))
            operation_id = str(uuid.uuid4())
            db.execute('INSERT INTO studio_direct_operations(id,fingerprint,kind,status,created_at) '
                       "VALUES (?,?,?,'running',?)", (operation_id, fingerprint, kind, at.isoformat()))
            if previous and previous['status'] == 'retry_allowed':
                db.execute('INSERT INTO studio_direct_parents VALUES (?,?)', (operation_id, previous['id']))
        token = self.current.set(operation_id)
        try:
            result = callback()
            finished = datetime.now(UTC)
            with closing(self.store._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                state = db.execute('SELECT unknown,pending FROM studio_direct_operations WHERE id=?',
                                   (operation_id,)).fetchone()
                if commit is not None:
                    if state['unknown'] + state['pending']:
                        raise OperationBlocked(operation_id, 'unknown')
                    result = commit(result, db)
                encoded = json.dumps(result, ensure_ascii=False)
                db.execute("UPDATE studio_direct_operations SET status=CASE WHEN unknown+pending>0 THEN 'unknown' ELSE 'done' END,"
                           'finished_at=?,valid_until=?,result_json=? WHERE id=?',
                           (finished.isoformat(), (finished + timedelta(seconds=ttl_seconds)).isoformat(), encoded, operation_id))
        except BaseException as exc:
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE studio_direct_operations SET status=CASE WHEN unknown+pending>0 THEN 'unknown' ELSE 'failed' END,"
                           'finished_at=? WHERE id=?', (datetime.now(UTC).isoformat(), operation_id))
                state = db.execute('SELECT status FROM studio_direct_operations WHERE id=?', (operation_id,)).fetchone()[0]
            if state == 'unknown' and isinstance(exc, Exception):
                raise OperationBlocked(operation_id, state) from exc
            raise
        else:
            return result, operation_id, False
        finally:
            self.current.reset(token)

    def http(self, original, *, is_paid=lambda _url: True):
        operation_id = self.current.get()
        if not operation_id:
            return original

        def send(method, url, headers, body, timeout):
            if not is_paid(url):
                return original.transport(method, url, headers, body, timeout)
            request = json.dumps([method, url, sorted(headers.items())], ensure_ascii=False).encode()
            call_key = hashlib.sha256(request + b'\x00' + (body or b'')).hexdigest()
            with closing(self.store._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT status,unknown,pending FROM studio_direct_operations WHERE id=?', (operation_id,)).fetchone()
                if not row or row['status'] != 'running' or row['pending']:
                    raise HttpError('request_outcome_unknown')
                attempted = db.execute('SELECT status,response_status FROM studio_direct_calls WHERE operation_id=? AND call_key=?',
                                       (operation_id, call_key)).fetchone()
                if attempted and (attempted['status'] in ('started', 'unknown') or (attempted['response_status'] or 0) >= 500):
                    raise HttpError('request_outcome_unknown')
                # Replay only within this operation's explicitly authorised
                # recovery lineage, never a later ordinary refresh.
                ancestor, seen = operation_id, set()
                while ancestor and ancestor not in seen:
                    seen.add(ancestor)
                    saved = db.execute("SELECT * FROM studio_direct_calls WHERE operation_id=? AND call_key=? "
                        "AND status='completed' AND response_status BETWEEN 200 AND 299", (ancestor, call_key)).fetchone()
                    if saved:
                        if ancestor != operation_id:
                            db.execute("INSERT OR REPLACE INTO studio_direct_calls VALUES (?,?,'completed',?,?,?,1)",
                                (operation_id, call_key, saved['response_status'], saved['response_headers'], saved['response_body']))
                        return Response(saved['response_status'], json.loads(saved['response_headers']), saved['response_body'])
                    parent = db.execute('SELECT parent_id FROM studio_direct_parents WHERE operation_id=?', (ancestor,)).fetchone()
                    ancestor = parent[0] if parent else None
                db.execute('UPDATE studio_direct_operations SET requests=requests+1,pending=pending+1 WHERE id=?', (operation_id,))
                db.execute("INSERT OR REPLACE INTO studio_direct_calls(operation_id,call_key,status) VALUES (?,?,'started')", (operation_id, call_key))
            try:
                response = original.transport(method, url, headers, body, timeout)
            except BaseException:
                with closing(self.store._connect()) as db, db:
                    db.execute('UPDATE studio_direct_operations SET unknown=unknown+1,pending=pending-1 WHERE id=?', (operation_id,))
                    db.execute("UPDATE studio_direct_calls SET status='unknown' WHERE operation_id=? AND call_key=?", (operation_id, call_key))
                raise
            with closing(self.store._connect()) as db, db:
                # A server error is received, but may already have incurred cost.
                # No automatic fix/negotiation retry after this response.
                db.execute('UPDATE studio_direct_operations SET unknown=unknown+?,pending=pending-1 WHERE id=?',
                           (int(response.status >= 500), operation_id))
                safe_headers = {k: v for k, v in response.headers.items() if k.lower() in ('content-type', 'retry-after')}
                db.execute("UPDATE studio_direct_calls SET status='completed',response_status=?,response_headers=?,response_body=? "
                           'WHERE operation_id=? AND call_key=?',
                           (response.status, json.dumps(safe_headers),
                            response.body if 200 <= response.status < 300 else b'', operation_id, call_key))
            return response
        return Http(send)

    def list(self):
        with closing(self.store._connect()) as db:
            return [dict(r) for r in db.execute('SELECT id,kind,status,created_at,finished_at,requests,unknown '
                'FROM studio_direct_operations ORDER BY rowid DESC LIMIT 50')]

    def authorize_retry(self, operation_id, *, accept_duplicate_cost):
        if accept_duplicate_cost is not True:
            raise ValueError('需要确认可能重复计费')
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM studio_direct_operations WHERE id=?', (operation_id,)).fetchone()
            if not row:
                raise KeyError(operation_id)
            if row['status'] not in ('unknown', 'failed', 'retry_allowed'):
                raise OperationBlocked(operation_id, row['status'])
            db.execute("UPDATE studio_direct_operations SET status='retry_allowed' WHERE id=?", (operation_id,))
        return {'id': operation_id, 'status': 'retry_allowed'}
