"""Neutral cited observation records and durable human feedback, not business scores."""

import hashlib
import json
import uuid
from contextlib import closing
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from project_watch.studio.application import ProjectMissing, VersionConflict
from project_watch.studio.event_state import timeline
from project_watch.studio.milestones import read as milestones
from project_watch.studio.store import now

SCHEMA = '''CREATE TABLE IF NOT EXISTS studio_concern_feedback (
 project_id TEXT NOT NULL REFERENCES projects(id), concern_id TEXT NOT NULL,
 version INTEGER NOT NULL, record TEXT NOT NULL, PRIMARY KEY(project_id,concern_id,version)
);'''


class ConcernFeedback(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    concern_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    status: Literal['open', 'resolved', 'dismissed']
    reason: str = Field(min_length=1, max_length=500)
    expected_version: Annotated[StrictInt, Field(ge=1)]
    idempotency_key: str = Field(min_length=1, max_length=100)


class ConcernStore:
    def __init__(self, service):
        self.service, self.store = service, service.store
        with closing(self.store._connect()) as db, db:
            db.executescript(SCHEMA)

    def read(self, project_id, *, include_internal=True):
        views = [('event', timeline(self.service, project_id, include_internal=include_internal)),
                 ('milestone', milestones(self.service, project_id, include_internal=include_internal))]
        output = []
        for kind, view in views:
            for group in view['items']:
                negative = [h for h in group['history'] if h.get('state', h.get('phase')) in ('cancelled', 'delayed', 'withdrawn', 'paused')]
                if not negative:
                    continue
                scoped = [kind, group['subject_id'], group.get('channel'), group['name'], group.get('period')]
                identity = hashlib.sha256(json.dumps(scoped, ensure_ascii=False).encode()).hexdigest()
                state = group.get('state', group.get('phase'))
                latest = group.get('effective_at') or max((h['occurred_at'] or '' for h in group['history']), default='')
                input_key = hashlib.sha256(json.dumps([scoped, state, latest, group['support']]).encode()).hexdigest()
                status = ('observation_insufficient' if group['support'] != 'source_assertion' else 'open'
                          if state in ('cancelled', 'delayed', 'withdrawn', 'paused') else 'resolved_by_source_change')
                with closing(self.store._connect()) as db:
                    records = [json.loads(r[0]) for r in db.execute('SELECT record FROM studio_concern_feedback WHERE project_id=? AND concern_id=? ORDER BY version DESC LIMIT 20', (project_id, identity))]
                if records and records[0]['input_key'] == input_key:
                    status = records[0]['status']
                label = {'cancelled': '取消', 'delayed': '延期', 'withdrawn': '撤回', 'paused': '暂停'}.get(state, '阶段变化')
                output.append({'id': identity, 'subject_id': group['subject_id'], 'scope': kind, 'name': group['name'],
                    'title': f"{group['name']} · 资料中的{label}", 'status': status, 'input_key': input_key,
                    'proposer': 'source_projection', 'observed_at': max(h['discovered_at'] for h in group['history']),
                    'fact_ids': list(dict.fromkeys(h['fact_id'] for h in group['history'])),
                    'refs': [r for h in group['history'] for r in h['refs']], 'unknowns': sorted({m for h in negative for m in h['missing']}),
                    'feedback_history': records if include_internal else [{k: v for k, v in r.items() if k not in ('reason', 'actor', 'fact_ids')} for r in records],
                    'basis_status': group['support'], 'investment_judgment': False})
        return {'project_id': project_id, 'schema_version': 1, 'items': output[:50],
                'truncated': len(output) > 50 or any(v['truncated'] for _, v in views), 'investment_judgment': False}

    def feedback(self, project_id, body, *, token=None):
        payload = hashlib.sha256(('concern:' + body.model_dump_json()).encode()).hexdigest()
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            grant = self.service._authorize(db, token, project_id, 'write') if token else None
            actor, internal = (grant['id'], grant['allow_internal']) if grant else ('local_web', True)
            old = db.execute('SELECT payload_hash,receipt FROM studio_operations WHERE instance_id=? AND project_id=? AND actor=? AND idempotency_key=?',
                (self.service.instance_id, project_id, actor, body.idempotency_key)).fetchone()
            if old:
                if old[0] != payload:
                    raise VersionConflict('idempotency key already used with another payload')
                return json.loads(old[1])
            project = db.execute('SELECT config_version FROM studio_projects WHERE project_id=?', (project_id,)).fetchone()
            if not project:
                raise ProjectMissing('Studio project not found')
            if project[0] != body.expected_version:
                raise VersionConflict('project configuration has changed; read latest version')
            row = next((r for r in self.read(project_id, include_internal=internal)['items'] if r['id'] == body.concern_id), None)
            if not row:
                raise ProjectMissing('concern not found in permitted project')
            version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM studio_concern_feedback WHERE project_id=? AND concern_id=?', (project_id, body.concern_id)).fetchone()[0]
            record = {'concern_id': body.concern_id, 'version': version, 'status': body.status, 'reason': body.reason,
                      'actor': actor, 'at': now(), 'input_key': row['input_key'], 'fact_ids': row['fact_ids']}
            db.execute('INSERT INTO studio_concern_feedback VALUES (?,?,?,?)', (project_id, body.concern_id, version, json.dumps(record)))
            db.execute('UPDATE studio_projects SET config_version=config_version+1 WHERE project_id=?', (project_id,))
            # A feedback reason can contain a private note. Only metadata enters public change scope.
            visibility = 'internal' if (self.service.objects.get(project_id, row['subject_id'], db=db)['visibility'] == 'internal'
                or any(self.service.evidence.get(project_id, ref['evidence_version_id'], db=db)['visibility'] == 'internal' for ref in row['refs'])) else 'public'
            self.service.evidence.dependencies._change(db, project_id, visibility, 'concern_feedback', {'concern_id': body.concern_id, 'status': body.status, 'version': version})
            receipt = {'instance_id': self.service.instance_id, 'project_id': project_id, 'schema_version': 1,
                'operation_id': str(uuid.uuid4()), 'version': project[0]+1, 'saved': True,
                'operation_status': 'completed', 'job_status': 'not_requested', 'job_id': None, 'concern_id': body.concern_id}
            db.execute('INSERT INTO studio_operations VALUES (?,?,?,?,?,?,?,?)', (receipt['operation_id'], project_id,
                self.service.instance_id, actor, body.idempotency_key, payload, json.dumps(receipt), now()))
            return receipt
