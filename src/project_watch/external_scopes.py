"""Persistent public-query scopes and consent validation, without reading projections."""
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

from project_watch.public_search import CADENCE_MINUTES, gdelt_rss_target, normalized_public_name

CATEGORIES = {
    'company': '公司', 'sector': '赛道', 'competitors': '竞品',
    'policy': '政策', 'industry': '行业',
}
CONSENT_VERSION = 'public-focus-v1'


def public_query(category, term, provider):
    from project_watch.bing_news import bing_news_target
    from project_watch.brave_news import brave_news_target

    if category not in CATEGORIES or provider not in {'gdelt', 'brave', 'bing'}:
        raise ValueError('请选择跟踪范围和公开来源。')
    term = normalized_public_name(term)
    target = {'brave': brave_news_target, 'gdelt': gdelt_rss_target, 'bing': bing_news_target}[provider](term)
    name = {'brave':'Brave News', 'gdelt':'GDELT', 'bing':'Bing 新闻订阅（试用来源，覆盖不保证）'}[provider]
    return {
        'category': category, 'term': term, 'provider': provider, 'target': target,
        'consent_version': CONSENT_VERSION, 'cadence_minutes': CADENCE_MINUTES,
        'disclosure': f'将“{term}”发送给 {name}，每天检索一次公开消息。'
                      '仅发送这个关注词，不发送项目资料；可随时暂停。关键词匹配不代表主体或适用性已核实。',
    }


class ExternalFocusStore:
    def __init__(self, repo):
        self.repo = repo

    def get(self, plan_id):
        with closing(self.repo._connect()) as connection:
            row = connection.execute('SELECT * FROM external_focus WHERE plan_id=?', (plan_id,)).fetchone()
            return dict(row) if row else None

    def create(self, project_id, *, category, term, provider):
        with closing(self.repo._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            plan_id = self.create_in_transaction(connection, project_id, category=category,
                                                 term=term, provider=provider)
        return self.repo.get_monitoring_plan(plan_id)

    def create_in_transaction(self, connection, project_id, *, category, term, provider):
        """Caller owns the transaction; allows atomic strategy + plans + first jobs."""
        from project_watch.repository import ReviewConflictError

        query = public_query(category, term, provider)
        kind = 'brave_news' if provider == 'brave' else 'rss'
        # Fixed canonical target validation still goes through the existing safety gate.
        self.repo._validate_monitoring_target(kind, query['target'], CADENCE_MINUTES)
        if not connection.in_transaction:
            raise ValueError('explicit transaction required')
        self.repo._require_project(connection, project_id)
        row = connection.execute(
            'SELECT id, cadence_minutes FROM monitoring_plans WHERE project_id=? AND kind=? AND target=?',
            (project_id, kind, query['target']),
        ).fetchone()
        plan_id = row['id'] if row else str(uuid4())
        if row and row['cadence_minutes'] != CADENCE_MINUTES:
            raise ReviewConflictError('相同关注词已有不同检查频率，请先处理原有来源。')
        existing = connection.execute('SELECT * FROM external_focus WHERE plan_id=?', (plan_id,)).fetchone()
        if existing and (existing['category'], existing['term'], existing['provider']) != (
            category, query['term'], provider
        ):
            raise ReviewConflictError('这个关注词已在其他分类中跟踪，请使用已有入口。')
        if row and not existing:
            # Never reclassify an already-used legacy stream and its historical signals.
            project = connection.execute('SELECT name FROM projects WHERE id=?', (project_id,)).fetchone()
            if category != 'company' or query['term'] != normalized_public_name(project['name']):
                raise ReviewConflictError('相同检索已有来源，不能改变其历史归属。')
        now = datetime.now(UTC).isoformat()
        if not row:
            connection.execute('''INSERT INTO monitoring_plans
                (id,project_id,kind,target,cadence_minutes,enabled,created_at)
                VALUES (?,?,?,?,?,0,?)''', (plan_id, project_id, kind, query['target'], CADENCE_MINUTES, now))
        if not existing:
            connection.execute('INSERT INTO external_focus VALUES (?,?,?,?,?,?,?)',
                               (plan_id, category, query['term'], provider, CONSENT_VERSION, now, 'local_user'))
        return plan_id
