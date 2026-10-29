"""Opt-in, finite public acceptance runs on the existing request journal."""
import hashlib
import json
import re
from contextlib import closing
from datetime import UTC, datetime

from project_watch.studio.http import HttpError

KINDS = ('model', 'search', 'page', 'open_data', 'site')
JOBS = ('external', 'external_followup', 'insight', 'scope', 'assessment', 'brief')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS studio_acceptance_batches (
 id TEXT PRIMARY KEY, specification TEXT NOT NULL, deadline TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','closed'))
);
CREATE TABLE IF NOT EXISTS studio_acceptance_projects (
 project_id TEXT PRIMARY KEY REFERENCES studio_projects(project_id),
 batch_id TEXT NOT NULL REFERENCES studio_acceptance_batches(id),
 allocation TEXT NOT NULL, public_input TEXT NOT NULL, input_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_acceptance_jobs (
 job_id TEXT PRIMARY KEY REFERENCES studio_jobs(id),
 project_id TEXT NOT NULL REFERENCES studio_acceptance_projects(project_id),
 parent_job_id TEXT, root_slot TEXT,
 UNIQUE(project_id,root_slot)
);
CREATE TABLE IF NOT EXISTS studio_acceptance_releases (
 batch_id TEXT PRIMARY KEY REFERENCES studio_acceptance_batches(id),
 job_fence INTEGER NOT NULL, released_at TEXT NOT NULL
);
'''


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def public_input(db, pid):
    row = db.execute('SELECT legal_name,website FROM studio_projects WHERE project_id=?', (pid,)).fetchone()
    if row is None:
        raise ValueError('acceptance project missing')
    return dict(row)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def limits(value):
    if set(value) != set(KINDS) or any(type(n) is not int or not 0 <= n <= 10000 for n in value.values()):
        raise ValueError('invalid finite acceptance limits')
    return dict(value)


def create(store, batch_id, *, deadline, caps, journeys, plan_sha256):
    """Local explicit opt-in only; no credentials, internal documents or network."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', batch_id):
        raise ValueError('invalid acceptance id')
    if not re.fullmatch(r'[0-9a-f]{64}', plan_sha256):
        raise ValueError('frozen public plan digest required')
    ends = datetime.fromisoformat(deadline)
    if ends.tzinfo is None or ends <= datetime.now(UTC):
        raise ValueError('acceptance deadline must be in the future')
    caps = limits(caps)
    if not 1 <= len(journeys) <= 2 or len({j['project_id'] for j in journeys}) != len(journeys):
        raise ValueError('acceptance requires one or two distinct journeys')
    allocation = []
    for journey in journeys:
        roots = journey['roots']
        if (not isinstance(roots, list) or not 1 <= len(roots) <= 3 or len(set(roots)) != len(roots)
                or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', slot) for slot in roots)):
            raise ValueError('invalid acceptance root slots')
        allocation.append(dict(project_id=journey['project_id'], caps=limits(journey['caps']), roots=roots))
    if any(sum(j['caps'][k] for j in allocation) > caps[k] for k in KINDS):
        raise ValueError('journey allocations exceed batch')
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        # No upsert: repeating admission must never reset its clock or balance.
        db.execute('INSERT INTO studio_acceptance_batches VALUES (?,?,?,?)',
                   (batch_id, encode(dict(caps=caps, journeys=allocation, plan_sha256=plan_sha256)),
                    ends.astimezone(UTC).isoformat(), 'open'))
        for journey in allocation:
            frozen = public_input(db, journey['project_id'])
            db.execute('INSERT INTO studio_acceptance_projects VALUES (?,?,?,?,?)',
                       (journey['project_id'], batch_id, encode(journey), encode(frozen), digest(frozen)))


def _project(db, pid):
    return db.execute('SELECT p.*, b.specification,b.deadline,b.state FROM studio_acceptance_projects p '
                      'JOIN studio_acceptance_batches b ON b.id=p.batch_id WHERE p.project_id=?', (pid,)).fetchone()


def _active(db, row):
    if row['state'] != 'open':
        raise HttpError('acceptance_closed')
    if datetime.fromisoformat(row['deadline']) <= datetime.now(UTC):
        raise HttpError('acceptance_expired')
    if digest(public_input(db, row['project_id'])) != row['input_digest']:
        raise HttpError('acceptance_input_changed')


def bind_root(store, batch_id, job_id, slot):
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        job = db.execute('SELECT * FROM studio_jobs WHERE id=?', (job_id,)).fetchone()
        row = _project(db, job['project_id']) if job else None
        if (not row or row['batch_id'] != batch_id or job['kind'] != 'external'
                or job['status'] not in ('queued', 'running') or slot not in json.loads(row['allocation'])['roots']):
            raise HttpError('acceptance_root_mismatch')
        _active(db, row)
        if db.execute('SELECT 1 FROM studio_job_calls WHERE job_id=? LIMIT 1', (job_id,)).fetchone():
            raise HttpError('acceptance_root_already_started')
        db.execute('INSERT INTO studio_acceptance_jobs VALUES (?,?,NULL,?)', (job_id, job['project_id'], slot))


def inherit(db, job_id, parent_id, *, reused=False):
    """Called in the same transaction that admits a declared child."""
    job = db.execute('SELECT * FROM studio_jobs WHERE id=?', (job_id,)).fetchone()
    row = _project(db, job['project_id'])
    parent = db.execute('SELECT * FROM studio_acceptance_jobs WHERE job_id=?', (parent_id,)).fetchone()
    if parent and parent['project_id'] != job['project_id']:
        raise HttpError('acceptance_unbound_successor')
    if row is None:
        return
    if _ordinary_after_release(db, row, job_id) and not parent:
        if not _ordinary_after_release(db, row, parent_id):
            raise HttpError('acceptance_old_successor')
        return
    _active(db, row)
    if not parent or parent['project_id'] != job['project_id'] or job['kind'] not in JOBS:
        raise HttpError('acceptance_unbound_successor')
    old = db.execute('SELECT * FROM studio_acceptance_jobs WHERE job_id=?', (job_id,)).fetchone()
    if old:
        if old['parent_job_id'] != parent_id:
            raise HttpError('acceptance_parent_changed')
        return
    # An existing unowned job has no proven parent, even if its input matches.
    if reused:
        raise HttpError('acceptance_parent_unproven')
    if db.execute('SELECT 1 FROM studio_job_calls WHERE job_id=? LIMIT 1', (job_id,)).fetchone():
        raise HttpError('acceptance_successor_already_started')
    db.execute('INSERT INTO studio_acceptance_jobs VALUES (?,?,?,NULL)', (job_id, job['project_id'], parent_id))


def usage(db, batch_id, kind, *, project_id=None):
    # A call linked through several descendants is counted once. In-flight,
    # unknown and failed attempts remain conservatively charged to this cap.
    return db.execute('''WITH members AS (
        SELECT j.job_id FROM studio_acceptance_jobs j JOIN studio_acceptance_projects p
        ON p.project_id=j.project_id WHERE p.batch_id=? AND (? IS NULL OR p.project_id=?)
    ), calls AS (
        SELECT c.job_id,c.call_key FROM studio_job_calls c JOIN members m ON m.job_id=c.job_id
        UNION SELECT l.origin_job_id,l.call_key FROM studio_job_budget_links l JOIN members m ON m.job_id=l.job_id
    ) SELECT count(*) FROM calls x JOIN studio_job_calls c ON c.job_id=x.job_id AND c.call_key=x.call_key
      WHERE c.kind=?''', (batch_id, project_id, project_id, kind)).fetchone()[0]


def guard_send(db, job, kind):
    """Inside the journal's BEGIN IMMEDIATE, before inserting started."""
    row = _project(db, job['project_id'])
    if row is None:
        return
    if _ordinary_after_release(db, row, job['id']):
        return
    _active(db, row)
    if (job['kind'] not in JOBS or kind not in KINDS or not db.execute(
            'SELECT 1 FROM studio_acceptance_jobs WHERE job_id=? AND project_id=?',
            (job['id'], job['project_id'])).fetchone()):
        raise HttpError('acceptance_unbound_job')
    allocation, specification = json.loads(row['allocation']), json.loads(row['specification'])
    if (usage(db, row['batch_id'], kind) >= specification['caps'][kind]
            or usage(db, row['batch_id'], kind, project_id=job['project_id']) >= allocation['caps'][kind]):
        raise HttpError('acceptance_budget_exhausted')


def close(store, batch_id):
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_acceptance_batches SET state='closed' WHERE id=?", (batch_id,))


def _ordinary_after_release(db, row, job_id):
    """Only newly admitted, independent work may leave the closed batch fence."""
    release = db.execute('SELECT job_fence FROM studio_acceptance_releases WHERE batch_id=?',
                         (row['batch_id'],)).fetchone()
    job = db.execute('SELECT rowid FROM studio_jobs WHERE id=? AND project_id=?',
                     (job_id, row['project_id'])).fetchone()
    if not release or not job or job[0] <= release[0]:
        return False
    if db.execute('SELECT 1 FROM studio_acceptance_jobs WHERE job_id=?', (job_id,)).fetchone():
        return False
    # A new ID with old inherited calls is still old work, not an independent round.
    return not db.execute('SELECT 1 FROM studio_job_budget_links l JOIN studio_jobs j ON j.id=l.origin_job_id '
                          'WHERE l.job_id=? AND j.project_id=? AND j.rowid<=? LIMIT 1',
                          (job_id, row['project_id'], release[0])).fetchone()


def guard_transfer(db, origin_job_id, target_job_id):
    """Releasing a project never authorizes carrying its old work into a new round."""
    origin = db.execute('SELECT project_id FROM studio_jobs WHERE id=?', (origin_job_id,)).fetchone()
    row = _project(db, origin[0]) if origin else None
    if row and _ordinary_after_release(db, row, target_job_id) and not _ordinary_after_release(db, row, origin_job_id):
        raise HttpError('acceptance_old_successor')
    if row and not _ordinary_after_release(db, row, origin_job_id):
        _active(db, row)


def release(store, batch_id):
    """Explicit, irreversible end; retain every member, receipt and original cap."""
    from project_watch.studio.material_ownership import project_guard
    from project_watch.studio.recovery import project_request_holds, unknown_queries

    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        batch = db.execute('SELECT * FROM studio_acceptance_batches WHERE id=?', (batch_id,)).fetchone()
        if not batch:
            raise HttpError('acceptance_missing')
        if db.execute('SELECT 1 FROM studio_acceptance_releases WHERE batch_id=?', (batch_id,)).fetchone():
            return  # No new fence, clock or allowance on repeated release.
        if batch['state'] == 'open' and datetime.fromisoformat(batch['deadline']) > datetime.now(UTC):
            raise HttpError('acceptance_not_ended')
        for row in db.execute('SELECT project_id FROM studio_acceptance_projects WHERE batch_id=?', (batch_id,)):
            pid = row[0]
            if db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND status IN ('queued','running') LIMIT 1",
                          (pid,)).fetchone():
                raise HttpError('acceptance_not_drained')
            project_guard(db, pid, '')
            if project_request_holds(db, pid) or any(unknown_queries(db, pid, j[0]) for j in db.execute(
                    'SELECT id FROM studio_jobs WHERE project_id=?', (pid,))):
                raise HttpError('acceptance_unsettled_requests')
        fence = db.execute('SELECT coalesce(max(rowid),0) FROM studio_jobs').fetchone()[0]
        db.execute('INSERT INTO studio_acceptance_releases VALUES (?,?,?)',
                   (batch_id, fence, datetime.now(UTC).isoformat()))


def remaining(store, job):
    """Advisory closeout view only; guard_send remains the atomic authority."""
    with closing(store._connect()) as db:
        db.execute('BEGIN')
        row = _project(db, job['project_id'])
        if row is None:
            return None
        if _ordinary_after_release(db, row, job['id']):
            return None
        try:
            _active(db, row)
        except HttpError:
            return dict.fromkeys(KINDS, 0)
        if job['kind'] not in JOBS or not db.execute(
                'SELECT 1 FROM studio_acceptance_jobs WHERE job_id=? AND project_id=?',
                (job['id'], job['project_id'])).fetchone():
            return dict.fromkeys(KINDS, 0)
        allocation, spec = json.loads(row['allocation']), json.loads(row['specification'])
        return {kind: max(0, min(spec['caps'][kind] - usage(db, row['batch_id'], kind),
                    allocation['caps'][kind] - usage(db, row['batch_id'], kind, project_id=job['project_id'])))
                for kind in KINDS}
