"""Immutable raw-material inputs; analysis generation has no collection capability.

Snapshots reuse the immutable version store but never advance judgment heads.
They reference original evidence, not an additional mutable facts database.
This boundary produces drafts, not reviewed publications.
"""
import json
from contextlib import closing

from project_watch.studio.http import HttpError
from project_watch.studio.store import digest, now

KEY = 'material-snapshot-v1'


def _permitted(db, project_id, version_id):
    row = db.execute(
        'SELECT e.record,e.visibility,l.status AS lifecycle,n.visibility AS head_visibility,hl.status AS head_lifecycle '
        'FROM studio_evidence_versions e '
        'JOIN studio_evidence_heads h ON h.project_id=e.project_id AND h.source_key=e.source_key '
        'JOIN studio_evidence_versions n ON n.id=h.evidence_version_id AND n.project_id=e.project_id '
        'LEFT JOIN studio_evidence_lifecycle l ON l.project_id=e.project_id AND l.evidence_version_id=e.id '
        'LEFT JOIN studio_evidence_lifecycle hl ON hl.project_id=n.project_id AND hl.evidence_version_id=n.id '
        'WHERE e.project_id=? AND e.id=?', (project_id, version_id)).fetchone()
    if not row or row['visibility'] != 'public' or row['head_visibility'] != 'public':
        raise ValueError('snapshot material is outside permitted public scope')
    if 'withdrawn' in (row['lifecycle'], row['head_lifecycle']):
        raise ValueError('snapshot material withdrawn')
    evidence = json.loads(row['record'])
    # Historical snapshots must still honor later item exclusions. Match the
    # stable source key as well as original observations, not only today's head.
    excluded = db.execute(
        "SELECT 1 FROM studio_items WHERE project_id=? AND (status='dismissed' OR relevant=0) "
        "AND (key=? OR 'item:'||id=?)", (project_id, evidence['source_key'], evidence['source_key'])).fetchone()
    if not excluded and db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_collection_observations'").fetchone():
        excluded = db.execute(
            "SELECT 1 FROM studio_collection_observations o JOIN studio_items i ON i.id=o.item_id "
            "WHERE i.project_id=? AND o.evidence_version_id=? AND (i.status='dismissed' OR i.relevant=0)",
            (project_id, version_id)).fetchone()
    if excluded:
        raise ValueError('snapshot material excluded')
    return evidence


def _guard(execution, db):
    # Another job's unknown request is accounting, not a reason to stop (dispatch.py).
    execution._guard(db)
    project = db.execute('SELECT status,consent_at FROM studio_projects WHERE project_id=?',
                         (execution.job['project_id'],)).fetchone()
    if not project or project[0] != 'active':
        raise HttpError('project_paused')
    if not project[1]:
        raise HttpError('project_consent_required')


