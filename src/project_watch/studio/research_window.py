"""Explicit successor execution windows on an unchanged cumulative cost account.

Admission is local and auditable. It never retries a job, refunds a reservation,
changes the original contract, or allows an old job to send another request.
"""
import json
import re
from contextlib import closing
from datetime import UTC, datetime, timedelta

from project_watch.studio.http import HttpError

PREFIX = 'research-execution-window:'


def current(db, batch_id):
    prefix = PREFIX + batch_id + ':'
    row = db.execute('SELECT value FROM studio_state WHERE substr(key,1,?)=? ORDER BY key DESC LIMIT 1',
                     (len(prefix), prefix)).fetchone()
    return json.loads(row[0]) if row else None


def checked(db, account):
    from project_watch.studio.acceptance_batch import digest

    window = current(db, account['batch_id'])
    if window and (window['specification_digest'] != digest(json.loads(account['specification']))
                   or window['original_deadline'] != account['deadline']):
        raise HttpError('research_window_contract_changed')
    return window


def deadline(db, account):
    window = checked(db, account)
    return window['deadline'] if window else account['deadline']


def guard_job(db, account, job):
    window = checked(db, account)
    if not window:
        return
    row = db.execute('SELECT rowid FROM studio_jobs WHERE id=?', (job['id'],)).fetchone()
    if (not row or row[0] <= window['job_fence']
            or job['project_id'] not in window['work']):
        raise HttpError('research_window_old_job')


def admit(store, batch_id, *, window_id, deadline, work, authorization):
    """Append a bounded continuation; historic calls and costs remain verbatim.

    Only an explicitly declared reference-tariff account may carry its frozen
    risk estimate forward. This does not claim a current provider cash tariff.
    """
    from project_watch.studio import acceptance_batch as batch
    from project_watch.studio import research_budget

    if not isinstance(authorization, str) or not 1 <= len(authorization) <= 500:
        raise ValueError('successor window requires explicit authorization')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', window_id):
        raise ValueError('invalid successor window identity')
    at, end = datetime.now(UTC), datetime.fromisoformat(deadline)
    if end.tzinfo is None or not at < end <= at + timedelta(hours=4):
        raise ValueError('successor window must end within four hours')
    if (not isinstance(work, dict) or not 1 <= len(work) <= 2
            or any(not isinstance(v, list) or not v or len(v) > 5
                   or len(set(v)) != len(v)
                   or set(v) - {'sources', 'events', 'analysis', 'reports', 'increment'} for v in work.values())):
        raise ValueError('finite project work list required')
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        original = db.execute('SELECT * FROM studio_acceptance_batches WHERE id=?', (batch_id,)).fetchone()
        if not original or original['state'] != 'closed':
            raise HttpError('research_window_not_closed')
        if db.execute('SELECT 1 FROM studio_acceptance_releases WHERE batch_id=?', (batch_id,)).fetchone():
            raise HttpError('acceptance_continuation_released')
        spec = json.loads(original['specification'])
        cost = spec.get('cost_policy') or {}
        if cost.get('basis') != 'reference_tariff':
            raise HttpError('research_window_price_review_required')
        research_budget.validate({**cost, 'valid_until': end.isoformat()})
        projects = {r['project_id']: r for r in db.execute(
            'SELECT * FROM studio_acceptance_projects WHERE batch_id=?', (batch_id,))}
        if set(work) - projects.keys():
            raise HttpError('research_window_project_changed')
        for pid, row in projects.items():
            if batch.digest(batch.public_input(db, pid)) != row['input_digest']:
                raise HttpError('acceptance_input_changed')
            if db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND status IN ('queued','running')", (pid,)).fetchone():
                raise HttpError('acceptance_not_drained')
        calls = [dict(r) for r in db.execute('SELECT c.* FROM studio_job_calls c '
            'JOIN studio_acceptance_jobs a ON a.job_id=c.job_id '
            'JOIN studio_acceptance_projects p ON p.project_id=a.project_id WHERE p.batch_id=? '
            'ORDER BY c.job_id,c.call_key', (batch_id,))]
        if any(c['status'] not in ('completed', 'failed_read', 'not_sent') for c in calls):
            raise HttpError('request_outcome_unknown')
        if any(c['kind'] == 'model' and (c['response_status'] or 0) >= 400 for c in calls):
            raise HttpError('received_model_failure_requires_review')
        costs = [dict(r) for r in db.execute('SELECT * FROM studio_research_costs WHERE batch_id=? ORDER BY job_id,call_key', (batch_id,))]
        if ({(c['job_id'], c['call_key']) for c in calls} != {(c['job_id'], c['call_key']) for c in costs}
                or any(c['settled'] == -1 for c in costs)):
            raise HttpError('research_history_unpriced')
        spent = sum(c['charged'] for c in costs)
        previous = current(db, batch_id)
        prefix = PREFIX + batch_id + ':'
        if any(json.loads(r[0])['id'] == window_id for r in db.execute(
                'SELECT value FROM studio_state WHERE substr(key,1,?)=?', (len(prefix), prefix))):
            raise HttpError('research_window_already_admitted')
        # Binary response bodies are hashed, never logged or rewritten.
        import hashlib

        for call in calls:
            body = call.pop('response_body')
            call['response_digest'] = hashlib.sha256(body).hexdigest() if body is not None else None
        value = dict(id=window_id, version=1 + (previous['version'] if previous else 0),
            authorization=authorization, admitted_at=at.isoformat(), deadline=end.isoformat(),
            original_deadline=original['deadline'], specification_digest=batch.digest(spec),
            work=work, job_fence=db.execute('SELECT coalesce(max(rowid),0) FROM studio_jobs').fetchone()[0],
            calls_digest=batch.digest(calls), costs_digest=batch.digest(costs),
            consumed={k: batch.usage(db, batch_id, k) for k in batch.KINDS},
            committed=spent, ceiling=cost['ceiling'], price_basis=cost['basis'])
        db.execute('INSERT INTO studio_state VALUES (?,?)',
                   (prefix + f"{value['version']:08d}", batch.encode(value)))
        db.execute("UPDATE studio_acceptance_batches SET state='open' WHERE id=?", (batch_id,))
        return value
