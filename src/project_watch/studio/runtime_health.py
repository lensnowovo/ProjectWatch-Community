"""Progress watchdog and read-only runtime projection. No provider calls."""

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

IDLE_SECONDS = 15 * 60
CALL_GRACE_SECONDS = 60


def stage_phase(stage):
    """Explicit vocabulary of emitted stages; unknown stages stay unknown.

    This is a display contract, not a scheduler or a claim that earlier steps
    completed. Jobs may revisit phases and run independently of one another.
    """
    if (stage or '').startswith('DSH '):
        return {'DSH 检索公开信息': 'collect', 'DSH 阅读来源': 'read',
                'DSH 检索公开信息：专题': 'collect', 'DSH 整理公开证据': 'read',
                'DSH 结束本轮探索': 'revise',
                'DSH 整理证据': 'read', 'DSH 检查覆盖': 'read',
                'DSH 保存公开档案': 'read'}.get(stage)
    groups = {
        'revise': ('制定监控方案', '生成首版监控方案', '修订监控方案'),
        'judge': ('更新跟踪问题', '对照新旧资料', '生成简报', '生成当前判断', '深度分析'),
        'collect': ('检索', '寻找公司官网', '查找报道原文', '更新融资与专业来源'),
        'read': ('阅读', '核对', 'AI 筛选信息', '筛选待阅读材料', '合并同一事件',
                 '更新事件线索', '整理公司档案', '补充公开公司档案', '提取产品',
                 '整理商业关系', '整理同行公司线索', '整理竞争与行业'),
    }
    return next((phase for phase, prefixes in groups.items() if (stage or '').startswith(prefixes)), None)


def job_phase(db, job):
    """Job-bound business phase; transport labels cannot erase it."""
    checkpoint = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? "
                            "AND stage='dsh_runtime_phase'", (job['id'],)).fetchone()
    try:
        value = json.loads(checkpoint[0]) if checkpoint else None
    except (ValueError, TypeError):
        value = None
    phase = value.get('phase') if isinstance(value, dict) else None
    if isinstance(phase, str) and phase in ('collect', 'read', 'judge', 'revise'):
        return phase
    # Older DSH receipts and classic jobs keep the existing display contract.
    return stage_phase(job['stage'])


def project_jobs(store, project_id, *, worker='running'):
    """All active jobs and the last terminal job per kind, without lease secrets.

    A small recent-history limit must not hide a still-unresolved older failure.
    """
    from project_watch.studio.recovery import _RECEIVED_MODEL_FAILURE, UNRESOLVED

    with closing(store._connect()) as db:
        rows = db.execute(f"""WITH ranked AS (
            SELECT j.*, ROW_NUMBER() OVER (
                PARTITION BY kind, CASE WHEN status IN ('done','failed') THEN 'terminal' ELSE id END
                ORDER BY COALESCE(finished_at,created_at) DESC, created_at DESC, id DESC
            ) AS rn FROM studio_jobs j WHERE project_id=?
        ) SELECT * FROM ranked WHERE rn=1 OR EXISTS (SELECT 1 FROM studio_job_calls c
          WHERE c.job_id=ranked.id AND (({UNRESOLVED}) OR ({_RECEIVED_MODEL_FAILURE})))
          ORDER BY created_at DESC""", (project_id,)).fetchall()
        dismissed = {r[0].removeprefix('task_dismissed:') for r in db.execute(
            "SELECT key FROM studio_state WHERE key LIKE 'task_dismissed:%'")}
        return [{**{key: row[key] for key in ('id', 'kind', 'status', 'stage', 'error', 'created_at', 'finished_at')},
                 'dismissed': row['id'] in dismissed, 'runtime': projection(db, row, worker=worker)} for row in rows]


def active_projections(store, *, worker='running'):
    """Small project-list status projection, preferring execution over queued work."""
    with closing(store._connect()) as db:
        result = {}
        for row in db.execute("SELECT * FROM studio_jobs WHERE status IN ('running','queued') "
                              "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at"):
            if row['project_id'] not in result:
                result[row['project_id']] = {'status': row['status'], 'runtime': projection(db, row, worker=worker)}
        return result


def stamp():
    return datetime.now(UTC).isoformat()


def seconds_since(value, at=None):
    if not value:
        return None
    try:
        return max(0, int(((at or datetime.now(UTC)) - datetime.fromisoformat(value)).total_seconds()))
    except (ValueError, TypeError):
        return None


