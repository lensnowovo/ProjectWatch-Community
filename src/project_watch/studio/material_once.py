"""Explicit one-material continuation of a terminal, budgeted public round.

No scheduler/API entry, new job, source fetch, retry or budget reset. A reviewed
caller must supply the exact read-only plan and a finite lease window. The
original terminal row and checkpoints remain versioned; a consumed start record
is permanent even on failure. Ordinary workers never choose this path.
"""
import hashlib
import json
import uuid
from contextlib import closing
from datetime import UTC, datetime

from project_watch.studio import (
    dsh_extraction,
    dsh_record_review,
    dsh_runtime,
    evidence_tasks,
    progressive_bootstrap,
)
from project_watch.studio.dsh_bootstrap import COVERAGE, PublicBootstrap
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.material_ownership import MaterialExecution, project_guard
from project_watch.studio.store import now

START = 'fixed_material_once_v1'
END = 'fixed_material_once_result_v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _material(db, job, task_id):
    row = db.execute('SELECT * FROM studio_evidence_tasks WHERE id=? AND job_id=? AND project_id=?',
                     (task_id, job['id'], job['project_id'])).fetchone()
    if not row or row['policy'] != evidence_tasks.TYPED_POLICY:
        raise ValueError('fixed material owner or policy changed')
    task = dict(row)
    if evidence_tasks._withdrawn(db, task):
        raise ValueError('fixed material withdrawn')
    evidence = db.execute("SELECT record,source_key,content_hash FROM studio_evidence_versions "
        "WHERE id=? AND project_id=? AND visibility='public'", (task['evidence_version_id'], job['project_id'])).fetchone()
    if not evidence:
        raise ValueError('fixed public evidence missing')
    latest = db.execute('SELECT id FROM studio_evidence_versions WHERE project_id=? AND source_key=? '
        'ORDER BY seq DESC LIMIT 1', (job['project_id'], evidence['source_key'])).fetchone()
    if latest[0] != task['evidence_version_id']:
        raise ValueError('fixed evidence superseded')
    items = [dict(r) for r in db.execute('SELECT DISTINCT i.id,i.category,i.status,i.extra,i.url FROM studio_items i '
        'JOIN studio_collection_observations o ON o.item_id=i.id WHERE o.evidence_version_id=? AND i.project_id=? '
        'ORDER BY i.id', (task['evidence_version_id'], job['project_id']))]
    if not items or any(i['category'] != 'paper' or i['status'] == 'dismissed' for i in items):
        raise ValueError('fixed paper unavailable')
    if not any(any(json.loads(i['extra']).get(k) for k in ('doi', 'pmid', 'openalex_id')) for i in items):
        raise ValueError('fixed paper stable identity required')
    for item in items:
        extra = json.loads(item.pop('extra'))
        item['identity'] = {k: extra.get(k) for k in ('doi', 'pmid', 'openalex_id', 'source_identity')}
    # Freeze the complete provider record, not a keyword-based attribution.
    identity = dict(task_id=task_id, evidence_version_id=task['evidence_version_id'],
                    record_sha256=hashlib.sha256(evidence['record'].encode()).hexdigest(), items_sha256=digest(items))
    return task, identity


def plan(store, job_id, task_id):
    """Read only. The caller also reviews the source's company association."""
    with closing(store._connect()) as db:
        db.execute('BEGIN')
        return _plan(store, db, job_id, task_id)


