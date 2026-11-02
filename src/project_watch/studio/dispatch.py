"""The one dispatch rule for every job's outgoing requests.

A request journal is accounting, not a gate:

- every dispatched request counts against the project's monthly budget for its
  kind, whatever its outcome (completed, failed, unknown);
- a request whose earlier outcome is unknown, or a model request answered
  with a 5xx, is never sent again automatically within the same job or the
  same analysis batch; the caller gets a definite "not sent". The owner's
  explicit redo is consent to resend; a request still in flight is never sent
  twice. A later round is a new request even if its bytes repeat;
- nothing else blocks work. Another job's pending request is not a reason to
  stop an independent request.

Completed answers are replayed only within the same job (in JobExecution.call):
many requests carry no date, so replaying another job's answer would freeze
news.
"""
import json
from contextlib import closing
from datetime import UTC, datetime

from project_watch.studio.http import HttpError
from project_watch.studio.store import now

KINDS = frozenset(('collect', 'analyze', 'report'))  # the round's job kinds
# Requests per project per calendar month (UTC). Generous for normal use and
# finite, so a fault can never spend without bound. Overridable per project.
DEFAULT_MONTHLY = {'model': 400, 'search': 400, 'open_data': 1500, 'page': 400, 'site': 120, 'commercial': 60}
LEGACY_KINDS = frozenset(('external', 'external_followup', 'insight', 'scope', 'assessment'))
SCHEMA = '''CREATE TABLE IF NOT EXISTS studio_project_budgets (
    project_id TEXT PRIMARY KEY REFERENCES projects(id), monthly TEXT NOT NULL, updated_at TEXT NOT NULL)'''


UNRESOLVED = "c.status IN ('started','unknown')"  # SQL over studio_job_calls c
RECEIVED_FAILURE = '(500,502,503,504,529)'  # answered, but no usable reply; billing may be unknown
RECEIVED_MODEL_FAILURE = f"c.kind='model' AND c.status='completed' AND c.response_status IN {RECEIVED_FAILURE}"
REDO_PREFIX = 'redo:'


def batch_of(job):
    """The collection batch an analysis reads, or None for every other job."""
    version = job.get('input_version') or ''
    if job.get('kind') != 'analyze':
        return None
    for prefix in ('collect:', REDO_PREFIX):
        if version.startswith(prefix):
            return version[len(prefix):]
    return None


def explicit_redo(job):
    """Jobs the owner started from the "re-analyse with saved material" action."""
    return (job.get('input_version') or '').startswith(REDO_PREFIX)


class NotSent(HttpError):
    """Definitely not dispatched; never an unknown outcome."""


def month_start(at=None):
    moment = at or datetime.now(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


def limits(db, project_id):
    db.execute(SCHEMA)
    row = db.execute('SELECT monthly FROM studio_project_budgets WHERE project_id=?', (project_id,)).fetchone()
    return {**DEFAULT_MONTHLY, **(json.loads(row[0]) if row else {})}


def spent(db, project_id, kind, *, since=None):
    """Dispatched requests this month, of every outcome, across all jobs."""
    return db.execute("SELECT count(*) FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
                      "WHERE j.project_id=? AND c.kind=? AND c.status!='not_sent' AND c.created_at>=?",
                      (project_id, kind, since or month_start())).fetchone()[0]


def check(db, job, kind, call_key, provider_hash):
    """Inside JobExecution.call's admission transaction, before the started row."""
    project = db.execute('SELECT status,consent_at FROM studio_projects WHERE project_id=?',
                         (job['project_id'],)).fetchone()
    if not project or (project['status'] == 'paused' and job['kind'] != 'internal'):
        # The owner's own uploaded documents keep being read while public monitoring is paused.
        raise NotSent('project_paused')
    if not project['consent_at'] and job['kind'] in KINDS:
        # Consent covers public monitoring rounds; owner documents and side jobs keep their own scheduling gates.
        raise NotSent('project_not_consented')
    # A request still in flight is never sent twice, whoever asks.
    if db.execute("SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
                  "WHERE j.project_id=? AND c.call_key=? AND c.status='started' AND j.status='running' "
                  "AND j.lease_until>? LIMIT 1", (job['project_id'], call_key, now())).fetchone():
        raise NotSent('request_in_flight')
    # The same logical request with an unresolved or billing-ambiguous outcome is
    # never resent automatically: within one job, and across the analyses of one
    # collection batch (their snapshot sends identical requests). An owner's
    # explicit redo is consent to resend it. A later round is a new request even
    # when its bytes repeat (an undated search query); the earlier one stays
    # recorded, counted and unsettled, never retried.
    batch = batch_of(job)
    if not explicit_redo(job) and db.execute(
            "SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
            "WHERE j.project_id=? AND c.call_key=? AND (j.id=? OR (? IS NOT NULL AND j.kind='analyze' "
            "AND j.input_version IN ('collect:'||?, ?||?))) AND (c.status IN ('unknown','started') "
            f"OR (c.kind='model' AND c.status='completed' AND c.response_status IN {RECEIVED_FAILURE})) LIMIT 1",
            (job['project_id'], call_key, job['id'], batch, batch, REDO_PREFIX, batch)).fetchone():
        raise NotSent('unknown_not_resent')
    backoff = db.execute('SELECT retry_after FROM studio_provider_backoff WHERE provider=?', (provider_hash,)).fetchone()
    if backoff and backoff[0] > now():
        raise NotSent('provider_backoff')
    limit = limits(db, job['project_id']).get(kind)
    if limit is not None and spent(db, job['project_id'], kind) >= limit:
        raise NotSent('project_budget')


def set_monthly(store, project_id, monthly):
    if not isinstance(monthly, dict) or any(k not in DEFAULT_MONTHLY or type(v) is not int or v < 0
                                            for k, v in monthly.items()):
        raise ValueError('invalid monthly budget')
    with closing(store._connect()) as db, db:
        db.execute(SCHEMA)
        db.execute('INSERT INTO studio_project_budgets VALUES (?,?,?) ON CONFLICT(project_id) '
                   'DO UPDATE SET monthly=excluded.monthly,updated_at=excluded.updated_at',
                   (project_id, json.dumps(monthly, sort_keys=True), now()))


def archive_legacy(store, *, reason='archived: replaced by the rewritten pipeline'):
    """End every unfinished job of a retired kind. Request rows are never touched.

    Their completed outputs stay in the evidence/item tables as ordinary
    material; their unknown requests stay in the journal, counted as spent and
    never resent. Returns the archived job ids. Idempotent.
    """
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        marks = ','.join('?' * len(LEGACY_KINDS))
        rows = [r[0] for r in db.execute(
            f"SELECT id FROM studio_jobs WHERE kind IN ({marks}) AND status IN ('queued','running')",
            tuple(sorted(LEGACY_KINDS)))]
        for job_id in rows:
            db.execute("UPDATE studio_jobs SET status='archived',error=?,finished_at=?,lease_token='',lease_until=NULL "
                       "WHERE id=?", (reason, now(), job_id))
        return rows
