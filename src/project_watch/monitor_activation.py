"""Atomic, restart-safe activation of an evidence-bound, approved strategy.

This bridge never calls an LLM or discovers consent. Its caller must separately
authorize the public terms. Existing paused/human scopes are never reactivated.
Collection is performed by the existing durable monitoring scheduler.
"""
import json
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

from project_watch.external_focus import ExternalFocusStore, public_query
from project_watch.monitor_strategy import Packet, digest, validate_strategy


def ensure_activation_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS monitor_activations (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        source_id TEXT NOT NULL REFERENCES sources(id), input_digest TEXT NOT NULL,
        activation_key TEXT NOT NULL UNIQUE, strategy_json TEXT NOT NULL,
        terms_json TEXT NOT NULL, provider TEXT NOT NULL, actor TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS monitor_activation_plans (
        activation_id TEXT NOT NULL REFERENCES monitor_activations(id),
        plan_id TEXT NOT NULL REFERENCES monitoring_plans(id),
        owned INTEGER NOT NULL CHECK(owned IN (0,1)), scope_json TEXT NOT NULL,
        PRIMARY KEY(activation_id,plan_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS monitor_activation_skips (
        activation_id TEXT NOT NULL REFERENCES monitor_activations(id),
        category TEXT NOT NULL, anchor TEXT NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY(activation_id,category,anchor))''')
    for table in ('monitor_activations', 'monitor_activation_plans', 'monitor_activation_skips'):
        for op in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()}
                BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'activation history is immutable'); END''')


def checked_strategy(packet, strategy):
    """Re-derive attached evidence; never trust client-supplied quotes or digests."""
    try:
        p = Packet.model_validate(packet).model_dump()
        def ids(refs):
            return [r['excerpt_id'] for r in refs]
        raw = dict(understanding=dict(text=strategy['understanding']['text'],
                                     evidence=ids(strategy['understanding']['source_refs'])),
                   scopes=[dict(category=s['category'], anchor=s['anchor'], question=s['question'],
                                rationale=s['rationale'], evidence=ids(s['source_refs']))
                           for s in strategy['scopes']], unknowns=strategy['unknowns'])
        checked = validate_strategy(p, raw, rule_version=strategy['rule_version'])
        if checked != strategy:
            raise ValueError('strategy snapshot changed')
        return p, checked
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid strategy snapshot') from None


def current_packet(db, p):
    source = db.execute('SELECT * FROM sources WHERE id=?', (p['source_id'],)).fetchone()
    if (not source or source['project_id'] != p['project_id'] or source['kind'] != 'document'
            or source['content_sha256'] != p['source_sha256'] or source['reference'] != p['reference']):
        raise ValueError('source snapshot changed')
    latest = db.execute('''SELECT id FROM sources WHERE project_id=? AND kind='document' AND reference=?
        ORDER BY created_at DESC,id DESC LIMIT 1''', (p['project_id'], p['reference'])).fetchone()
    if latest['id'] != p['source_id']:
        raise ValueError('newer source version exists')
    fragments = db.execute('SELECT locator,text FROM document_fragments WHERE source_id=?', (p['source_id'],)).fetchall()
    for excerpt in p['excerpts']:
        if not any(r['locator'] == excerpt['locator'] and excerpt['text'] in r['text'] for r in fragments):
            raise ValueError('excerpt not in parsed source')


class MonitorActivationStore:
    def __init__(self, repo, *, initialize=True):
        self.repo = repo
        if initialize:
            with closing(repo._connect()) as db, db:
                ensure_activation_schema(db)

    def activate(self, packet, strategy, *, approved_terms, provider, actor, consent, precondition=None, allow_partial=False):
        p, checked = checked_strategy(packet, strategy)
        scopes = checked['scopes']
        terms = [s['anchor'] for s in scopes]
        if (consent is not True or not isinstance(approved_terms, list)
                or len(approved_terms) != len(terms) or set(approved_terms) != set(terms)
                or not isinstance(actor, str) or not 1 <= len(actor) <= 128):
            raise ValueError('explicit public term approval required')
        # Preflight all scopes before any write; queries contain only approved anchors.
        for scope in scopes:
            public_query(scope['category'], scope['anchor'], provider)
        key = digest([checked, provider, sorted(terms)])
        now = datetime.now(UTC)
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current_packet(db, p)
            if precondition is not None:
                precondition(db)
            previous = db.execute('SELECT id FROM monitor_activations WHERE activation_key=?', (key,)).fetchone()
            if previous:
                return {'id': previous['id'], 'created': False}
            activation_id = str(uuid4())
            db.execute('INSERT INTO monitor_activations VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (activation_id, p['project_id'], p['source_id'], checked['input_digest'], key,
                        json.dumps(checked, ensure_ascii=False), json.dumps(terms, ensure_ascii=False),
                        provider, actor, now.isoformat()))
            focus = ExternalFocusStore(self.repo)
            for scope in scopes:
                query = public_query(scope['category'], scope['anchor'], provider)
                kind = 'brave_news' if provider == 'brave' else 'rss'
                # Reuse an already authorized, exactly matching scope even when
                # the provider differs. No relabel, cadence change or resume.
                shared = db.execute('''SELECT p.id FROM monitoring_plans p JOIN external_focus f ON f.plan_id=p.id
                    WHERE p.project_id=? AND f.category=? AND f.term=?
                    ORDER BY p.enabled DESC,p.created_at,p.id LIMIT 1''',
                    (p['project_id'], scope['category'], scope['anchor'])).fetchone()
                existing = shared or db.execute('SELECT id FROM monitoring_plans WHERE project_id=? AND kind=? AND target=?',
                                      (p['project_id'], kind, query['target'])).fetchone()
                from project_watch.repository import ReviewConflictError
                try:
                    plan_id = shared['id'] if shared else focus.create_in_transaction(
                        db, p['project_id'], category=scope['category'], term=scope['anchor'], provider=provider)
                except ReviewConflictError:
                    if not allow_partial:
                        raise
                    # Do not relabel a human/historical stream, nor stop other scopes.
                    db.execute('INSERT INTO monitor_activation_skips VALUES(?,?,?,?)',
                               (activation_id, scope['category'], scope['anchor'], 'existing_scope_conflict'))
                    continue
                owned = existing is None
                db.execute('INSERT INTO monitor_activation_plans VALUES(?,?,?,?)',
                           (activation_id, plan_id, int(owned), json.dumps(scope, ensure_ascii=False)))
                if owned:
                    db.execute('UPDATE monitoring_plans SET enabled=1 WHERE id=?', (plan_id,))
                    self.repo._enqueue_monitoring_job(db, project_id=p['project_id'], plan_id=plan_id,
                        idempotency_key='external-focus:first-run:v1', trigger='strategy', now=now)
        return {'id': activation_id, 'created': True}

    def read(self, project_id):
        with closing(self.repo._connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_activations'").fetchone():
                return None
            latest = db.execute('SELECT * FROM monitor_activations WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 1',
                                (project_id,)).fetchone()
            if latest is None:
                return None
            rows = db.execute('''SELECT a.*,p.enabled FROM monitor_activation_plans a
                JOIN monitoring_plans p ON p.id=a.plan_id WHERE a.activation_id=?''', (latest['id'],)).fetchall()
            skipped = [dict(r) for r in db.execute('SELECT category,anchor,reason FROM monitor_activation_skips WHERE activation_id=?', (latest['id'],))] if db.execute("SELECT 1 FROM sqlite_master WHERE name='monitor_activation_skips'").fetchone() else []
        scopes, successes, failures, pending, paused = [], 0, 0, 0, 0
        for row in rows:
            plan = self.repo.get_monitoring_plan(row['plan_id'])
            status = plan.last_run_status
            if not row['enabled']:
                paused += 1
            elif status in {'completed', 'no_results'}:
                successes += 1
            elif status in {'unreachable', 'rate_limited'}:
                failures += 1
            else:
                pending += 1
            scope = json.loads(row['scope_json'])
            scopes.append({**scope, 'plan_id': plan.id, 'enabled': bool(row['enabled']),
                           'last_status': status, 'owned': bool(row['owned'])})
        coverage = ('paused' if paused == len(rows) else 'partial' if successes and (failures or pending or paused)
                    else 'failed' if failures and not successes else 'pending' if pending else 'checked')
        strategy = json.loads(latest['strategy_json'])
        if skipped:
            coverage = 'partial' if rows else 'conflict'
        return {'id': latest['id'], 'project_id': project_id, 'source_id': latest['source_id'],
                'understanding': strategy['understanding'], 'scopes': scopes,
                'unknowns': strategy['unknowns'] + [f'“{s["anchor"]}”与历史监控分类或频率冲突，本次未覆盖；原有设置保留。' for s in skipped],
                'skipped': skipped,
                'coverage': coverage, 'succeeded': successes, 'failed': failures, 'pending': pending,
                'paused': paused, 'created_at': latest['created_at'], 'formal_facts': False}