def _plan(store, db, job_id, task_id):
    job = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (job_id,)).fetchone())
    if job['status'] != 'done':
        raise ValueError('completed original round required')
    if db.execute('SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage=?', (job_id, START)).fetchone():
        raise ValueError('one-time continuation already consumed')
    project_guard(db, job['project_id'], job_id)
    if db.execute("SELECT 1 FROM studio_job_calls WHERE job_id=? AND status IN ('started','unknown')", (job_id,)).fetchone():
        raise HttpError('request_outcome_unknown')
    if db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND status IN ('running','queued')",
                  (job['project_id'],)).fetchone():
        raise ValueError('project has other work')
    task, identity = _material(db, job, task_id)
    if task['status'] != 'pending' or json.loads(task['result']):
        raise ValueError('unattempted pending material required')
    if db.execute('SELECT 1 FROM studio_material_calls WHERE task_id=?', (task_id,)).fetchone() or db.execute(
        'SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage IN (?,?,?)',
        (job_id, 'extract_attempt:' + task_id, 'extract_result:' + task_id, evidence_tasks.DEFERRED + task_id)).fetchone():
        raise ValueError('material already attempted')
    execution = JobExecution(store, job)
    caps = execution.read_checkpoint('material_round_caps_v1', db=db)
    owner = execution.read_checkpoint(progressive_bootstrap.OWNER, db=db) or {}
    closeout = execution.read_checkpoint(progressive_bootstrap.CLOSEOUT, db=db) or {}
    if (owner.get('owner') != 'materials' or closeout.get('reason') != 'publication_reserve'
            or task_id not in closeout.get('pending_task_ids', []) or not caps or type(caps.get('model')) is not int):
        raise ValueError('original material admission required')
    used = execution.budget_used('model', db=db)
    if caps['model'] - used < 2:
        raise ValueError('two original material calls required')
    checkpoints = [list(r) for r in db.execute('SELECT stage,data FROM studio_job_checkpoints WHERE job_id=? ORDER BY stage', (job_id,))]
    calls = [list(r) for r in db.execute('SELECT job_id,call_key,status,kind,provider FROM studio_job_calls '
        'WHERE job_id=? ORDER BY call_key', (job_id,))]
    return dict(version=1, job_id=job_id, project_id=job['project_id'], original_job=job,
                checkpoints_sha256=digest(checkpoints), calls_sha256=digest(calls), material=identity,
                used_model=used, original_caps=caps, maximum_new_model=2,
                source_calls=0, extraction_calls=1, admission_calls=1)


class OnceExecution(JobExecution):
    def __init__(self, store, job, frozen, token, deadline):
        caps = {k: 0 for k in JobExecution(store, job).budgets}
        caps['model'] = min(frozen['original_caps']['model'], frozen['used_model'] + 2)
        super().__init__(store, job, budgets=caps)
        self.allowed_call_kinds = frozenset({"model"})
        self.frozen, self.token, self.deadline = frozen, token, deadline

    def _guard(self, db):
        super()._guard(db)
        saved = super()._read_checkpoint(db, START)
        if not saved or saved['token'] != self.token or now() >= self.deadline:
            raise RuntimeError('continuation lease expired or replaced')
        project = db.execute('SELECT status FROM studio_projects WHERE project_id=?', (self.job['project_id'],)).fetchone()
        if not project or project[0] != 'active':
            raise HttpError('project_paused')
        if db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND id!=? AND status IN ('running','queued')",
                      (self.job['project_id'], self.job['id'])).fetchone():
            raise ValueError('project has other work')
        project_guard(db, self.job['project_id'], self.job['id'])
        if _material(db, self.job, self.frozen['material']['task_id'])[1] != self.frozen['material']:
            raise ValueError('fixed material changed')

    @staticmethod
    def _stage(stage):
        return 'once:' + hashlib.sha256(stage.encode()).hexdigest()[:48]

    def checkpoint(self, stage, data):
        super().checkpoint(self._stage(stage), data)

    def checkpoint_chunked(self, stage, data):
        super().checkpoint_chunked(self._stage(stage), data)

    def _read_checkpoint(self, db, stage):
        value = super()._read_checkpoint(db, self._stage(stage))
        if value is not None:
            return value
        # Keep the former completed publication intact; new records are built
        # only from the selected material. Everything else retains its identity.
        if stage == 'dsh_state':
            original = super()._read_checkpoint(db, stage)
            return {'sources': [], 'searches': [], 'errors': [], 'events': [], **(original or {}),
                    'records': [], 'finished': None}
        if stage.startswith('dsh_record_review_') or stage in ('dsh_record_admission', 'material_mapping_v2'):
            return None
        return super()._read_checkpoint(db, stage)

    def call(self, kind, *args, **kwargs):
        if kind != 'model':
            raise HttpError('continuation_source_denied')
        return super().call(kind, *args, **kwargs)


class OnceMaterial(MaterialExecution):
    def __init__(self, parent, task):
        super().__init__(parent, task)
        self.budgets['model'] = min(self.budgets['model'], parent.frozen['used_model'] + 1)

    def _read_checkpoint(self, db, stage):
        return self.parent._read_checkpoint(db, stage)

    def checkpoint(self, stage, data):
        return self.parent.checkpoint(stage, data)

    def checkpoint_chunked(self, stage, data):
        return self.parent.checkpoint_chunked(stage, data)

    def call(self, kind, *args, **kwargs):
        if kind != 'model':
            raise HttpError('continuation_source_denied')
        return super().call(kind, *args, **kwargs)