def capture(execution):
    """Freeze current public originals, scope and collection receipts atomically.

    Replay returns the same manifest. Newly acquired material needs a new batch;
    it cannot silently change the input of an already-started analysis.
    """
    pid, jid, store = execution.job['project_id'], execution.job['id'], execution.store
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)  # Local persistence does not spend or reset provider budgets.
        key = KEY + ':' + jid
        previous = db.execute('SELECT record FROM studio_insight_versions WHERE project_id=? AND artifact_key=?',
                              (pid, key)).fetchone()
        if previous:
            return json.loads(previous[0])
        materials, exclusions = [], []
        for row in db.execute('SELECT evidence_version_id FROM studio_evidence_heads WHERE project_id=? ORDER BY source_key', (pid,)):
            eid = row[0]
            try:
                evidence = _permitted(db, pid, eid)
            except ValueError:
                exclusions.append(dict(evidence_version_id=eid, reason='not_permitted'))
                continue
            materials.append(dict(evidence_version_id=eid, fingerprint=digest(evidence),
                source_key=evidence['source_key'], classification='candidate',
                source_type=evidence['source_type'], read_scope=evidence['read_scope']))
        baseline = {r[0]: r[1] for r in db.execute(
            'SELECT artifact_key,version_id FROM studio_insight_heads WHERE project_id=? ORDER BY artifact_key', (pid,))}
        objects = {r[0]: r[1] for r in db.execute(
            "SELECT id,version FROM studio_monitor_objects WHERE project_id=? AND visibility='public' ORDER BY id", (pid,))}
        ledgers = [dict(stage=r[0], fingerprint=digest(json.loads(r[1]))) for r in db.execute(
            "SELECT stage,data FROM studio_job_checkpoints WHERE job_id=? AND "
            "(stage='collection_round_v1' OR stage LIKE 'collection_extra:%') ORDER BY stage", (jid,))]
        value = dict(protocol=1, project_id=pid, batch_id=jid, materials=materials,
                     exclusions=exclusions, baseline_analysis=baseline, object_versions=objects,
                     collection_receipts=ledgers, visibility='public')
        signature = digest(value)
        value.update(id=digest([KEY, pid, jid, signature]), fingerprint=signature, created_at=now())
        db.execute('INSERT INTO studio_insight_versions VALUES (?,?,?,?,?,?)',
            (value['id'], pid, key, signature, json.dumps(value, ensure_ascii=False), value['created_at']))
        return value


def read(store, project_id, snapshot_id):
    """Resolve every original part, rechecking disclosure; no prefix clipping."""
    with closing(store._connect()) as db:
        db.execute('BEGIN')
        row = db.execute('SELECT record FROM studio_insight_versions WHERE project_id=? AND id=? AND artifact_key LIKE ?',
                         (project_id, snapshot_id, KEY + ':%')).fetchone()
        if not row:
            raise KeyError('snapshot not found in project')
        value = json.loads(row[0])
        originals = []
        for material in value['materials']:
            evidence = _permitted(db, project_id, material['evidence_version_id'])
            if digest(evidence) != material['fingerprint']:
                raise ValueError('snapshot evidence fingerprint changed')
            originals.append({**material, 'evidence': evidence})
        return {**value, 'materials': originals}


def derive_permitted(execution, snapshot_id):
    """Explicit local successor retaining only still-permitted original versions.

    No current-head replacement or recollection; the old manifest stays intact.
    A successor is not permission to retry an unknown analysis request.
    """
    pid, jid = execution.job['project_id'], execution.job['id']
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        row = db.execute('SELECT record FROM studio_insight_versions WHERE project_id=? AND id=? AND artifact_key LIKE ?',
                         (pid, snapshot_id, KEY + ':%')).fetchone()
        if not row:
            raise KeyError('snapshot not found in project')
        old = json.loads(row[0])
        materials, exclusions = [], list(old['exclusions'])
        for material in old['materials']:
            try:
                evidence = _permitted(db, pid, material['evidence_version_id'])
            except ValueError:
                exclusions.append(dict(evidence_version_id=material['evidence_version_id'], reason='not_permitted'))
                continue
            if digest(evidence) != material['fingerprint']:
                raise ValueError('snapshot evidence fingerprint changed')
            materials.append(material)
        value = {k: v for k, v in old.items() if k not in ('id', 'fingerprint', 'created_at')}
        value.update(materials=materials, exclusions=exclusions, parent_snapshot_id=snapshot_id,
                     derivation='permission_recheck', derivation_job_id=jid)
        signature = digest(value)
        key = KEY + ':' + jid + ':permitted:' + snapshot_id
        existing = db.execute('SELECT record FROM studio_insight_versions WHERE project_id=? AND artifact_key=? AND fingerprint=?',
                              (pid, key, signature)).fetchone()
        if existing:
            return json.loads(existing[0])
        value.update(id=digest([key, pid, signature]), fingerprint=signature, created_at=now())
        db.execute('INSERT INTO studio_insight_versions VALUES (?,?,?,?,?,?)',
                   (value['id'], pid, key, signature, json.dumps(value, ensure_ascii=False), value['created_at']))
        return value
