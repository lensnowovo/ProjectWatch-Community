"""Provider-reported usage, attached to existing dispatch receipts.

Missing counts stay null; known sums are lower bounds, not invoice estimates.
Unleased settings checks have separate local receipts without prompts or bodies.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from contextlib import closing

from project_watch.studio.http import Http, HttpError
from project_watch.studio.store import now

FIELDS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens')


def token_count(value):
    # Do not coerce strings, bools, floats, negatives or absurd/untrusted counts.
    return value if type(value) is int and 0 <= value <= 10**12 else None


def reported(response):
    result = dict.fromkeys(FIELDS)
    try:
        data = response.json()
        usage = data.get('usage') if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return result
        result['input_tokens'] = token_count(usage.get('prompt_tokens', usage.get('input_tokens')))
        result['output_tokens'] = token_count(usage.get('completion_tokens', usage.get('output_tokens')))
        # Keep provider fields separate; different APIs include caches differently.
        result['cache_read_tokens'] = token_count(usage.get('cache_read_input_tokens'))
        result['cache_creation_tokens'] = token_count(usage.get('cache_creation_input_tokens'))
    except (HttpError, ValueError, TypeError):
        pass
    return result


class UsageCounter:
    """Local opt-in evaluation counter, including failed provider dispatches."""
    def __init__(self):
        self.totals = {'provider_requests': 0, 'known_calls': 0, 'unknown_calls': 0,
                       **dict.fromkeys(FIELDS, 0)}

    def http(self, original):
        def send(*args):
            self.totals['provider_requests'] += 1
            self.totals['unknown_calls'] += 1
            response = original.transport(*args)
            counts = reported(response)
            if all(counts[f] is not None for f in ('input_tokens', 'output_tokens')):
                self.totals['known_calls'] += 1
                self.totals['unknown_calls'] -= 1
            for f in FIELDS:
                self.totals[f] += counts[f] or 0
            return response
        return Http(send)


def direct_http(store, original, *, is_model, provider):
    """Record each actual unleased model dispatch, including failures/negotiation."""
    provider_hash = hashlib.sha256(provider.encode()).hexdigest()
    def send(method, url, headers, body, timeout):
        if not is_model(url):
            return original.transport(method, url, headers, body, timeout)
        call_id = str(uuid.uuid4())
        with closing(store._connect()) as db, db:
            db.execute('INSERT INTO studio_direct_model_calls(id,provider,status,created_at) '
                       "VALUES (?,?,'started',?)", (call_id, provider_hash, now()))
        started = time.monotonic()
        try:
            response = original.transport(method, url, headers, body, timeout)
        except BaseException:
            with closing(store._connect()) as db, db:
                db.execute("UPDATE studio_direct_model_calls SET status='unknown',elapsed_ms=?,finished_at=? WHERE id=?",
                           (int((time.monotonic() - started) * 1000), now(), call_id))
            raise
        counts = reported(response)
        with closing(store._connect()) as db, db:
            db.execute("UPDATE studio_direct_model_calls SET status='completed',response_status=?,"
                       'input_tokens=?,output_tokens=?,cache_read_tokens=?,cache_creation_tokens=?,elapsed_ms=?,finished_at=? WHERE id=?',
                       (response.status, *(counts[f] for f in FIELDS), int((time.monotonic() - started) * 1000), now(), call_id))
        return response
    return Http(send)


def rows(store, days=30):
    """Per-calendar-day observed totals; legacy aggregates are explicitly separate.

    Old journal rows are not re-counted alongside old daily aggregates. No model
    inference/backfill of missing usage and no network operations on this read.
    """
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(UTC).date() - timedelta(days=max(1, min(int(days), 365)) - 1)).isoformat()
    with closing(store._connect()) as db:
        calls = db.execute('SELECT created_at,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens '
                           "FROM studio_job_calls WHERE kind='model' AND usage_version=1 AND created_at>=? "
                           'UNION ALL SELECT created_at,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens '
                           'FROM studio_direct_model_calls WHERE created_at>=?', (since, since)).fetchall()
        legacy = db.execute('SELECT * FROM studio_usage WHERE day>=?', (since,)).fetchall()
    totals = {}
    def day_row(day):
        return totals.setdefault(day, {'day': day, 'calls': 0, 'input_tokens': 0, 'output_tokens': 0,
            'known_calls': 0, 'unknown_calls': 0, 'legacy_calls': 0, 'input_known_calls': 0,
            'output_known_calls': 0, 'cache_read_tokens': 0, 'cache_creation_tokens': 0})
    for call in calls:
        row = day_row(call['created_at'][:10])
        row['calls'] += 1
        known = all(call[f] is not None for f in ('input_tokens', 'output_tokens'))
        row['known_calls' if known else 'unknown_calls'] += 1
        for f in FIELDS:
            row[f] += call[f] or 0
        row['input_known_calls'] += call['input_tokens'] is not None
        row['output_known_calls'] += call['output_tokens'] is not None
    for old in legacy:
        row = day_row(old['day'])
        row['calls'] += old['calls']
        row['legacy_calls'] += old['calls']
        row['input_tokens'] += old['input_tokens']
        row['output_tokens'] += old['output_tokens']
    return [totals[day] for day in sorted(totals, reverse=True)]


def jobs(store, project_id, *, limit=30):
    """Stored bounded job/purpose totals; no bodies, keys, leases or paid reads."""
    result = []
    with closing(store._connect()) as db:
        recent = db.execute('SELECT id,kind,status,created_at FROM studio_jobs WHERE project_id=? '
                            'ORDER BY created_at DESC,id DESC LIMIT ?',
                            (project_id, max(1, min(int(limit), 100)))).fetchall()
        for job in recent:
            kinds = [dict(r) for r in db.execute('''SELECT c.kind,count(*) requests,
                sum(c.status='completed') completed,
                sum(c.status IN ('started','unknown')) outcome_unknown,
                sum(c.usage_version=0) legacy_requests,
                sum(c.kind='model' AND c.usage_version=1 AND c.input_tokens IS NOT NULL
                    AND c.output_tokens IS NOT NULL) known_usage,
                COALESCE(sum(c.input_tokens),0) input_tokens,COALESCE(sum(c.output_tokens),0) output_tokens,
                COALESCE(sum(c.elapsed_ms),0) elapsed_ms,sum(c.elapsed_ms IS NOT NULL) timed_requests,
                COALESCE(sum(h.hits),0) replay_hits,min(c.request_limit) min_request_limit,
                max(c.request_limit) max_request_limit
                FROM studio_job_calls c LEFT JOIN studio_job_cache_hits h
                ON c.job_id=h.job_id AND c.call_key=h.call_key WHERE c.job_id=? GROUP BY c.kind''', (job['id'],))]
            query_uses = {r['outcome']: r['n'] for r in db.execute('SELECT outcome,count(*) n '
                'FROM studio_query_uses WHERE project_id=? AND job_id=? GROUP BY outcome', (project_id, job['id']))}
            result.append({**dict(job), 'purpose': job['kind'], 'token_budget': None,
                           'kinds': kinds, 'query_uses': query_uses})
    return result
