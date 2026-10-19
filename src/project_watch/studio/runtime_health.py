"""Progress watchdog and read-only runtime projection. No provider calls."""

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

IDLE_SECONDS = 15 * 60
CALL_GRACE_SECONDS = 60


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
            state, label = 'waiting_response', '等待模型响应' if pending[0] == 'model' else '等待来源响应'
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
        from project_watch.studio.recovery import UNRESOLVED

        if db.execute(f'SELECT 1 FROM studio_job_calls c WHERE c.job_id=? AND {UNRESOLVED} LIMIT 1', (job['id'],)).fetchone():
            state, label = ('needs_confirmation', '请求结果不明，等待确认') if state == 'failed' else (
                'partial', '已有结果保留，部分请求结果不明')
        elif job['error'].startswith('watchdog_'):
            state, label = 'stalled', '多次无进展，已停止恢复'
        elif state == 'done':
            coverage = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='commercial_coverage'", (job['id'],)).fetchone()
            if coverage and json.loads(coverage[0]).get('partial'):
                state, label = 'partial', '部分来源已更新，其余暂不可用'
    return {'state': state, 'label': label, 'progress_at': job['progress_at'],
            'heartbeat_at': job['heartbeat_at'], 'progress_age_seconds': seconds_since(job['progress_at'], at),
            'yield_count': job['yield_count'], 'recovery_attempts': max(0, job['attempts'] - 1)}
