"""Owner-facing, stored-only external monitoring progress; no business silence claim."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

COPY = {
    'queued': ('等待更新', '正在等待更新，尚未完成本轮监控。'),
    'running': ('正在更新', '正在检索和整理，已有结果保留。'),
    'failed': ('更新未完成', '本轮更新未完成，已有结果保留。'),
    'partial': ('部分完成', '部分信息尚未整理完成。'),
    'unobserved': ('尚未完成首次监控', '尚未完成首次监控。'),
    'stale': ('近期尚未更新', '近期监控尚未完成。'),
    'paused': ('已暂停', '监控已暂停，已有结果保留。'),
    'inactive': ('尚未开启监控', '尚未开启监控。'),
    'observed': ('已整理', '已收录信息未见重要变化，公开信息覆盖有限。'),
}


def read(store, project_id: str, *, days: int = 60) -> dict:
    """A completed timestamp alone cannot certify collection or classification.

    Use one read snapshot for the project, its latest external job, stored collection
    report and outstanding classification. This projection is for the local owner,
    not an expansion of Agent disclosure grants. No provider, enqueue or recovery.
    """
    with closing(store._connect()) as db:
        db.execute('BEGIN')
        project = db.execute('SELECT status,consent_at,last_external_at FROM studio_projects WHERE project_id=?',
                             (project_id,)).fetchone()
        if not project:
            return _result('inactive', None, 0, 0)
        job = db.execute("SELECT status,kind FROM studio_jobs WHERE project_id=? AND kind IN ('external','external_followup') "
                         "ORDER BY created_at DESC,rowid DESC LIMIT 1", (project_id,)).fetchone()
        plan = db.execute('SELECT data FROM studio_plan WHERE project_id=?', (project_id,)).fetchone()
        pending = db.execute('SELECT count(*) FROM studio_items WHERE project_id=? AND triaged=0', (project_id,)).fetchone()[0]
        plan_data = json.loads(plan[0]) if plan else {}
        report = plan_data.get('last_report') or {}
    rows = [row for row in report.values() if isinstance(row, dict)]
    failures = sum(len(row.get('errors') or []) for row in rows)
    checked = sum(row.get('checked', 0) for row in rows if type(row.get('checked', 0)) is int)
    completed_at = project['last_external_at']
    if project['status'] == 'paused':
        state = 'paused'
    elif not project['consent_at']:
        state = 'inactive'
    elif (job and job['kind'] == 'external_followup' and job['status'] in ('queued', 'running')
          and plan_data.get('collection_phase') == 'first'):
        state = 'partial'
    elif job and job['status'] in ('queued', 'running', 'failed'):
        state = job['status']
    elif pending or failures:
        state = 'partial'
    elif not completed_at or not checked:
        state = 'unobserved'
    else:
        try:
            timestamp = datetime.fromisoformat(completed_at).astimezone(UTC)
            current = datetime.now(UTC)
            state = 'unobserved' if timestamp > current else 'stale' if timestamp < current - timedelta(days=days) else 'observed'
        except (TypeError, ValueError):
            state = 'unobserved'
    result = _result(state, completed_at, failures, pending)
    result['first_result_at'] = plan_data.get('first_result_at')
    if state == 'partial' and plan_data.get('collection_phase') == 'first':
        result.update(label='首轮已整理', empty_text='竞品、行业等信息仍在补全。')
    return result


def _result(state, completed_at, failures, pending):
    label, empty_text = COPY[state]
    return {'state': state, 'label': label, 'empty_text': empty_text,
            'completed_at': completed_at, 'source_failures': failures, 'pending_items': pending,
            'first_result_at': None,
            'full_coverage': False}
