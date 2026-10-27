"""Evidence-bound public query scopes, not company ownership assertions."""
import hashlib
import json
import uuid
from contextlib import closing

from project_watch.studio.monitoring_contracts import LocatedRef
from project_watch.studio.store import now

# Adapter contracts, not industry words or model-authored applicability rules.
ADAPTERS = {
    'openalex_affiliation': ('paper', True), 'openalex_author': ('paper', True),
    'openalex_topic': ('paper', False), 'patent_search': ('patent', True),
    'clinical_sponsor': ('trial', True), 'clinical_term': ('trial', True),
    'cn_regulatory': ('trial', True),
}
SCHEMA = '''CREATE TABLE IF NOT EXISTS studio_collection_targets (
    project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL,
    record TEXT NOT NULL, PRIMARY KEY(project_id,id))'''
SOURCE_ADAPTERS = frozenset(ADAPTERS) | {'search', 'site', 'cninfo'}
DECISION_SCHEMA = '''CREATE TABLE IF NOT EXISTS studio_collection_source_decisions (
    project_id TEXT NOT NULL REFERENCES projects(id), adapter TEXT NOT NULL,
    version INTEGER NOT NULL, record TEXT NOT NULL, PRIMARY KEY(project_id,adapter,version))'''


class DecisionConflict(ValueError):
    """A stale editor must reload rather than overwrite another decision."""


def source_decisions(store, project_id):
    """Read the latest human selection; enabled is not a capability assertion."""
    with closing(store._connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_collection_source_decisions'").fetchone():
            return {}
        rows = db.execute('''SELECT adapter,record FROM studio_collection_source_decisions d
            WHERE project_id=? AND version=(SELECT MAX(version) FROM studio_collection_source_decisions h
            WHERE h.project_id=d.project_id AND h.adapter=d.adapter) ORDER BY adapter''', (project_id,))
        return {row[0]: json.loads(row[1]) for row in rows}


def decide_source(store, project_id, *, adapter, selection, reason, expected_version):
    """Local owner's explicit decision, append-only and optimistic-versioned.

    No DSH tool exposes this operation. The recorded human reason is the basis
    of an exclusion, not invented external evidence or a source success receipt.
    It does not enqueue work, alter pause state, or retry a request.
    """
    if adapter not in SOURCE_ADAPTERS or selection not in ('enabled', 'disabled', 'not_applicable'):
        raise ValueError('invalid source selection')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 600:
        raise ValueError('source selection requires a reason')
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('invalid decision version')
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT id FROM projects WHERE id=?', (project_id,)).fetchone():
            raise KeyError(project_id)
        db.execute(DECISION_SCHEMA)
        row = db.execute('SELECT version,record FROM studio_collection_source_decisions '
                         'WHERE project_id=? AND adapter=? ORDER BY version DESC LIMIT 1', (project_id, adapter)).fetchone()
        version = row[0] if row else 0
        # An identical repeat of the successful write is idempotent, including
        # a client whose first response was lost. It never erases later edits.
        if row and version == expected_version + 1:
            old = json.loads(row[1])
            if old['selection'] == selection and old['reason'] == reason.strip():
                return old
        if version != expected_version:
            raise DecisionConflict('来源选择已变化，请刷新后再保存')
        record = dict(id='source-decision:' + uuid.uuid4().hex, adapter=adapter, version=version + 1,
                      selection=selection, reason=reason.strip(), actor='human', basis='human_confirmation',
                      created_at=now(), schema_version=1)
        db.execute('INSERT INTO studio_collection_source_decisions VALUES (?,?,?,?)',
                   (project_id, adapter, record['version'], json.dumps(record, ensure_ascii=False)))
        return record


def require_source_enabled(store, project_id, adapter):
    decision = source_decisions(store, project_id).get(adapter)
    if decision and decision['selection'] != 'enabled':
        raise ValueError('source excluded by owner decision')


def retain(research, *, adapter, query, reason, ref):
    """Only a public, exact source excerpt can supply a specialist search term.

    This registers a search hypothesis. Relevance, entity disambiguation and
    company attribution still require reviewing the resulting evidence.
    """
    if adapter not in ADAPTERS or not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
        raise ValueError('invalid professional source query')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 600:
        raise ValueError('source query requires reason')
    query = query.strip()
    ref = LocatedRef.model_validate(ref)
    research.pipeline.evidence.resolve(research.project_id, ref, include_internal=False)
    if query not in ref.quote:
        raise ValueError('query must occur in its cited public excerpt')
    identity = hashlib.sha256(json.dumps([adapter, query], ensure_ascii=False).encode()).hexdigest()
    category, historical = ADAPTERS[adapter]
    record = dict(id=identity, adapter=adapter, query=query, category=category, historical=historical,
                  reason=reason.strip(), ref=ref.model_dump(), status='active', created_at=now(),
                  basis='public_query_candidate', schema_version=1)
    execution = research.execution
    with research.store.write_scope(execution._guard), closing(research.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        db.execute(SCHEMA)
        # Do not revive a disabled scope or overwrite its original provenance.
        db.execute('INSERT OR IGNORE INTO studio_collection_targets VALUES (?,?,?)',
                   (research.project_id, identity, json.dumps(record, ensure_ascii=False)))
        saved = json.loads(db.execute('SELECT record FROM studio_collection_targets WHERE project_id=? AND id=?',
                                     (research.project_id, identity)).fetchone()[0])
    if saved['status'] != 'active':
        raise ValueError('professional query scope is disabled')
    research.pipeline.evidence.resolve(research.project_id, LocatedRef.model_validate(saved['ref']), include_internal=False)
    return saved


def targets(research):
    """No raw internal plan, inferred clinical label or silent invalid-ref drop."""
    with closing(research.store._connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_collection_targets'").fetchone():
            return []
        rows = [json.loads(r[0]) for r in db.execute(
            'SELECT record FROM studio_collection_targets WHERE project_id=? ORDER BY id', (research.project_id,))]
    for row in rows:
        if row.get('schema_version') != 1 or row.get('adapter') not in ADAPTERS:
            raise ValueError('invalid stored professional query scope')
        try:
            ref = LocatedRef.model_validate(row['ref'])
            research.pipeline.evidence.resolve(research.project_id, ref, include_internal=False)
            if row['query'] not in ref.quote:
                raise ValueError('query basis changed')
        except (KeyError, ValueError):
            row.update(capability='needs_scope', reason='Public query basis unavailable')
        else:
            row['capability'] = 'available' if row['status'] == 'active' else 'disabled'
    return rows
