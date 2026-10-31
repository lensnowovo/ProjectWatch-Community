"""A fresh public-source pass may coexist with terminal private-extractor holds.

This is not recovery and cannot extract, review, publish or schedule analysis.
It uses the existing collection host, journal, cache and bounded executor. The
material/analysis path requires its own separately frozen admission.
"""
import hashlib
import json
from contextlib import closing

from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError

STAGE = 'independent_public_collection_v1'
RESULT = 'independent_public_collection_result_v1'


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def dependencies(db, project_id, *, exclude_job=''):
    from project_watch.studio.recovery import RefreshBlocked, project_request_holds

    holds = project_request_holds(db, project_id, exclude_job=exclude_job)
    fingerprints, blockers = [], []
    for hold in holds:
        prefix = {'business': 'business', 'details': 'detail'}.get(hold['job_kind'])
        saved = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                           (hold['job_id'], (prefix or '') + '_inputs')).fetchone()
        value = json.loads(saved[0]) if saved else {}
        if (not prefix or hold['kind'] != 'model' or hold['job_status'] not in ('failed', 'done')
                or not value.get('input_version') or not value.get('binding')):
            blockers.append(hold)
            continue
        # Bind the exact old job, frozen extractor inputs and receipts, not a
        # broad exception by kind. No old output is loaded by this public pass.
        calls = [tuple(r) for r in db.execute('SELECT * FROM studio_job_calls WHERE job_id=? ORDER BY call_key',
                                              (hold['job_id'],))]
        item = dict(job_id=hold['job_id'], kind=hold['job_kind'],
                    inputs_sha256=_hash(value), calls_sha256=_hash(calls))
        if item not in fingerprints:
            fingerprints.append(item)
    if blockers:
        raise RefreshBlocked(blockers)
    return sorted(fingerprints, key=lambda r: r['job_id'])


class PublicExecution(JobExecution):
    def __init__(self, original, frozen):
        super().__init__(original.store, original.job, budgets=original.budgets)
        self.allowed_call_kinds = original.allowed_call_kinds
        self.original, self.frozen = original, frozen

    def _guard(self, db):
        self.original._guard(db)
        if dependencies(db, self.job['project_id'], exclude_job=self.job['id']) != self.frozen['holds']:
            raise HttpError('independent_collection_inputs_changed')
        project = db.execute('SELECT status FROM studio_projects WHERE project_id=?',
                             (self.job['project_id'],)).fetchone()
        if not project or project[0] != 'active':
            raise HttpError('project_paused')
        # No continuation key, old paid call inheritance, or takeover can enter
        # through this path, even if inserted after admission.
        if (db.execute('SELECT 1 FROM studio_job_budget_links WHERE job_id=?', (self.job['id'],)).fetchone()
                or db.execute('SELECT 1 FROM studio_call_recoveries WHERE job_id=?', (self.job['id'],)).fetchone()
                or db.execute("SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND "
                              "(stage LIKE 'dsh_request:%' OR stage IN ('material_pipeline_v1','dsh_model_continuation'))",
                              (self.job['id'],)).fetchone()):
            raise HttpError('independent_collection_requires_new_job')

    def call(self, kind, key, transport, **kwargs):
        if kind not in ('search', 'open_data', 'page', 'site'):
            raise HttpError('independent_collection_sources_only')
        return super().call(kind, key, transport, **kwargs)


def run_if_admitted(pipeline, original):
    from project_watch.studio.public_research_admission import STAGE as FULL

    if original.read_checkpoint(FULL):
        return False  # Continue through the ordinary material + insight path.
    frozen = original.read_checkpoint(STAGE)
    if frozen is None:
        return False
    if (original.job['kind'] != 'external' or frozen.get('project_id') != original.job['project_id']
            or frozen.get('job_id') != original.job['id'] or not frozen.get('holds')):
        raise HttpError('invalid_independent_collection')
    from project_watch.studio import collection_host
    from project_watch.studio.dsh_bootstrap import PublicBootstrap

    execution = PublicExecution(original, frozen)
    collection_host.limit_budgets(execution, 'refresh')
    caps = execution.read_checkpoint('independent_collection_caps_v1')
    if caps is None:
        caps = {**execution.budgets, 'model': 0, 'commercial': 0}
        execution.checkpoint('independent_collection_caps_v1', caps)
    for kind, cap in caps.items():
        if cap is not None:
            previous = execution.budgets.get(kind)
            execution.budgets[kind] = cap if previous is None else min(previous, cap)
    previous = getattr(pipeline._execution_state, 'current', None)
    pipeline._execution_state.current = execution
    try:
        with closing(pipeline.store._connect()) as db:
            execution._guard(db)
        if execution.read_checkpoint(RESULT) is not None:
            return True
        # tools binds sources to this job's journal. No model client is invoked.
        _settings, _http, _llm, collectors = pipeline.tools()
        # Freeze admission before PublicBootstrap writes dsh_binding/state;
        # otherwise a fresh job is mistaken for an already-started legacy run.
        collection_host.admit(execution, mode='refresh')
        research = PublicBootstrap(pipeline, execution, collectors, pipeline.reader())
        research.tools = ()
        collected = collection_host.prepare(research, admit_materials=False)
        result = dict(status='partial', reason='public_sources_saved_prior_requests_held',
                      collection=collected, records=0, analysis='not_started',
                      blocking_jobs=[r['job_id'] for r in frozen['holds']])
        execution.checkpoint(RESULT, result)
        execution.checkpoint('dsh_coverage', {'partial': True, 'coverage': {}})
        research.state['finished'] = result
        research.save()
        return True
    finally:
        pipeline._execution_state.current = previous
