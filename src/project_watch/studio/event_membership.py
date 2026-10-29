"""Automatic membership receipts and local, version-fenced user correction."""
import hashlib
import json
from contextlib import closing

from project_watch.studio.event_revisions import FIELDS, _encoded, record
from project_watch.studio.material_ownership import project_guard
from project_watch.studio.store import now


def before_attach(db, project_id, target, item_ids, split_event=None):
    moved = []
    versions = {target: record(db, project_id, target)}
    for item_id in sorted(set(item_ids)):
        row = db.execute('SELECT * FROM studio_items WHERE id=? AND project_id=?', (item_id, project_id)).fetchone()
        if row is None:
            raise ValueError('source is not in project')
        if row['event_id'] == target:
            continue
        moved.append({'id': item_id, 'event_id': row['event_id']})
        if row['event_id']:
            versions[row['event_id']] = record(db, project_id, row['event_id'])
        elif split_event is None:
            split_event = {**{k: row[k] for k in ('title', 'summary', 'impact', 'importance')},
                           'category': db.execute('SELECT category FROM studio_events WHERE id=?', (target,)).fetchone()[0],
                           'event_at': row['published_at']}
    return {'kind': 'attachment', 'target': target, 'versions': versions,
            'moved': moved, 'split_event': split_event}


def after_attach(db, project_id, saved):
    after = {event: record(db, project_id, event) for event in saved['versions']}
    if not saved['moved']:
        return None
    key = hashlib.sha256(_encoded([project_id, saved]).encode()).hexdigest()
    db.execute('INSERT INTO studio_event_merge_receipts VALUES (?,?,?,?,0)',
               (key, project_id, _encoded(saved), _encoded(after)))
    return key


def undo_attachment(store, db, project_id, merge, saved):
    # _check has already compared every affected immutable revision with live
    # fields and evidence. Restore only this write, never subsequent human text.
    target = saved['target']
    old = json.loads(db.execute('SELECT record FROM studio_event_revisions WHERE id=?',
                               (saved['versions'][target],)).fetchone()[0])
    db.execute('UPDATE studio_events SET ' + ','.join(k + '=?' for k in FIELDS) +
               ',updated_at=? WHERE id=? AND project_id=?',
               (*[old['fields'][k] for k in FIELDS], now(), target, project_id))
    unattached = []
    for item in saved['moved']:
        if item['event_id']:
            db.execute('UPDATE studio_items SET event_id=? WHERE id=? AND project_id=?',
                       (item['event_id'], item['id'], project_id))
        else:
            unattached.append(item['id'])
    if unattached:
        # A newly discovered source has no prior event to restore. Materialize
        # its already-admitted pre-attachment projection so it stays visible.
        store._insert_event(db, project_id, saved['split_event'], unattached)
    for event in saved['versions']:
        record(db, project_id, event)
    db.execute('UPDATE studio_event_merge_receipts SET undone=1 WHERE id=?', (merge['id'],))


def receipts(store, project_id, event_id):
    with closing(store._connect()) as db:
        if not db.execute('SELECT 1 FROM studio_events WHERE id=? AND project_id=?', (event_id, project_id)).fetchone():
            raise KeyError('event not found in project')
        rows = db.execute('SELECT * FROM studio_event_merge_receipts WHERE project_id=? ORDER BY rowid DESC', (project_id,))
        result = []
        for row in rows:
            before, after = json.loads(row['before_state']), json.loads(row['after_state'])
            if event_id not in after:
                continue
            result.append({'id': row['id'], 'target': before['target'], 'undone': bool(row['undone']),
                           'kind': before.get('kind', 'merge'), 'after_versions': after})
            if len(result) == 50:
                break
        return result


def correct(store, project_id, receipt_id):
    """Explicit user write; no new worker, lease impersonation or paid request."""
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        merge = db.execute('SELECT * FROM studio_event_merge_receipts WHERE id=? AND project_id=?',
                           (receipt_id, project_id)).fetchone()
        if not merge:
            raise KeyError('merge not found in project')
        if merge['undone']:
            return {'id': receipt_id, 'undone': True}
        project_guard(db, project_id, '')
        if db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND status='running' LIMIT 1", (project_id,)).fetchone():
            raise ValueError('wait for the running project job before correcting membership')
        store.event_revisions._undo(db, project_id, merge)
        return {'id': receipt_id, 'undone': True}
