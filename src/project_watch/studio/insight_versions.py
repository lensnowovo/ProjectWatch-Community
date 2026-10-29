"""Immutable object judgments and strict public-input publication fences."""
import json
from contextlib import closing

from project_watch.studio.dependencies import semantic
from project_watch.studio.dsh_bootstrap import digest
from project_watch.studio.material_ownership import project_guard
from project_watch.studio.store import now

SCHEMA = '''
CREATE TABLE IF NOT EXISTS studio_insight_versions (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 artifact_key TEXT NOT NULL, fingerprint TEXT NOT NULL, record TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(project_id,artifact_key,fingerprint));
CREATE TABLE IF NOT EXISTS studio_insight_heads (
 project_id TEXT NOT NULL, artifact_key TEXT NOT NULL,
 version_id TEXT NOT NULL REFERENCES studio_insight_versions(id),
 PRIMARY KEY(project_id,artifact_key));
CREATE TRIGGER IF NOT EXISTS immutable_insight_update BEFORE UPDATE ON studio_insight_versions
 BEGIN SELECT RAISE(ABORT,'insight version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_insight_delete BEFORE DELETE ON studio_insight_versions
 BEGIN SELECT RAISE(ABORT,'insight version is immutable'); END;
'''


def initialize(store):
    with closing(store._connect()) as db:
        db.executescript(SCHEMA)


def policy(pipeline):
    from project_watch.studio import dsh_judgment_review as review

    return digest(['fixed-object-insight-v1', getattr(pipeline.insight_generator, 'SYSTEM', ''),
        review.VERSION, review.SYSTEM, review.CONSISTENCY_SYSTEM, review.MAX_FIELDS,
        review.MAX_FIELD_CHARS, review.MAX_BATCHES, review.OUTPUT_TOKENS])


def relations(pipeline, project_id, object_id, *, db):
    return [r for r in pipeline.relationships.page(project_id, include_internal=False, db=db)
            if object_id in r.get('object_versions', {})]


def validate(pipeline, project_id, work, *, db):
    if work['model_binding'] != pipeline.model_binding() or work['analysis_policy'] != policy(pipeline):
        raise ValueError('insight model or policy changed')
    for oid, version in work['object_versions'].items():
        row = db.execute("SELECT version FROM studio_monitor_objects WHERE project_id=? AND id=? AND visibility='public'",
                         (project_id, oid)).fetchone()
        if not row or row[0] != version:
            raise ValueError('insight object version changed')
    if relations(pipeline, project_id, work['object']['id'], db=db) != work['relationships']:
        raise ValueError('insight relationship version changed')
    pipeline.store.event_revisions._check(db, project_id, work['event_versions'])
    for eid in work['evidence_ids']:
        row = db.execute("SELECT e.record,n.record,l.status,hl.status FROM studio_evidence_versions e "
            "JOIN studio_evidence_heads h ON h.project_id=e.project_id AND h.source_key=e.source_key "
            "JOIN studio_evidence_versions n ON n.id=h.evidence_version_id "
            "LEFT JOIN studio_evidence_lifecycle l ON l.project_id=e.project_id AND l.evidence_version_id=e.id "
            "LEFT JOIN studio_evidence_lifecycle hl ON hl.project_id=n.project_id AND hl.evidence_version_id=n.id "
            "WHERE e.project_id=? AND e.id=? AND e.visibility='public' AND n.visibility='public'", (project_id, eid)).fetchone()
        if not row or 'withdrawn' in (row[2], row[3]) or semantic(json.loads(row[0])) != semantic(json.loads(row[1])):
            raise ValueError('insight evidence version changed')


def current(store, project_id, object_id, *, db=None):
    if db is None:
        with closing(store._connect()) as connection:
            return current(store, project_id, object_id, db=connection)
    row = db.execute('SELECT v.record FROM studio_insight_heads h JOIN studio_insight_versions v ON v.id=h.version_id '
                     'WHERE h.project_id=? AND h.artifact_key=?', (project_id, 'insight:' + object_id)).fetchone()
    if not row:
        return None
    record = json.loads(row[0])
    dependency = store.evidence.dependencies.get(project_id, record['artifact_key'], db=db)
    return {**record, 'dependency': dependency, 'status': (dependency or {}).get('status', 'stale')}


def save(research, analysis, ctx, review):
    execution, store, work = research.execution, research.store, research.work
    pid, key = research.project_id, 'insight:' + work['object']['id']
    identity = digest([pid, key, work['fingerprint'], review['binding']])
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        project_guard(db, pid, execution.job['id'])
        validate(research.pipeline, pid, work, db=db)
        old = db.execute('SELECT record FROM studio_insight_versions WHERE project_id=? AND artifact_key=? AND fingerprint=?',
                         (pid, key, work['fingerprint'])).fetchone()
        deps = store.evidence.dependencies
        dependency = deps.track(pid, key, 'insight', work['evidence_ids'], db=db)
        dependency = deps.track_objects(pid, key, work['object_versions'], db=db)
        if dependency['status'] != 'current':
            raise ValueError('insight dependency is stale')
        store.event_revisions.bind(pid, key, work['event_versions'], db=db)
        previous = current(store, pid, work['object']['id'], db=db)
        if old:
            result = json.loads(old[0])
            if not previous or previous['id'] != result['id']:
                raise ValueError('insight replay cannot move current pointer backwards')
            return result
        result = dict(id=identity, artifact_key=key, fingerprint=work['fingerprint'], analysis=analysis,
            generated_at=now(), model='dsh-public', parent_version_id=previous['id'] if previous else None,
            input=work, numbered=ctx['numbered'], review=review, job_id=execution.job['id'],
            origin_job_id=research.origin_job_id, total=len(ctx['numbered']), counts={'N': len(ctx['numbered'])},
            model_binding=research.binding, publication_policy='fixed-object-insight-v1')
        db.execute('INSERT INTO studio_insight_versions VALUES (?,?,?,?,?,?)',
                   (identity, pid, key, work['fingerprint'], json.dumps(result, ensure_ascii=False), result['generated_at']))
        db.execute('INSERT INTO studio_insight_heads VALUES (?,?,?) ON CONFLICT(project_id,artifact_key) '
                   'DO UPDATE SET version_id=excluded.version_id', (pid, key, identity))
        primary = db.execute('SELECT object_id FROM studio_primary_objects WHERE project_id=?', (pid,)).fetchone()
        if primary and primary[0] == work['object']['id']:
            db.execute('INSERT INTO studio_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('insight:' + pid, json.dumps(result, ensure_ascii=False)))
        return result
