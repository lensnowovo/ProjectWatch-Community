"""Durable material-stage work under its existing parent job and call budget.

These are candidate observations, never admitted news or semantic approval.
No independent scheduler, lease, transport or automatic retry is introduced.
"""
import hashlib
import json
from contextlib import closing
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

POLICY = 'event-observation-v1'
SCHEMA = """CREATE TABLE IF NOT EXISTS studio_evidence_tasks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    job_id TEXT NOT NULL REFERENCES studio_jobs(id),
    evidence_version_id TEXT NOT NULL REFERENCES studio_evidence_versions(id),
    policy TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','complete','rejected')),
    result TEXT NOT NULL DEFAULT '{}',
    UNIQUE(project_id,evidence_version_id,policy))"""


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=3000)
    event_at: str | None = Field(default=None, min_length=10, max_length=10)
    refs: list[Annotated[str, Field(min_length=64, max_length=64)]] = Field(min_length=1, max_length=20)

    @field_validator('event_at')
    @classmethod
    def explicit_date(cls, value):
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError('explicit ISO date required')
        return value


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    observations: list[Observation] = Field(max_length=50)


def admit(db, execution, evidence_id):
    """Caller owns the ingest transaction; material and work commit together."""
    execution._guard(db)
    project_id = execution.job['project_id']
    row = db.execute("SELECT 1 FROM studio_evidence_versions WHERE id=? AND project_id=? AND visibility='public'",
                     (evidence_id, project_id)).fetchone()
    if not row:
        raise ValueError('stage input outside public project')
    key = hashlib.sha256(json.dumps([project_id, 'public', evidence_id, POLICY]).encode()).hexdigest()
    db.execute("INSERT OR IGNORE INTO studio_evidence_tasks (id,project_id,job_id,evidence_version_id,policy,status) "
               "VALUES (?,?,?,?,?,'pending')", (key, project_id, execution.job['id'], evidence_id, POLICY))
    return key


def _input(execution, task):
    record = execution.store.evidence.get(task['project_id'], task['evidence_version_id'], include_internal=False)
    # A reference ID selects an exact immutable part. The host restores the
    # original text, not a model-transcribed quote. Location is not entailment.
    refs = {}
    for index, part in enumerate(record['parts']):
        key = hashlib.sha256(f"{record['id']}:{index}".encode()).hexdigest()
        refs[key] = dict(evidence_version_id=record['id'], part_index=index, start=0,
                         end=len(part['text']), quote=part['text'], locator=part['locator'])
    return dict(task_id=task['id'], evidence_version_id=record['id'], source_type=record['source_type'],
                read_scope=record['read_scope'], refs=refs)


def _withdrawn(db, task):
    return db.execute("SELECT 1 FROM studio_evidence_lifecycle WHERE project_id=? AND evidence_version_id=? "
                      "AND status='withdrawn'", (task['project_id'], task['evidence_version_id'])).fetchone() is not None


def _save(execution, task, status, result):
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        current = db.execute('SELECT status FROM studio_evidence_tasks WHERE id=? AND job_id=?',
                             (task['id'], execution.job['id'])).fetchone()
        if not current or current[0] != 'pending':
            raise RuntimeError('material stage already resolved or replaced')
        if _withdrawn(db, task):
            status, result = 'rejected', {'code': 'source_withdrawn'}
        if status == 'complete':
            dependency = execution.store.evidence.dependencies.track(task['project_id'], 'event-input:' + task['id'],
                'fact', [task['evidence_version_id']], db=db)
            result['input_status'] = dependency['status']
        db.execute('UPDATE studio_evidence_tasks SET status=?,result=? WHERE id=?',
                   (status, json.dumps(result, ensure_ascii=False, allow_nan=False), task['id']))


def run_pending(execution, extractor, *, limit=20):
    """Bounded processing using the same JobExecution as collection/review.

The extractor must journal external calls on that execution with task_id in
its request identity. Transport/unknown/lease errors propagate, never become
item-format errors. Invalid returned structure affects only that material.
"""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid material batch size')
    from project_watch.studio.material_ownership import MaterialExecution, project_guard

    with closing(execution.store._connect()) as db:
        execution._guard(db)
        tasks = [dict(row) for row in db.execute("SELECT * FROM studio_evidence_tasks WHERE job_id=? AND project_id=? "
            "AND policy=? AND status='pending' ORDER BY rowid LIMIT ?",
            (execution.job['id'], execution.job['project_id'], POLICY, limit))]
    for task in tasks:
        material = MaterialExecution(execution, task)
        with closing(execution.store._connect()) as db:
            material._guard(db)
            project_guard(db, task['project_id'], execution.job['id'])
            withdrawn = _withdrawn(db, task)
        if withdrawn:
            _save(execution, task, 'rejected', {'code': 'source_withdrawn'})
            continue
        source = _input(execution, task)
        response = extractor(source, material)
        try:
            proposed = Proposal.model_validate(response)
            observations = []
            for observation in proposed.observations:
                if any(ref not in source['refs'] for ref in observation.refs):
                    raise ValueError('reference_not_in_material')
                observations.append(dict(text=observation.text,
                    event_at=observation.event_at, refs=list(dict.fromkeys(observation.refs))))
        except (ValidationError, ValueError):
            _save(execution, task, 'rejected', {'code': 'invalid_observation'})
            continue
        used = {ref for observation in observations for ref in observation['refs']}
        _save(execution, task, 'complete', {'observations': observations,
            'refs': {ref: source['refs'][ref] for ref in used}, 'review_status': 'pending'})
    return len(tasks)