def progress(db, job_id):
    db.execute('UPDATE studio_jobs SET progress_at=? WHERE id=?', (stamp(), job_id))


def expired_reason(db, job, at=None):
    from project_watch.studio.recovery import UNRESOLVED

    at = at or datetime.now(UTC)
    pending = db.execute(f"SELECT created_at,deadline_at FROM studio_job_calls c WHERE c.job_id=? AND c.status='started' AND {UNRESOLVED}",
                         (job['id'],)).fetchall()
    if pending:
        # A running transport has its own allowance, not the idle-stage timeout.
        for call in pending:
            deadline = call['deadline_at'] or (datetime.fromisoformat(call['created_at']) + timedelta(seconds=IDLE_SECONDS)).isoformat()
            if deadline <= at.isoformat():
                return 'response_timeout'
        return ''
    age = seconds_since(job['progress_at'] or job['started_at'], at)
    return 'no_progress' if age is not None and age >= IDLE_SECONDS else ''


def tick(store, job, *, at=None):
    """Renew or revoke atomically. Never start a replacement Python thread.

    Safe work is queued under the same id (bounded recovery); an ambiguous call
    holds the job indefinitely. Old writers are fenced before either transition.
    """
    from project_watch.studio.recovery import UNRESOLVED

    at = at or datetime.now(UTC)
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute("SELECT * FROM studio_jobs WHERE id=? AND status='running' AND lease_token=? AND lease_until>?",
                             (job['id'], job['lease_token'], at.isoformat())).fetchone()
        if not current:
            return 'lost'
        reason = expired_reason(db, current, at)
        if not reason:
            db.execute('UPDATE studio_jobs SET heartbeat_at=?,lease_until=? WHERE id=?',
                       (at.isoformat(), (at + timedelta(seconds=90)).isoformat(), job['id']))
            return 'alive'
        uncertain = db.execute(f'SELECT 1 FROM studio_job_calls c WHERE c.job_id=? AND {UNRESOLVED} LIMIT 1',
                               (job['id'],)).fetchone()
        held = bool(uncertain) or current['attempts'] >= 3
        db.execute("UPDATE studio_jobs SET status=?,error=?,stage=?,stalled_at=?,finished_at=?,retry_after=?,"
                   "lease_token='',lease_until=NULL WHERE id=?",
                   ('failed' if held else 'queued', 'watchdog_' + reason,
                    '等待确认请求结果' if uncertain else '等待恢复', at.isoformat(), at.isoformat() if held else None,
                    '9999-12-31' if held else None, job['id']))
        return 'held' if held else 'recovering'


