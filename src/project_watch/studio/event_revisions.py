"""Reversible event membership history; never a semantic admission bypass."""
import hashlib
import json
from contextlib import closing, contextmanager

from project_watch.studio.dependencies import DependencyStore
from project_watch.studio.store import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_event_revisions (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 event_id TEXT NOT NULL REFERENCES studio_events(id), parent_id TEXT,
 record TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS studio_event_revision_heads (
 event_id TEXT PRIMARY KEY REFERENCES studio_events(id), revision_id TEXT NOT NULL REFERENCES studio_event_revisions(id));
CREATE TABLE IF NOT EXISTS studio_event_dependency_links (
 project_id TEXT NOT NULL, artifact_key TEXT NOT NULL, event_id TEXT NOT NULL,
 revision_id TEXT NOT NULL REFERENCES studio_event_revisions(id),
 PRIMARY KEY(project_id,artifact_key,event_id),
 FOREIGN KEY(project_id,artifact_key) REFERENCES studio_analysis_dependencies(project_id,artifact_key));
CREATE TABLE IF NOT EXISTS studio_event_merge_receipts (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 before_state TEXT NOT NULL, after_state TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER IF NOT EXISTS immutable_event_revision_update BEFORE UPDATE ON studio_event_revisions
 BEGIN SELECT RAISE(ABORT,'event revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_event_revision_delete BEFORE DELETE ON studio_event_revisions
 BEGIN SELECT RAISE(ABORT,'event revision is immutable'); END;
"""
FIELDS = ('title', 'summary', 'impact', 'category', 'importance', 'event_at')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def snapshot(db, project_id, event_id):
    row = db.execute('SELECT * FROM studio_events WHERE project_id=? AND id=?', (project_id, event_id)).fetchone()
    if row is None:
        raise KeyError('event not found in project')
    members = [r[0] for r in db.execute('SELECT id FROM studio_items WHERE project_id=? AND event_id=? ORDER BY id',
                                       (project_id, event_id))]
    # Items are mutable legacy projections. Preserve their evidence-bearing
    # values in the immutable event revision as well as membership IDs.
    sources = [dict(r) for r in db.execute("SELECT id,title,url,source,snippet,published_at,body,body_status,extra,relevant,status='dismissed' AS withdrawn "
        'FROM studio_items WHERE project_id=? AND event_id=? ORDER BY id', (project_id, event_id))]
    return dict(project_id=project_id, event_id=event_id, fields={k: row[k] for k in FIELDS},
                withdrawn=row['status'] == 'dismissed', members=members, sources=sources)


def record(db, project_id, event_id):
    """Must be called inside the event writer's transaction and lease guard."""
    data = snapshot(db, project_id, event_id)
    previous = db.execute('SELECT revision_id FROM studio_event_revision_heads WHERE event_id=?', (event_id,)).fetchone()
    parent = previous[0] if previous else None
    if parent:
        prior = db.execute('SELECT record FROM studio_event_revisions WHERE id=?', (parent,)).fetchone()
        if prior[0] == _encoded(data):
            return parent
    # Parent participates so undo can restore content without rewinding history.
    revision = hashlib.sha256(_encoded([parent, data]).encode()).hexdigest()
    db.execute('INSERT INTO studio_event_revisions VALUES (?,?,?,?,?,?)',
               (revision, project_id, event_id, parent, _encoded(data), now()))
    db.execute('INSERT INTO studio_event_revision_heads VALUES (?,?) ON CONFLICT(event_id) DO UPDATE SET revision_id=excluded.revision_id',
               (event_id, revision))
    # A newly admitted event expands the analysis input set even when its
    # sources were already saved. Legacy head seeding conservatively bumps too.
    db.execute('INSERT INTO studio_dependency_state VALUES (?,1) ON CONFLICT(project_id) DO UPDATE SET generation=generation+1',
               (project_id,))
    generation = DependencyStore._generation(db, project_id)
    affected = db.execute('SELECT d.artifact_key,d.record FROM studio_analysis_dependencies d '
        'JOIN studio_event_dependency_links l ON l.project_id=d.project_id AND l.artifact_key=d.artifact_key '
        'WHERE l.project_id=? AND l.event_id=? AND l.revision_id!=?', (project_id, event_id, revision)).fetchall()
    for row in affected:
        changed = {**json.loads(row['record']), 'status': 'stale', 'generation': generation,
                   'stale_reason': 'event_revised', 'invalidated_at': now()}
        db.execute("UPDATE studio_analysis_dependencies SET status='stale',generation=?,record=? WHERE project_id=? AND artifact_key=?",
                   (generation, _encoded(changed), project_id, row['artifact_key']))
        db.execute("INSERT INTO studio_rebuild_requests VALUES (?,?,?,'queued') ON CONFLICT(project_id,artifact_key) "
                   "DO UPDATE SET generation=excluded.generation,status='queued'", (project_id, row['artifact_key'], generation))
    DependencyStore._change(db, project_id, 'public', 'event_revised', {'event_id': event_id, 'revision_id': revision, 'generation': generation})
    return revision


def previous_memberships(db, project_id, item_ids):
    """Capture legacy heads before a writer moves accepted source membership."""
    affected = set()
    for item_id in item_ids:
        row = db.execute('SELECT event_id FROM studio_items WHERE id=? AND project_id=?', (item_id, project_id)).fetchone()
        if row and row[0]:
            affected.add(row[0])
    for event_id in affected:
        record(db, project_id, event_id)
    return affected


@contextmanager
def changing_items(db, item_ids):
    """Legacy item writers preserve event evidence before and after mutation."""
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    affected = set()
    for item_id in item_ids:
        row = db.execute('SELECT project_id,event_id FROM studio_items WHERE id=?', (item_id,)).fetchone()
        if row and row['event_id']:
            affected.add((row['project_id'], row['event_id']))
    for project_id, event_id in affected:
        record(db, project_id, event_id)
    yield
    for project_id, event_id in affected:
        record(db, project_id, event_id)


class EventRevisions:
    def __init__(self, store):
        self.store = store
        with closing(store._connect()) as db:
            db.executescript(SCHEMA)

    def bind(self, project_id, artifact_key, versions, *, db):
        if not versions or len(versions) > 100:
            raise ValueError('bounded event dependency required')
        artifact = db.execute('SELECT record FROM studio_analysis_dependencies WHERE project_id=? AND artifact_key=?',
                              (project_id, artifact_key)).fetchone()
        if not artifact:
            raise KeyError('artifact dependency required')
        self._check(db, project_id, versions)
        for event_id, revision in versions.items():
            found = db.execute('SELECT 1 FROM studio_event_revisions r JOIN studio_event_revision_heads h ON h.revision_id=r.id '
                'WHERE r.project_id=? AND r.event_id=? AND r.id=?', (project_id, event_id, revision)).fetchone()
            if not found:
                raise ValueError('event version is not current in project')
        db.execute('DELETE FROM studio_event_dependency_links WHERE project_id=? AND artifact_key=?', (project_id, artifact_key))
        db.executemany('INSERT INTO studio_event_dependency_links VALUES (?,?,?,?)',
                       [(project_id, artifact_key, event_id, revision) for event_id, revision in versions.items()])
        data = {**json.loads(artifact[0]), 'event_versions': versions}
        db.execute('UPDATE studio_analysis_dependencies SET record=? WHERE project_id=? AND artifact_key=?',
                   (_encoded(data), project_id, artifact_key))

    def _check(self, db, project_id, expected):
        for event_id, revision in expected.items():
            row = db.execute('SELECT r.record FROM studio_event_revision_heads h JOIN studio_event_revisions r '
                'ON r.id=h.revision_id WHERE h.event_id=? AND r.project_id=? AND r.id=?',
                (event_id, project_id, revision)).fetchone()
            if not row or json.loads(row[0]) != snapshot(db, project_id, event_id):
                raise ValueError('event changed since review')

    def merge(self, execution, target, expected):
        """Host-approved current events only; no new assertions are invented."""
        if target not in expected or not 2 <= len(expected) <= 20:
            raise ValueError('merge requires target and bounded source events')
        project_id = execution.job['project_id']
        key = hashlib.sha256(_encoded([project_id, target, expected]).encode()).hexdigest()
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            execution._guard(db)
            old = db.execute('SELECT * FROM studio_event_merge_receipts WHERE id=?', (key,)).fetchone()
            if old:
                if old['undone']:
                    raise ValueError('merge was undone; review current versions')
                self._check(db, project_id, json.loads(old['after_state']))
                return key
            self._check(db, project_id, expected)
            if any(snapshot(db, project_id, event_id)['withdrawn'] for event_id in expected):
                raise ValueError('withdrawn event cannot merge')
            before = {'versions': expected, 'target': target, 'statuses': {event_id: db.execute(
                'SELECT status FROM studio_events WHERE id=?', (event_id,)).fetchone()[0] for event_id in expected}}
            for event_id in expected:
                if event_id != target:
                    db.execute('UPDATE studio_items SET event_id=? WHERE project_id=? AND event_id=?', (target, project_id, event_id))
                    db.execute("UPDATE studio_events SET status='dismissed' WHERE project_id=? AND id=?", (project_id, event_id))
            after = {event_id: record(db, project_id, event_id) for event_id in expected}
            db.execute('INSERT INTO studio_event_merge_receipts VALUES (?,?,?,?,0)',
                       (key, project_id, _encoded(before), _encoded(after)))
            return key

    def undo_merge(self, execution, merge_id):
        """Split to exact pre-merge memberships only if no intervening edits."""
        project_id = execution.job['project_id']
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            execution._guard(db)
            merge = db.execute('SELECT * FROM studio_event_merge_receipts WHERE id=? AND project_id=?', (merge_id, project_id)).fetchone()
            if not merge:
                raise KeyError('merge not found in project')
            if merge['undone']:
                return
            self._check(db, project_id, json.loads(merge['after_state']))
            saved = json.loads(merge['before_state'])
            before = saved['versions']
            for event_id, revision in before.items():
                old = json.loads(db.execute('SELECT record FROM studio_event_revisions WHERE id=?', (revision,)).fetchone()[0])
                if event_id != saved['target']:
                    db.execute("UPDATE studio_events SET status=?,updated_at=? WHERE id=? AND project_id=?",
                               (saved['statuses'][event_id], now(), event_id, project_id))
                db.executemany('UPDATE studio_items SET event_id=? WHERE id=? AND project_id=?',
                               [(event_id, item, project_id) for item in old['members']])
            for event_id in before:
                record(db, project_id, event_id)
            db.execute('UPDATE studio_event_merge_receipts SET undone=1 WHERE id=?', (merge_id,))
