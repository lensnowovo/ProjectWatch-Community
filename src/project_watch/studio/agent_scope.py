"""Project-scoped public monitoring changes, no research or provider invocation."""
import hashlib
import json
import uuid
from contextlib import closing

from project_watch.studio.application import AccessDenied, ProjectMissing, VersionConflict
from project_watch.studio.store import now


def scope_version(db, project_id, internal):
    rows = [dict(row) for row in db.execute('SELECT * FROM studio_watchpoints WHERE project_id=? ORDER BY id', (project_id,))
            if internal or row['visibility'] == 'public']
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def read_scope(service, plan, project_id, token):
    with closing(service.store._connect()) as db:
        db.execute('BEGIN')
        grant = service._authorize(db, token, project_id, 'read')
        items = [item for item in plan.monitor.watchpoints(project_id, db=db)
                 if grant['allow_internal'] or item.get('visibility') == 'public']
        version = scope_version(db, project_id, grant['allow_internal'])
        project = service.project(project_id, db=db)
    return {**project, 'items': items, 'scope_version': version}


def write_scope(service, plan, project_id, body, token):
    payload_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    with closing(service.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        grant = service._authorize(db, token, project_id, 'write')
        actor = grant['id']
        prior = db.execute('SELECT * FROM studio_operations WHERE instance_id=? AND project_id=? AND actor=? AND idempotency_key=?',
                           (service.instance_id, project_id, actor, body.idempotency_key)).fetchone()
        if prior:
            if prior['payload_hash'] != payload_hash:
                raise VersionConflict('idempotency key already used with another payload')
            return json.loads(prior['receipt'])
        row = db.execute('SELECT config_version FROM studio_projects WHERE project_id=?', (project_id,)).fetchone()
        if not row:
            raise ProjectMissing('project not found')
        if row[0] != body.expected_version:
            raise VersionConflict('project changed; read latest version')
        if scope_version(db, project_id, grant['allow_internal']) != body.expected_scope_version:
            raise VersionConflict('monitoring scope changed; read latest scope')
        if body.watch_id:
            current = db.execute('SELECT visibility FROM studio_watchpoints WHERE id=? AND project_id=?',
                                 (body.watch_id, project_id)).fetchone()
            if not current:
                raise ProjectMissing('scope item not in project')
            if current[0] != 'public' and not grant['allow_internal']:
                raise AccessDenied('internal scope requires internal grant')
            item = plan.update(body.watch_id, body.item.model_dump(), actor='agent', reason=body.item.reason, db=db)
        else:
            item = plan.add(project_id, body.item.model_dump(), actor='agent', origin='draft',
                            visibility='public', reason=body.item.reason, db=db)
        db.execute('UPDATE studio_projects SET config_version=config_version+1 WHERE project_id=?', (project_id,))
        version = db.execute('SELECT config_version FROM studio_projects WHERE project_id=?', (project_id,)).fetchone()[0]
        result = {'instance_id': service.instance_id, 'schema_version': 1, 'project_id': project_id,
                  'operation_id': uuid.uuid4().hex, 'saved': True, 'version': version,
                  'watch_id': item['id'], 'job_status': 'not_requested', 'actor': actor}
        db.execute('INSERT INTO studio_operations VALUES (?,?,?,?,?,?,?,?)',
                   (result['operation_id'], project_id, service.instance_id, actor, body.idempotency_key,
                    payload_hash, json.dumps(result), now()))
        return result