def activity(db, job_id, at):
    """Receipt-only progress. Never return evidence, prompts or provider errors."""
    from project_watch.studio.recovery import UNRESOLVED

    calls = db.execute('SELECT kind,status,response_status,created_at,input_tokens,output_tokens,'
                       'cache_read_tokens,cache_creation_tokens FROM studio_job_calls WHERE job_id=?',
                       (job_id,)).fetchall()
    models = [c for c in calls if c['kind'] == 'model']
    pending = db.execute(f"SELECT kind,created_at FROM studio_job_calls c WHERE job_id=? "
                         f"AND c.status='started' AND {UNRESOLVED} ORDER BY created_at LIMIT 1",
                         (job_id,)).fetchall()
    def completed(kind):
        return sum(c['kind'] == kind and c['status'] == 'completed'
                   and 200 <= (c['response_status'] or 0) < 300 for c in calls)
    def known_sum(field):
        values = [c[field] for c in models if c[field] is not None]
        return sum(values) if values else None
    checkpoint = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? "
                            "AND stage='dsh_validation_errors'", (job_id,)).fetchone()
    try:
        errors = json.loads(checkpoint[0]) if checkpoint else []
    except (ValueError, TypeError):
        errors = []
    errors = errors[-100:] if isinstance(errors, list) else []
    # Display categories only; unknown validator codes stay generic, not hidden.
    safe_codes = {'unsupported_numbers', 'missing_qualifications', 'invalid_field', 'missing_citation', 'quote_not_verbatim',
                  'quote_not_found', 'source_not_found', 'invalid_date', 'record_validation'}
    reasons = set()
    for error in errors[-3:]:
        if not isinstance(error, dict):
            continue
        issues = error.get('issues')
        issues = issues if isinstance(issues, list) else []
        for issue in issues[:20]:
            if isinstance(issue, dict):
                code = issue.get('code')
                reasons.add(code if isinstance(code, str) and code in safe_codes else 'record_validation')
        if not issues:
            reasons.add('record_validation')
    # Saved, job-bound bootstrap records; never substitute provider calls or project-wide totals.
    saved = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='dsh_state'",
                       (job_id,)).fetchone()
    try:
        snapshot = json.loads(saved[0]) if saved else None
    except (ValueError, TypeError):
        snapshot = None
    records = snapshot.get('records') if isinstance(snapshot, dict) else None
    records_saved = None
    if isinstance(records, list) and all(isinstance(r, dict) and isinstance(r.get('key'), str)
                                         and r['key'] for r in records):
        records_saved = len({r['key'] for r in records})
    return {'records_saved': records_saved, 'model_requests': len(models), 'model_completed': completed('model'),
            'search_completed': completed('search'),
            'pending_kind': pending[0]['kind'] if pending else None,
            'pending_seconds': seconds_since(pending[0]['created_at'], at) if pending else None,
            'validation_attempts': len(errors), 'validation_reasons': sorted(reasons),
            'usage_missing_calls': sum(c['input_tokens'] is None or c['output_tokens'] is None for c in models),
            **{f: known_sum(f) for f in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens')}}


def projection(db, job, *, worker='running', at=None):
    """Do not expose prompts, provider URLs, credentials or lease tokens."""
    at = at or datetime.now(UTC)
    state = job['status']
    labels = {'running': '正在处理', 'queued': '等待开始', 'done': '已完成', 'failed': '未完成'}
    label = labels.get(state, state)
    if state == 'running':
        reason = expired_reason(db, job, at)
        from project_watch.studio.recovery import UNRESOLVED

        pending = db.execute(f"SELECT kind FROM studio_job_calls c WHERE c.job_id=? AND c.status='started' AND {UNRESOLVED} LIMIT 1", (job['id'],)).fetchone()
        if reason:
            state, label = 'stalled', '长时间无进展，等待恢复'
        elif worker != 'running' or not job['lease_until'] or job['lease_until'] <= at.isoformat():
            state, label = 'recovering', '后台中断，等待恢复'
        elif pending:
            state, label = 'waiting_response', '正在研究 · 模型处理中' if pending[0] == 'model' else '正在获取来源响应'
    elif state == 'queued':
        project = db.execute('SELECT status FROM studio_projects WHERE project_id=?', (job['project_id'],)).fetchone()
        if project and project[0] == 'paused' and job['kind'] != 'internal':
            state, label = 'paused', '项目已暂停'
        elif worker != 'running':
            state, label = 'blocked', '等待后台恢复'
        elif job['stalled_at']:
            state, label = 'recovering', '已保留进度，等待恢复'
        elif job['yield_count']:
            label = '已保存进度，排队继续'
    elif state in ('failed', 'done'):
        from project_watch.studio.recovery import _RECEIVED_MODEL_FAILURE, UNRESOLVED

        if db.execute(f'SELECT 1 FROM studio_job_calls c WHERE c.job_id=? AND {UNRESOLVED} LIMIT 1', (job['id'],)).fetchone():
            state, label = ('needs_confirmation', '请求结果不明，等待确认') if state == 'failed' else (
                'partial', '已有结果保留，部分请求结果不明')
        elif db.execute(f'SELECT 1 FROM studio_job_calls c WHERE c.job_id=? AND ({_RECEIVED_MODEL_FAILURE}) LIMIT 1', (job['id'],)).fetchone():
            state, label = 'needs_confirmation', '模型服务出错，等待确认恢复'
        elif job['error'].startswith('watchdog_'):
            state, label = 'stalled', '多次无进展，已停止恢复'
        elif state == 'done':
            bootstrap = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='dsh_coverage'", (job['id'],)).fetchone()
            if bootstrap and json.loads(bootstrap[0]).get('partial'):
                state, label = 'partial', '公开信息已整理，仍有覆盖缺口'
            coverage = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='commercial_coverage'", (job['id'],)).fetchone()
            if coverage and json.loads(coverage[0]).get('partial'):
                state, label = 'partial', '部分来源已更新，其余暂不可用'
    return {'state': state, 'label': label, 'phase': job_phase(db, job), 'progress_at': job['progress_at'],
            'heartbeat_at': job['heartbeat_at'], 'progress_age_seconds': seconds_since(job['progress_at'], at),
            'yield_count': job['yield_count'], 'recovery_attempts': max(0, job['attempts'] - 1),
            'activity': activity(db, job['id'], at)}
