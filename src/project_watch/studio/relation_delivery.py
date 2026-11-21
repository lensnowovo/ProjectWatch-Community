"""Deliver public commercial assertions to standing monitoring, without model I/O.

Human changes and retired watches win. The mapping records only watches created
here, so source withdrawal cannot remove a manually managed relationship.
"""
import json
from contextlib import closing
from datetime import date

from project_watch.studio.business_identity import current_located_names, ownership_refs
from project_watch.studio.business_relations import project_relation
from project_watch.studio.monitoring_plan import PlanCapacityError, _norm

SCHEMA = '''CREATE TABLE IF NOT EXISTS studio_relation_delivery (
 project_id TEXT NOT NULL, peer_id TEXT NOT NULL, role TEXT NOT NULL,
 watch_id TEXT NOT NULL, lifecycle_version TEXT NOT NULL,
 PRIMARY KEY(project_id,peer_id,role));'''
LABELS = {'supplier': '供应商', 'customer': '客户', 'partner': '合作方',
          'pilot_partner': '试点合作方', 'investor': '投资方', 'competitor': '竞品'}


def _withdrawn(pipeline, project_id, artifact_key):
    """Whether any evidence behind an artifact was withdrawn (not merely revised)."""
    dependency = pipeline.evidence.dependencies.get(project_id, artifact_key) or {}
    ids = list(dependency.get('evidence_ids') or ())
    if not ids:
        return False
    with closing(pipeline.store._connect()) as db:
        return db.execute("SELECT 1 FROM studio_evidence_lifecycle WHERE project_id=? AND status='withdrawn' "
                          f"AND evidence_version_id IN ({','.join('?' * len(ids))}) LIMIT 1",
                          (project_id, *ids)).fetchone() is not None


def _awaiting_reverification(pipeline, project_id, fact):
    """A fact made stale by a revision, still inside its validity window and not withdrawn."""
    key = 'fact:' + fact['id']
    dependency = pipeline.evidence.dependencies.get(project_id, key) or {}
    day = date.today().isoformat()
    return (dependency.get('status') == 'stale'
            and (not fact.get('valid_from') or fact['valid_from'] <= day)
            and (not fact.get('valid_to') or fact['valid_to'] >= day)
            and not _withdrawn(pipeline, project_id, key))


