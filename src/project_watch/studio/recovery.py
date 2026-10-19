"""Owner-confirmed retry of a failed leased round; all old receipts are retained."""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import closing
from dataclasses import asdict

from project_watch.studio.store import now

SCHEMA = '''
CREATE TABLE IF NOT EXISTS studio_call_recoveries (
    job_id TEXT NOT NULL REFERENCES studio_jobs(id), base_key TEXT NOT NULL,
    previous_key TEXT NOT NULL, next_key TEXT NOT NULL, created_at TEXT NOT NULL,
    consumed_at TEXT, PRIMARY KEY(job_id,next_key), UNIQUE(job_id,previous_key)
);
CREATE TABLE IF NOT EXISTS studio_query_recoveries (
    project_id TEXT NOT NULL REFERENCES projects(id), job_id TEXT NOT NULL,
    query_key TEXT NOT NULL, unknown_use_id TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,job_id,unknown_use_id)
);
CREATE TABLE IF NOT EXISTS studio_recovery_actions (
    idempotency_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL,
    request_hash TEXT NOT NULL, response_data TEXT NOT NULL, created_at TEXT NOT NULL
);
'''

# Use only for work-blocking state, not historical uncertainty/usage totals.
UNRESOLVED = """c.status IN ('started','unknown') AND NOT EXISTS
    (SELECT 1 FROM studio_call_recoveries r WHERE r.job_id=c.job_id AND r.previous_key=c.call_key
     AND r.consumed_at IS NOT NULL)"""
# Received model 5xx responses have no usable answer, but their billing may still
# be unknown. Reuse the same explicit, single-use cost consent as lost responses.
# Do not turn refusals, invalid credentials, quotas or malformed 200s into retries.
_RECEIVED_MODEL_FAILURE = """c.kind='model' AND c.status='completed'
    AND c.response_status IN (500,502,503,504,529) AND NOT EXISTS
    (SELECT 1 FROM studio_call_recoveries r WHERE r.job_id=c.job_id AND r.previous_key=c.call_key
     AND r.consumed_at IS NOT NULL)"""
KINDS = {'internal', 'external', 'external_followup', 'brief', 'public_profile',
         'landscape', 'business', 'competition', 'details', 'insight'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def input_binding(pipeline, project_id, db=None):
    """Only a hash persists; no private settings/text is copied to checkpoints."""
    if db is None:
        with closing(pipeline.store._connect()) as connection:
            return input_binding(pipeline, project_id, connection)
    settings = pipeline.settings_store.load()
    project = pipeline.store.get_project(project_id) or {}
    entity = {key: project.get(key) for key in (
        'name', 'legal_name', 'short_name', 'credit_code', 'website', 'industry',
        'description', 'aliases', 'entity_source', 'stage', 'founded', 'location')}
    sources = [dict(r) for r in db.execute('SELECT * FROM sources WHERE project_id=? ORDER BY id', (project_id,))]
    intake = [dict(r) for r in db.execute('SELECT id,source_id,status,updated_at FROM intake_jobs '
                                        'WHERE project_id=? ORDER BY id', (project_id,))]
    permissions = [dict(r) for r in db.execute('SELECT * FROM studio_source_permissions WHERE project_id=? '
                                             'ORDER BY source_id', (project_id,))]
    return digest({'version': 'recovery-inputs-v1', 'entity': entity, 'sources': sources,
                   'intake': intake, 'permissions': permissions, 'model': asdict(settings.model),
                   'search': asdict(settings.search), 'registry': asdict(settings.registry),
                   'proxy': settings.proxy_url})


def unknown_queries(db, project_id, job_id):
    # Later held uses in other jobs do not change which actual attempt is unknown.
    return db.execute("""SELECT u.* FROM studio_query_uses u
        WHERE u.project_id=? AND u.job_id=? AND u.outcome='unknown' AND NOT EXISTS
        (SELECT 1 FROM studio_query_uses n WHERE n.project_id=u.project_id AND n.query_key=u.query_key
         AND n.rowid>u.rowid AND n.outcome!='held')""", (project_id, job_id)).fetchall()


def _preview(pipeline, project_id, job_id, db):
    job = db.execute('SELECT * FROM studio_jobs WHERE id=? AND project_id=?', (job_id, project_id)).fetchone()
    if not job:
        raise KeyError('任务不存在')
    calls = db.execute(f'SELECT c.call_key,c.kind,c.status,c.response_status FROM studio_job_calls c '
                       f'WHERE c.job_id=? AND (({UNRESOLVED}) OR ({_RECEIVED_MODEL_FAILURE}))',
                       (job_id,)).fetchall()
    queries = unknown_queries(db, project_id, job_id)
    binding = input_binding(pipeline, project_id, db)
    saved = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='recovery_inputs'", (job_id,)).fetchone()
    saved_binding = json.loads(saved[0]).get('binding') if saved else None
    project = pipeline.store.get_project(project_id) or {}
    reason = ''
    if job['status'] != 'failed':
        reason = '这次更新没有等待恢复的失败'
    elif job['kind'] not in KINDS:
        reason = '这类请求需在对应来源中处理'
    elif not calls and not queries:
        reason = '没有可恢复的失败请求'
    elif not saved_binding:
        reason = '旧任务缺少恢复依据，请开始新一轮更新'
    elif saved_binding != binding:
        reason = '公司、材料或服务配置已变化，请开始新一轮更新'
    elif not project.get('activated') or project.get('status') != 'active':
        reason = '请先恢复项目监控'
    elif db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind=? AND id<>? "
                    "AND status IN ('queued','running')", (project_id, job['kind'], job_id)).fetchone():
        reason = '另一轮同类更新正在等待或执行'
    requests = {}
    for call in calls:
        requests[call['kind']] = requests.get(call['kind'], 0) + 1
    if queries and not requests.get('search'):
        requests['search'] = len(queries)
    if requests.get('model') and not pipeline.settings_store.load().model.ready:
        reason = reason or '请先连接模型服务'
    version = digest({'job': dict(job), 'calls': [dict(c) for c in calls],
                      'queries': [q['id'] for q in queries], 'binding': binding})
    public = {'job_id': job_id, 'kind': job['kind'], 'version': version,
              'can_recover': not reason, 'reason': reason, 'requests': requests,
              'requires_duplicate_cost_consent': True}
    return public, calls, queries


