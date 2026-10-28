"""Material ownership transfers within the existing leased execution/journal.

No new worker or retry mechanism. Old call receipts remain owned by their job;
links permit exact completed-response reuse, not redispatch of uncertain calls.
"""
import hashlib
import json
from contextlib import closing

from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response
from project_watch.studio.recovery import UNRESOLVED
from project_watch.studio.store import now

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS studio_material_calls (
    task_id TEXT NOT NULL REFERENCES studio_evidence_tasks(id), kind TEXT NOT NULL,
    request_key TEXT NOT NULL, provider TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES studio_jobs(id),
    PRIMARY KEY(task_id,kind,request_key,provider))""",
    """CREATE TABLE IF NOT EXISTS studio_material_transfers (
    task_id TEXT NOT NULL REFERENCES studio_evidence_tasks(id), previous_job TEXT NOT NULL,
    next_job TEXT NOT NULL, transferred_at TEXT NOT NULL,
    PRIMARY KEY(task_id,previous_job,next_job))""",
)


def project_guard(db, project_id, current_job):
    uncertain = db.execute("SELECT 1 FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id "
        f"WHERE j.project_id=? AND ({UNRESOLVED}) AND (c.status='unknown' OR c.job_id!=?) LIMIT 1",
        (project_id, current_job)).fetchone()
    if uncertain:
        raise HttpError('request_outcome_unknown')
    holds = db.execute("SELECT c.data FROM studio_job_checkpoints c JOIN studio_jobs j ON j.id=c.job_id "
                       "WHERE j.project_id=? AND c.stage='dsh_hold'", (project_id,))
    for row in holds:
        hold = json.loads(row[0])
        if hold:
            raise HttpError(hold['code'])


def take_over(execution, task_id):
    """Explicit transfer of a pending material from a terminal job, atomically."""
    from project_watch.studio.evidence_tasks import TYPED_POLICY

    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        pid, next_job = execution.job['project_id'], execution.job['id']
        project_guard(db, pid, next_job)
        task = db.execute('SELECT * FROM studio_evidence_tasks WHERE id=? AND project_id=?', (task_id, pid)).fetchone()
        if not task or task['status'] != 'pending':
            raise ValueError('pending material required in project')
        previous = task['job_id']
        if previous == next_job:
            return
        old = db.execute('SELECT status FROM studio_jobs WHERE id=?', (previous,)).fetchone()
        if old[0] not in ('done', 'failed'):
            raise ValueError('previous material owner is not terminal')
        # Older adapters may have dispatched without a material-call link.
        # Never infer that missing provenance means an unsent request.
        attempts = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                              (previous, 'extract_attempt:' + task_id)).fetchone()
        result = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                            (previous, 'extract_result:' + task_id)).fetchone()
        linked = db.execute('SELECT count(*) FROM studio_material_calls WHERE task_id=?', (task_id,)).fetchone()[0]
        deferred = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                              (previous, 'material_deferred:' + task_id)).fetchone()
        frozen = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                            (previous, 'material_pipeline_v1')).fetchone()
        unsent = (not attempts and not linked and not result and deferred and frozen
                  and json.loads(frozen[0]).get('owner') == 'materials'
                  and json.loads(frozen[0]).get('policy') == task['policy'] == TYPED_POLICY
                  and json.loads(deferred[0]) == {'task_id': task_id, 'policy': task['policy'],
                                                 'dispatch': 'not_started', 'reason': 'publication_reserve'})
        if attempts and json.loads(attempts[0]) and not result and not linked:
            raise ValueError('material dispatch provenance missing')
        if not result and not linked and not unsent and db.execute("SELECT 1 FROM studio_job_calls WHERE job_id=? AND kind='model' LIMIT 1",
                                                   (previous,)).fetchone():
            raise ValueError('material dispatch provenance missing')
        for prefix in ('extract_attempt:', 'extract_result:'):
            stage = prefix + task_id
            source = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?', (previous, stage)).fetchone()
            existing = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?', (next_job, stage)).fetchone()
            if existing and (not source or source[0] != existing[0]):
                raise ValueError('material checkpoint conflict')
            if source:
                db.execute('INSERT OR IGNORE INTO studio_job_checkpoints VALUES (?,?,?)', (next_job, stage, source[0]))
        db.execute('UPDATE studio_evidence_tasks SET job_id=? WHERE id=? AND job_id=?', (next_job, task_id, previous))
        # Admission belongs to the whole successor job, not one role wrapper.
        # UNION/PK deduplicate shared receipts, including multi-hop transfers.
        db.execute('INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,c.job_id,c.call_key '
            'FROM studio_material_calls l JOIN studio_job_calls c ON c.job_id=l.job_id AND c.call_key=l.request_key '
            'WHERE l.task_id=?', (next_job, task_id))
        if result and not linked:
            # A legacy saved proposal can be reused locally, but its old model
            # calls lack material attribution. Conservatively carry that cost.
            db.execute("INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,job_id,call_key FROM studio_job_calls "
                       "WHERE job_id=? AND kind='model'", (next_job, previous))
            db.execute('INSERT OR IGNORE INTO studio_job_budget_links SELECT ?,origin_job_id,call_key '
                       'FROM studio_job_budget_links WHERE job_id=?', (next_job, previous))
        db.execute('INSERT INTO studio_material_transfers VALUES (?,?,?,?)', (task_id, previous, next_job, now()))


class MaterialExecution(JobExecution):
    """Same job, lease and budgets, plus durable material-to-call provenance."""
    def __init__(self, parent, task):
        super().__init__(parent.store, parent.job, budgets=parent.budgets)
        self.parent, self.task = parent, task

    def _guard(self, db):
        self.parent._guard(db)
        row = db.execute("SELECT 1 FROM studio_evidence_tasks WHERE id=? AND job_id=? AND status='pending'",
                         (self.task['id'], self.job['id'])).fetchone()
        if not row:
            raise RuntimeError('material owner expired or replaced')

    def call(self, kind, key, transport, *, provider='', **kwargs):
        digest = hashlib.sha256((kind + ':' + key).encode()).hexdigest()
        provider_hash = hashlib.sha256(provider.encode()).hexdigest()
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._guard(db)
            project_guard(db, self.job['project_id'], self.job['id'])
            link = db.execute('SELECT job_id FROM studio_material_calls WHERE task_id=? AND kind=? AND request_key=? AND provider=?',
                              (self.task['id'], kind, digest, provider_hash)).fetchone()
            if link and link[0] != self.job['id']:
                old = db.execute('SELECT * FROM studio_job_calls WHERE job_id=? AND call_key=?', (link[0], digest)).fetchone()
                if old:
                    if old['status'] != 'completed' or old['provider'] != provider_hash:
                        raise HttpError('request_outcome_unknown')
                    db.execute('INSERT INTO studio_job_cache_hits VALUES (?,?,1) ON CONFLICT(job_id,call_key) DO UPDATE SET hits=hits+1',
                               (link[0], digest))
                    self.last_replay = True
                    return Response(old['response_status'], json.loads(old['response_headers']), old['response_body'])
            db.execute('INSERT INTO studio_material_calls VALUES (?,?,?,?,?) ON CONFLICT(task_id,kind,request_key,provider) '
                       'DO UPDATE SET job_id=excluded.job_id', (self.task['id'], kind, digest, provider_hash, self.job['id']))
        return super().call(kind, key, transport, provider=provider, **kwargs)
