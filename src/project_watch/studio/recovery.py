"""Owner-confirmed retry of a failed leased round; all old receipts are retained."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime

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


def other_pending_requests(db, project_id, job_id):
    """Safe diagnostics for project-wide DSH holds; never returns payloads or keys."""
    return [dict(row) for row in db.execute(f"""SELECT c.job_id,c.kind,c.status,c.response_status
        FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id
        WHERE j.project_id=? AND c.job_id<>?
        AND (({UNRESOLVED}) OR ({_RECEIVED_MODEL_FAILURE})) ORDER BY c.rowid""", (project_id, job_id))]


def pending_reason(rows):
    models = sum(row['kind'] == 'model' and row['status'] == 'completed' for row in rows)
    parts = []
    if models:
        parts.append(f'{models} 条模型服务错误回执（已收到 HTTP 5xx，但没有可用回答，计费待核）')
    if len(rows) > models:
        parts.append(f'{len(rows) - models} 条结果不明的请求')
    return '同项目其他任务还有' + '、'.join(parts) + '。本次恢复不包含这些请求，请先处理对应条目；未自动重发。'


def received_models_only(rows):
    return all(row['kind'] == 'model' and row['status'] == 'completed'
               and row['response_status'] in (500, 502, 503, 504, 529) for row in rows)


def dsh_enabled():
    return bool(os.environ.get('PROJECT_WATCH_DSH_RUNTIME')) and os.environ.get('PROJECT_WATCH_DSH_MONITOR') == '1'


def _preview(pipeline, project_id, job_id, db):
    job = db.execute('SELECT * FROM studio_jobs WHERE id=? AND project_id=?', (job_id, project_id)).fetchone()
    if not job:
        raise KeyError('任务不存在')
    calls = db.execute(f'SELECT c.call_key,c.kind,c.status,c.response_status FROM studio_job_calls c '
                       f'WHERE c.job_id=? AND (({UNRESOLVED}) OR ({_RECEIVED_MODEL_FAILURE}))',
                       (job_id,)).fetchall()
    # Presentation only: started is a safety fence, not proof of a lost reply.
    # Never change UNRESOLVED or the request journal/admission checks to fix UI.
    from project_watch.studio.runtime_health import expired_reason

    at = datetime.now(UTC)
    in_flight = (job['status'] == 'running' and job['lease_token'] and job['lease_until']
                 and job['lease_until'] > at.isoformat() and not expired_reason(db, job, at))
    waiting_response = in_flight and any(c['status'] == 'started' for c in calls)
    if in_flight:
        calls = [c for c in calls if c['status'] != 'started']
    queries = unknown_queries(db, project_id, job_id)
    binding = input_binding(pipeline, project_id, db)
    saved = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='recovery_inputs'", (job_id,)).fetchone()
    saved_binding = json.loads(saved[0]).get('binding') if saved else None
    project = pipeline.store.get_project(project_id) or {}
    reason = ''
    terminal = (job['status'] == 'done' and job['kind'] in ('external', 'external_followup')
                and len(calls) == 1 and calls[0]['kind'] == 'model' and not queries and dsh_enabled())
    if job['status'] != 'failed' and not terminal:
        if job['status'] in ('running', 'queued') and (calls or queries):
            reason = '仍有待核对的请求，请等待任务收尾后再确认恢复；未自动重发'
        elif waiting_response:
            reason = '请求正在执行，等待响应；请勿重复恢复'
        else:
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
    elif project.get('execution_mode') == 'agent':
        reason = '当前为 Agent 模式，请在 Agent 中处理'
    elif db.execute("SELECT 1 FROM studio_jobs WHERE project_id=? AND kind=? AND id<>? "
                    "AND status IN ('queued','running')", (project_id, 'external_followup' if terminal else job['kind'], job_id)).fetchone():
        reason = '另一轮同类更新正在等待或执行'
    requests = {}
    for call in calls:
        requests[call['kind']] = requests.get(call['kind'], 0) + 1
    if queries and not requests.get('search'):
        requests['search'] = len(queries)
    if requests.get('model') and not pipeline.settings_store.load().model.ready:
        reason = reason or '请先连接模型服务'
    blockers = []
    if dsh_enabled() and (requests.get('site') or requests.get('model')):
        blockers = other_pending_requests(db, project_id, job_id)
        # An exact authorized GET can settle before a received model failure.
        # This does NOT admit tools/models or cross any unknown request.
        if blockers and not (set(requests) == {'site'} and received_models_only(blockers)):
            reason = reason or pending_reason(blockers)
    linked = db.execute("SELECT j.id,j.status FROM studio_job_checkpoints p JOIN studio_jobs j ON j.id=p.job_id "
                        "WHERE p.stage='recovery_origin' AND j.project_id=? AND json_extract(p.data,'$.job_id')=? "
                        "ORDER BY j.rowid DESC LIMIT 1", (project_id, job_id)).fetchone() if terminal else None
    if linked and linked['status'] != 'failed':
        reason = reason or '已建立独立续跑任务，请查看该任务状态；不会重复安排'
    version = digest({'job': dict(job), 'calls': [dict(c) for c in calls],
                      'queries': [q['id'] for q in queries], 'binding': binding, 'blocking_requests': blockers,
                      'linked': dict(linked) if linked else None})
    public = {'job_id': job_id, 'kind': job['kind'], 'version': version,
              'can_recover': not reason, 'reason': reason, 'requests': requests,
              'requires_duplicate_cost_consent': True, 'job_status': job['status'],
              'mode': 'checkpoint_continuation' if terminal else 'resume',
              'continuation_job_id': linked['id'] if linked else None,
              'notice': ('只恢复这条网页请求，模型错误仍需另行确认，完整链路暂不继续。'
                         if blockers and not reason else '')}
    return public, calls, queries


def preview(pipeline, project_id, job_id):
    with closing(pipeline.store._connect()) as db:
        db.execute('BEGIN')
        return _preview(pipeline, project_id, job_id, db)[0]


def project_preview(pipeline, project_id):
    """Locate receipts across ALL jobs, including dismissed or terminal history."""
    with closing(pipeline.store._connect()) as db:
        db.execute('BEGIN')
        ids = [row[0] for row in db.execute(f"""SELECT DISTINCT c.job_id FROM studio_job_calls c
            JOIN studio_jobs j ON j.id=c.job_id WHERE j.project_id=?
            AND (({UNRESOLVED}) OR ({_RECEIVED_MODEL_FAILURE}))""", (project_id,))]
        ids += [r[0] for r in db.execute("SELECT DISTINCT u.job_id FROM studio_query_uses u JOIN studio_jobs j ON j.id=u.job_id "
                                       "WHERE u.project_id=? AND j.project_id=u.project_id AND u.outcome='unknown'", (project_id,))]
        items = [_preview(pipeline, project_id, jid, db)[0] for jid in dict.fromkeys(ids)]
        items = [p for p in items if p['requests']]
        return {'items': sorted(items, key=lambda p: (not p['can_recover'], 'site' not in p['requests'], p['job_id']))}


def model_continuation_slot(db, job):
    """Journal-level check of one pending request and its explicit origin link."""
    rows = db.execute(f"""SELECT c.job_id,c.call_key,c.kind,c.status FROM studio_job_calls c
        JOIN studio_jobs j ON j.id=c.job_id WHERE j.project_id=?
        AND (({UNRESOLVED}) OR ({_RECEIVED_MODEL_FAILURE}))""", (job['project_id'],)).fetchall()
    if len(rows) != 1 or rows[0]['kind'] != 'model' or rows[0]['status'] not in ('unknown', 'completed'):
        return None
    call = rows[0]
    if call['job_id'] != job['id']:
        marker = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='recovery_origin'", (job['id'],)).fetchone()
        origin = json.loads(marker[0]) if marker else {}
        if origin != {'job_id': call['job_id'], 'previous_key': call['call_key']}:
            return None
        parent = db.execute("SELECT 1 FROM studio_jobs WHERE id=? AND project_id=? AND status='done'", (call['job_id'], job['project_id'])).fetchone()
        if not parent:
            return None
    slot = db.execute('SELECT previous_key,next_key FROM studio_call_recoveries '
                      'WHERE job_id=? AND previous_key=? AND consumed_at IS NULL', (call['job_id'], call['call_key'])).fetchone()
    return {**dict(slot), 'origin_job_id': call['job_id']} if slot else None


def dsh_model_continuation(pipeline, execution):
    """One owner-authorized model continuation, not proof of old HTTP success."""
    with closing(pipeline.store._connect()) as db:
        execution._guard(db)
        slot = model_continuation_slot(db, execution.job)
        if not slot:
            return None
        saved = execution.read_checkpoint("recovery_inputs")
        if not saved or saved.get("binding") != input_binding(pipeline, execution.job["project_id"], db):
            return None
        return dict(slot)


def dsh_site_recovery(pipeline, execution, db=None):
    """One exact legacy GET, never a model/tool permission or a new URL grant."""
    if db is None:
        with closing(pipeline.store._connect()) as connection:
            connection.execute('BEGIN')
            return dsh_site_recovery(pipeline, execution, db=connection)
    execution._guard(db)
    rows = db.execute(f"""SELECT c.job_id,c.call_key,c.kind,c.status,c.provider,c.response_status
        FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id
        WHERE j.project_id=? AND (({UNRESOLVED}) OR ({_RECEIVED_MODEL_FAILURE}))""",
        (execution.job['project_id'],)).fetchall()
    sites = [r for r in rows if r['job_id'] == execution.job['id'] and r['kind'] == 'site' and r['status'] == 'unknown']
    if len(sites) != 1 or not received_models_only([r for r in rows if r not in sites]):
        return None
    call = sites[0]
    if call['job_id'] != execution.job['id'] or call['kind'] != 'site' or call['status'] != 'unknown':
        return None
    slot = db.execute('SELECT base_key,previous_key,next_key FROM studio_call_recoveries '
                      'WHERE job_id=? AND previous_key=? AND consumed_at IS NULL',
                      (call['job_id'], call['call_key'])).fetchone()
    saved = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='recovery_inputs'",
                       (execution.job['id'],)).fetchone()
    project = db.execute('SELECT status,consent_at FROM studio_projects WHERE project_id=?',
                         (execution.job['project_id'],)).fetchone()
    if (not slot or not saved or not project or project['status'] != 'active' or not project['consent_at']
            or json.loads(saved[0]).get('binding') != input_binding(pipeline, execution.job['project_id'], db)):
        return None
    return {**dict(slot), 'provider': call['provider']}


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
        target_id = job_id
        if state['mode'] == 'checkpoint_continuation':
            target_id = state['continuation_job_id'] or uuid.uuid4().hex
            if state['continuation_job_id']:
                db.execute("UPDATE studio_jobs SET status='queued',error='',finished_at=NULL,retry_after=NULL,"
                           "lease_token='',lease_until=NULL WHERE id=? AND status='failed'", (target_id,))
            else:
                db.execute("INSERT INTO studio_jobs(id,project_id,kind,status,created_at,input_version) "
                           "VALUES (?,?,'external_followup','queued',?,?)", (target_id, project_id, now(), 'recovery:' + job_id))
                db.executemany('INSERT INTO studio_job_checkpoints VALUES (?,?,?)', [
                    (target_id, 'recovery_origin', json.dumps({'job_id': job_id, 'previous_key': calls[0]['call_key']})),
                    (target_id, 'recovery_inputs', json.dumps({'binding': input_binding(pipeline, project_id, db)}))])
        else:
            db.execute("UPDATE studio_jobs SET status='queued',error='',finished_at=NULL,retry_after=NULL,"
                       "lease_token='',lease_until=NULL WHERE id=?", (job_id,))
        response = {'job_id': target_id, 'status': 'queued', 'recovery_id': idempotency_key}
        db.execute('INSERT INTO studio_recovery_actions VALUES (?,?,?,?,?,?)',
                   (idempotency_key, project_id, job_id, request_hash, json.dumps(response), now()))
        return response
