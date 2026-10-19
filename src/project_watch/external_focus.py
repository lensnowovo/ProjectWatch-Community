"""Explicit public-query scopes, separate from private project context and facts.

SQLite-only additive schema. A scope is immutable: changing its category or term
requires a new query, never relabels historical receipts. Existing company feeds
retain their old matcher until explicitly configured.
"""

from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from project_watch.external_focus_schema import (
    ensure_external_focus_schema as ensure_external_focus_schema,
)
from project_watch.external_scopes import CATEGORIES as CATEGORIES
from project_watch.external_scopes import CONSENT_VERSION as CONSENT_VERSION
from project_watch.external_scopes import ExternalFocusStore as ExternalFocusStore
from project_watch.external_scopes import public_query as public_query


def external_reading(repo, project_id):
    from project_watch.external_attention import headline_attention
    from project_watch.external_context import attach_internal_context
    from project_watch.repository import NotFoundError
    from project_watch.signal_projection import deduplicate_project_signals

    project = repo.get_project(project_id)
    if project is None:
        raise NotFoundError('项目不存在。')
    groups = {key: {'key': key, 'title': title, 'coverage': 'not_configured', 'plans': [], 'items': []}
              for key, title in CATEGORIES.items()}
    store = ExternalFocusStore(repo)
    source_checks = {}
    with closing(repo._connect()) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_source_attempts'").fetchone():
            for r in db.execute('''SELECT a.*,r.id AS run_id FROM monitor_source_attempts a
                JOIN source_runs r ON r.plan_id=a.plan_id AND r.idempotency_key=a.run_key
                WHERE r.project_id=? ORDER BY a.id''',(project_id,)):
                source_checks.setdefault(r['run_id'],[]).append({
                    **{k:r[k] for k in ('provider','state','error_code','item_count')},
                    'request_variant':dict(r).get('request_variant','primary'),
                })
    scopes = {}
    for plan in repo.list_monitoring_plans(project_id):
        scope = store.get(plan.id)
        category = scope['category'] if scope else 'company'
        scopes[plan.id] = scope
        runs = repo.list_source_runs(plan.id)
        successful = [r for r in runs if r.status in {'completed', 'no_results'}]
        groups[category]['plans'].append({
            'id': plan.id, 'term': scope['term'] if scope else project.name,
            'provider': scope['provider'] if scope else ('brave' if plan.kind == 'brave_news' else 'rss'),
            'enabled': plan.enabled, 'last_status': plan.last_run_status,
            'source_checks': source_checks.get(runs[-1].id,[]) if runs else [],
            'last_success_at': max((r.finished_at for r in successful), default=None),
        })
    signals = repo.list_signals(project_id)
    now = datetime.now(UTC)
    for category, group in groups.items():
        plans = group['plans']
        active = [p for p in plans if p['enabled']]
        statuses = {p['last_status'] for p in active}
        supplement_failed = any(a.get('request_variant') == 'recent' and a['state'] != 'succeeded'
                                for p in active for a in p['source_checks'])
        if plans:
            group['coverage'] = ('paused' if not active else
                                 'failed' if statuses & {'unreachable', 'rate_limited'} or supplement_failed else
                                 'pending' if statuses - {'completed', 'no_results'} else 'checked')
        ids = {p['id'] for p in plans}
        current = [s for s in signals if s.project_id == project_id and s.plan_id in ids and s.freshness == 'current'
                   and s.status != 'rejected' and s.published_at is not None
                   and now - timedelta(days=30) <= s.published_at <= now + timedelta(days=1)]
        for signal in deduplicate_project_signals(current):
            scope = scopes.get(signal.plan_id)
            term = scope['term'] if scope else project.name
            reason = (f'与公开关注词“{term}”匹配。政策的地域、对象及生效条件尚需核实。'
                      if category == 'policy' else
                      f'名称匹配“{term}”；同名主体尚需核实。' if category in {'company', 'competitors'} else
                      f'与公开关注词“{term}”匹配；对本项目的具体影响尚未确定。')
            group['items'].append({**asdict(signal), 'category': category, 'match_term': term,
                                   'collection_sources': list(dict.fromkeys(a['provider'] for a in source_checks.get(signal.run_id,[]) if a['state'] == 'succeeded')),
                                   'relevance': reason,
                                   'attention': headline_attention(signal.title, term)})
        group['items'].sort(key=lambda s: s['published_at'], reverse=True)
    attach_internal_context(repo, project_id, groups.values())
    from project_watch.monitor_activation import MonitorActivationStore
    from project_watch.monitor_briefing import build_external_briefing
    strategy = MonitorActivationStore(repo, initialize=False).read(project_id)
    categories = list(groups.values())
    return {'project_id': project_id, 'categories': categories, 'monitoring_strategy': strategy,
            'briefing': build_external_briefing(repo, project_id, categories, strategy)}