def preview(pipeline, project_id, job_id):
    with closing(pipeline.store._connect()) as db:
        db.execute('BEGIN')
        return _preview(pipeline, project_id, job_id, db)[0]


def authorize(pipeline, project_id, job_id, *, expected_version, idempotency_key, accept_duplicate_cost):
    if accept_duplicate_cost is not True:
        raise ValueError('请明确确认可能重复计费后继续')
    request_hash = digest([project_id, job_id, expected_version, accept_duplicate_cost])
    with closing(pipeline.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM studio_recovery_actions WHERE idempotency_key=?', (idempotency_key,)).fetchone()
        if old:
            if old['request_hash'] != request_hash:
                raise ValueError('恢复操作已变化，请重新打开确认窗口')
            return json.loads(old['response_data'])
        state, calls, queries = _preview(pipeline, project_id, job_id, db)
        if state['version'] != expected_version or not state['can_recover']:
            raise ValueError(state['reason'] or '更新状态已变化，请重新打开确认窗口')
        for call in calls:
            pending = db.execute('SELECT 1 FROM studio_call_recoveries WHERE job_id=? AND previous_key=? '
                                 'AND consumed_at IS NULL', (job_id, call['call_key'])).fetchone()
            if pending:
                continue  # No HTTP intent occurred yet; retain the existing slot.
            prior = db.execute('SELECT base_key FROM studio_call_recoveries WHERE job_id=? AND next_key=?',
                               (job_id, call['call_key'])).fetchone()
            base = prior[0] if prior else call['call_key']
            next_key = digest([base, uuid.uuid4().hex])
            db.execute('INSERT INTO studio_call_recoveries VALUES (?,?,?,?,?,NULL)',
                       (job_id, base, call['call_key'], next_key, now()))
        for query in queries:
            db.execute('INSERT OR IGNORE INTO studio_query_recoveries VALUES (?,?,?,?,?)',
                       (project_id, job_id, query['query_key'], query['id'], now()))
        db.execute("UPDATE studio_jobs SET status='queued',error='',finished_at=NULL,retry_after=NULL,"
                   "lease_token='',lease_until=NULL WHERE id=?", (job_id,))
        response = {'job_id': job_id, 'status': 'queued', 'recovery_id': idempotency_key}
        db.execute('INSERT INTO studio_recovery_actions VALUES (?,?,?,?,?,?)',
                   (idempotency_key, project_id, job_id, request_hash, json.dumps(response), now()))
        return response