def sync(pipeline, project_id, *, guard=None):
    store, plan = pipeline.store, pipeline.monitoring_plan
    project = store.get_project(project_id) or {}
    if not project.get('activated') or project.get('status') != 'active':
        return {'added': 0, 'retired': 0}
    generation = pipeline.evidence.dependencies.generation(project_id)
    with closing(store._connect()) as db:
        primary = db.execute('SELECT object_id FROM studio_primary_objects WHERE project_id=?', (project_id,)).fetchone()
        if not primary:
            return {'added': 0, 'retired': 0}
        rows = db.execute("SELECT record FROM studio_fact_records WHERE project_id=? AND visibility='public' "
                          "AND json_extract(record,'$.attributes.kind')='business_relation' ORDER BY id", (project_id,)).fetchall()
    groups, pending = {}, set()
    for row in rows:
        fact = json.loads(row[0])
        try:
            relation = project_relation(pipeline, project_id, fact, include_internal=False)
        except KeyError:
            continue
        # A located lead can be monitored before the commercial assertion is
        # established. This never changes the fact or its pending projection.
        allowed_gaps = {'original_passage', 'commercial_stage', 'explicit_actual_attribution'}
        if relation['subject_id'] != primary[0] or fact['basis'] != 'actual' or relation['role'] not in LABELS:
            continue
        key = (relation['peer_id'], relation['role'])
        gaps = set(relation['missing']) - allowed_gaps
        if not gaps:
            groups.setdefault(key, []).append(relation)
        elif gaps == {'stale_or_outside_validity'} and _awaiting_reverification(pipeline, project_id, fact):
            # Same rule as a stale relationship below: hold an existing watch, add none.
            pending.add(key)
    # Use the override-aware projection, not raw candidate rows. A candidate
    # needs a current located company identity, but not a proven competitor role.
    relationships = pipeline.relationships.page(project_id, include_internal=False)
    # A relationship waiting to be re-verified (an object gained a version, or its
    # evidence was revised) still holds an existing watch. Retirement is permanent,
    # so only a relationship that is gone, disputed or unrelated retires it.
    for row in relationships:
        # A stale row's support reads 'pending'; a dispute it had before stays a dispute.
        if (row['subject_id'] != primary[0] or 'disputed' in (row.get('support'), row.get('previous_support'))
                or row['relation_type'] == 'unrelated'):
            continue
        peer = pipeline.objects.get(project_id, row['peer_id'], include_internal=False)
        names, refs = current_located_names(store, project_id, peer, peer['refs'], include_internal=False)
        if not any(ownership_refs(peer['name'], peer['name'], r['quote'], names) is not None for r in refs):
            continue
        if row.get('status') != 'current' or row.get('scope_identity_changed'):
            if not _withdrawn(pipeline, project_id, 'relationship:' + row['scope_key']):
                pending.add((row['peer_id'], 'competitor'))
            continue
        groups.setdefault((peer['id'], 'competitor'), []).append({
            'peer_name': peer['name'], 'fact_id': row['scope_key'], 'source_kind': 'relationship',
            'support': 'pending'})
    result = {'added': 0, 'retired': 0}
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if guard:
            guard(db)
        db.execute(SCHEMA)
        project = store.get_project(project_id, db=db) or {}
        if not project.get('activated') or project.get('status') != 'active':
            return result
        # Pending relationship writes no longer bump the generation; compare the rows read.
        if (pipeline.evidence.dependencies._generation(db, project_id) != generation
                or pipeline.relationships.page(project_id, include_internal=False, db=db) != relationships):
            return {**result, 'superseded': True}
        mappings = { (r['peer_id'], r['role']): dict(r) for r in db.execute(
            'SELECT * FROM studio_relation_delivery WHERE project_id=?', (project_id,)) }
        watches = [dict(r) for r in db.execute('SELECT * FROM studio_watchpoints WHERE project_id=?', (project_id,))]
        by_id = {r['id']: r for r in watches}
        for key, mapped in mappings.items():
            watch = by_id.get(mapped['watch_id'])
            if (key in groups or key in pending or not watch or watch['status'] == 'retired'
                    or plan._owned_by_people(watch)):
                continue
            plan.update(watch['id'], {'status': 'retired'}, actor='system', reason='原关联依据已失效',
                        expected_lifecycle_version=str(watch['lifecycle_version']), db=db)
            result['retired'] += 1
        for (peer_id, role), relations in groups.items():
            target = relations[0]['peer_name']
            if (peer_id, role) in mappings or any(
                    w.get('scope_kind') == 'relation' and _norm(w.get('relation_target', '')) == _norm(target)
                    and w.get('relation_type') == role for w in watches):
                continue  # Includes retired watches: never silently restore them.
            label = LABELS[role]
            fields = {'text': f'关注{target}的{label}动态', 'label': target,
                'axis': 'peers' if role == 'competitor' else 'customer' if role == 'customer' else 'partners', 'scope_kind': 'relation',
                'relation_type': role, 'relation_target': target, 'objects': [target],
                'queries': [target], 'sources': '; '.join(r.get('source_kind', 'fact') + ':' + r['fact_id'] for r in relations),
                'reason': (f'已收录材料记载其为{label}' if any(r['support'] == 'source_assertion' for r in relations)
                           else f'{label}线索，跟踪核实关系'), 'tier': 'background', 'cadence': 'medium'}
            try:
                item = plan.add(project_id, fields,
                    actor='system', origin='system', visibility='public', reason='从有来源的商业关系接入监控',
                    evidence=[{'kind': r.get('source_kind', 'fact'), 'id': r['fact_id']} for r in relations], db=db)
            except PlanCapacityError:
                # Keep the source fact and retry local admission when room exists;
                # never abort extraction or other projects' scheduling.
                result['deferred'] = result.get('deferred', 0) + 1
                continue
            db.execute('INSERT INTO studio_relation_delivery VALUES (?,?,?,?,?)',
                       (project_id, peer_id, role, item['id'], str(item['lifecycle_version'])))
            result['added'] += 1
            watches.append(item)
    return result
