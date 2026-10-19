"""Explicit same-term fallback, durable provider backoff, and acquisition receipts.

No extra source without a project-scope grant. Two requests maximum per collection;
no LLM, arbitrary URL expansion, paid provider or background retry loop here.
"""
import re
import time
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

from project_watch.bing_news import BingNewsAdapter, bing_news_target, is_bing_news_target
from project_watch.external_focus import ExternalFocusStore, public_query
from project_watch.monitoring import AdapterResult, items_to_signals, select_project_items
from project_watch.public_search import is_gdelt_rss_target


class SourceFallbackStore:
    def __init__(self, repo, *, clock=time.time, initialize=True):
        self.repo, self.clock = repo, clock
        if initialize:
            with closing(repo._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('''CREATE TABLE IF NOT EXISTS monitor_fallback_grants(
                    plan_id TEXT PRIMARY KEY REFERENCES monitoring_plans(id),
                    primary_target TEXT NOT NULL, alternate_target TEXT NOT NULL,
                    actor TEXT NOT NULL, created_at REAL NOT NULL)''')
                db.execute('''CREATE TABLE IF NOT EXISTS monitor_provider_backoff(
                    provider TEXT PRIMARY KEY, next_at REAL NOT NULL)''')
                db.execute('''CREATE TABLE IF NOT EXISTS monitor_source_attempts(
                    id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES monitoring_plans(id),
                    run_key TEXT NOT NULL, provider TEXT NOT NULL, state TEXT NOT NULL,
                    error_code TEXT, item_count INTEGER NOT NULL, observed_at REAL NOT NULL)''')
                columns = {r[1] for r in db.execute('PRAGMA table_info(monitor_source_attempts)')}
                if 'request_variant' not in columns:
                    db.execute("ALTER TABLE monitor_source_attempts ADD COLUMN request_variant TEXT NOT NULL DEFAULT 'primary'")
                for table in ('monitor_fallback_grants','monitor_source_attempts'):
                    for op in ('UPDATE','DELETE'):
                        db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()}
                            BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'source history is immutable'); END''')

    def approve(self, plan_id, *, consent, actor):
        if consent is not True or not isinstance(actor, str) or not 1 <= len(actor) <= 128:
            raise ValueError('explicit source approval required')
        plan = self.repo.get_monitoring_plan(plan_id)
        scope = ExternalFocusStore(self.repo).get(plan_id)
        if (not plan or not scope or scope['provider'] != 'gdelt'
                or plan.target != public_query(scope['category'],scope['term'],'gdelt')['target']):
            raise ValueError('only evidence-bound public scopes can use the alternate')
        with closing(self.repo._connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO monitor_fallback_grants VALUES(?,?,?,?,?)',
                (plan.id,plan.target,bing_news_target(scope['term']),actor,self.clock()))

    def _reserve(self, provider):
        now = self.clock()
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT next_at FROM monitor_provider_backoff WHERE provider=?',(provider,)).fetchone()
            if row and row[0] > now:
                return False
            db.execute('INSERT INTO monitor_provider_backoff VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET next_at=excluded.next_at',
                       (provider,now+(6 if provider == 'gdelt' else 0)))
        return True

    def _attempt(self, plan, key, provider, target, adapter, *, variant='primary'):
        current = self.repo.get_monitoring_plan(plan.id)
        if current is None or not current.enabled or current.target != plan.target:
            return AdapterResult(unavailable_error='monitor_paused')
        reserved = self._reserve(provider)
        if reserved:
            try:
                result = adapter.fetch(target)
            except Exception:
                result = AdapterResult(unavailable_error='adapter_error')
        else:
            result = AdapterResult(unavailable_error='rate_limited')
        error = result.unavailable_error
        if error and not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', error):
            result = AdapterResult(unavailable_error='adapter_error')
            error = 'adapter_error'
        with closing(self.repo._connect()) as db, db:
            if reserved and error == 'rate_limited':
                seconds = result.retry_after_seconds or 60
                delay = max(5,min(seconds,86400))
                db.execute('UPDATE monitor_provider_backoff SET next_at=max(next_at,?) WHERE provider=?',
                           (self.clock()+delay,provider))
            db.execute('INSERT INTO monitor_source_attempts(plan_id,run_key,provider,state,error_code,item_count,observed_at,request_variant) VALUES(?,?,?,?,?,?,?,?)',
                       (plan.id,key,provider,'cooldown' if not reserved else 'failed' if error else 'succeeded',
                        error,len(result.items),self.clock(),variant))
        return result

    def collect(self, plan, key, primary, alternate=None):
        provider = 'gdelt' if is_gdelt_rss_target(plan.target) else 'bing' if is_bing_news_target(plan.target) else None
        if provider is None:
            return primary.fetch(plan.target)
        result = self._attempt(plan,key,provider,plan.target,primary)
        if provider == 'bing' and not result.unavailable_error:
            term = parse_qs(urlsplit(plan.target).query)['q'][0]
            matched = select_project_items(result.items,term)
            if not any(s.freshness == 'current' for s in items_to_signals(matched)):
                extra = self._attempt(plan,key,'bing',bing_news_target(term,recent=True),primary,variant='recent')
                if extra.unavailable_error == 'monitor_paused':
                    return extra
                if not extra.unavailable_error:
                    # Prefer the second response for a repeated identity; retain older/unmatched
                    # items so the runner can classify them with its unchanged evidence rules.
                    merged = {i.external_id:i for i in (*result.items,*extra.items)}
                    return AdapterResult(items=tuple(merged.values()))
                # The supplementary failure remains explicit in append-only receipts.
            return result
        if not result.unavailable_error or result.unavailable_error == 'monitor_paused':
            return result
        with closing(self.repo._connect()) as db:
            grant = db.execute('SELECT * FROM monitor_fallback_grants WHERE plan_id=?',(plan.id,)).fetchone()
        if grant and provider == 'gdelt' and grant['primary_target'] == plan.target:
            return self._attempt(plan,key,'bing',grant['alternate_target'],alternate or BingNewsAdapter())
        return result

    def attempts(self, plan_id, key):
        with closing(self.repo._connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM monitor_source_attempts WHERE plan_id=? AND run_key=? ORDER BY id',
                                                (plan_id,key))]