def begin(store, frozen, *, deadline, grant_id):
    """Consume the single slot atomically; explicit runner supplies review grant."""
    end = datetime.fromisoformat(deadline)
    seconds = (end - datetime.now(UTC)).total_seconds()
    if not grant_id or not 0 < seconds <= 5400:
        raise ValueError('finite continuation grant required')
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if _plan(store, db, frozen['job_id'], frozen['material']['task_id']) != frozen:
            raise ValueError('continuation baseline changed')
        token = uuid.uuid4().hex
        # Append the complete old terminal row before taking the normal lease.
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (frozen['job_id'], START,
            json.dumps(dict(plan=frozen, token=token, deadline=deadline, grant_id=grant_id, started_at=now()))))
        db.execute("UPDATE studio_jobs SET status='running',lease_token=?,lease_until=? WHERE id=? AND status='done'",
                   (token, deadline, frozen['job_id']))
        job = dict(db.execute('SELECT * FROM studio_jobs WHERE id=?', (frozen['job_id'],)).fetchone())
    return OnceExecution(store, job, frozen, token, deadline)


def finish(execution, outcome):
    """Keep even failed once receipts; never clear a hold or permit another run."""
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT lease_token FROM studio_jobs WHERE id=?', (execution.job['id'],)).fetchone()
        if not current or current[0] != execution.token:
            raise RuntimeError('continuation lease replaced; preserve current owner')
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', (execution.job['id'], END,
            json.dumps(dict(outcome=outcome, ended_at=now(), new_model_used=execution.budget_used('model', db=db)
                            - execution.frozen['used_model']))))
        original = execution.frozen['original_job']
        columns = [k for k in original if k != 'id']
        db.execute('UPDATE studio_jobs SET ' + ','.join('"' + k + '"=?' for k in columns) + ' WHERE id=?',
                   [original[k] for k in columns] + [original['id']])


def run(pipeline, execution, runtime_root, directory):
    """One extraction and at most one standard semantic admission, no analysis."""
    task_id = execution.frozen['material']['task_id']
    gateway = None
    outcome = {'status': 'stopped', 'reason': 'execution_failed'}
    try:
        settings, http, _, collectors = pipeline.tools(execution=execution)
        research = PublicBootstrap(pipeline, execution, collectors, None)
        gateway = dsh_runtime.Gateway(research, settings.model, dsh_runtime.guarded_http(research, http))
        def extract(source, material):
            scoped = OnceMaterial(execution, material.task)
            source = {**source, 'project': {'legal_name': research.project['legal_name']}}
            return dsh_extraction.extract(research, source, settings.model, http, runtime_root, directory, execution=scoped)

        if evidence_tasks.run_pending(execution, extract, limit=1, policy=evidence_tasks.TYPED_POLICY, task_id=task_id) != 1:
            raise ValueError('fixed material unavailable')
        with closing(execution.store._connect()) as db:
            task = dict(db.execute('SELECT * FROM studio_evidence_tasks WHERE id=?', (task_id,)).fetchone())
        if task['status'] != 'complete':
            outcome = {'status': 'stopped', 'reason': json.loads(task['result']).get('code', task['status'])}
            return outcome
        progressive_bootstrap._map_records(research, task_id=task_id)
        mapping = execution.read_checkpoint('material_mapping_v2')
        if mapping['issues'] or not research.state['records']:
            outcome = {'status': 'stopped', 'reason': 'candidate_not_admitted'}
            return outcome
        old = pipeline.store.get_public_profile(research.project_id) or {}
        needed = dsh_record_review.pending_admission_calls(research, research.state['records'], old.get('facts') or [])
        if needed != 1:
            outcome = {'status': 'stopped', 'reason': 'admission_exceeds_single_call'}
            return outcome
        _, _, receipt = dsh_record_review.admit(research, research.state['records'], old.get('facts') or [])
        if receipt['rejected']:
            outcome = {'status': 'stopped', 'reason': 'semantic_rejected', 'admission': receipt}
            return outcome
        # The existing publisher reuses the exact saved admission; no second
        # reviewer invocation, collection, analysis or new queue is introduced.
        result = research.pw_finish({'gaps': {k: '本次仅处理一份已有材料' for k in COVERAGE}})
        research.save()
        outcome = {'status': 'partial', 'reason': 'fixed_material_published', 'publication': result}
        return outcome
    except BaseException as exc:
        outcome = {'status': 'stopped', 'reason': getattr(exc, 'code', type(exc).__name__)}
        raise
    finally:
        if gateway:
            gateway.closing.set()
        finish(execution, outcome)
