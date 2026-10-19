"""Local purpose-separated consent, at-most-once dispatch and saved external analysis.

Reuses transport budget primitives, never company-reading permissions. No network
on read, no automatic formal facts, and no implicit inclusion of private context.
"""
import json
import logging
import re
import secrets
import threading
import time
from contextlib import closing, suppress
from dataclasses import asdict
from decimal import Decimal
from uuid import uuid4

from project_watch import analysis_recovery
from project_watch.analysis_failures import CODES, failure_code
from project_watch.external_analysis import (
    OUTPUT_TOKENS,
    RULE_VERSION,
    Selection,
    build_external_packet,
    digest,
    external_packet_current,
    parse_packet,
    selected_material_references,
    validate_external_analysis,
)
from project_watch.external_article_packet import ARTICLE_RULE_VERSION
from project_watch.external_focus import external_reading
from project_watch.model_gateway import BudgetGate, GateRejected
from project_watch.model_provider import ModelProviderConfig
from project_watch.reading_jobs import LEASE_SECONDS, MAX_ATTEMPTS, ReadingDenied, ReadingLimits

_log = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def ensure_external_analysis_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS external_analysis_authorizations (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        config_id TEXT NOT NULL, recipient_json TEXT NOT NULL, scope_json TEXT NOT NULL,
        limits_json TEXT NOT NULL, rule_version TEXT NOT NULL, actor TEXT NOT NULL,
        request_key TEXT NOT NULL, created_at REAL NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','paused','revoked')),
        UNIQUE(project_id,request_key))''')
    db.execute('''CREATE TABLE IF NOT EXISTS external_analysis_authorization_events (
        id INTEGER PRIMARY KEY, authorization_id TEXT NOT NULL REFERENCES external_analysis_authorizations(id),
        status TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS external_analysis_jobs (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        authorization_id TEXT NOT NULL REFERENCES external_analysis_authorizations(id),
        job_key TEXT NOT NULL UNIQUE, packet_json TEXT NOT NULL, input_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','running','dispatched','succeeded','failed','uncertain')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
        lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS external_analysis_dispatches (
        job_id TEXT PRIMARY KEY REFERENCES external_analysis_jobs(id),
        authorization_id TEXT NOT NULL REFERENCES external_analysis_authorizations(id),
        request_bytes INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        reserved_cny TEXT NOT NULL, dispatched_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS external_analysis_results (
        job_id TEXT PRIMARY KEY REFERENCES external_analysis_jobs(id),
        project_id TEXT NOT NULL REFERENCES projects(id), output_json TEXT NOT NULL, created_at REAL NOT NULL)''')
    for table in ('external_analysis_authorization_events', 'external_analysis_dispatches', 'external_analysis_results'):
        for op in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table}
                BEGIN SELECT RAISE(ABORT, 'external analysis history is append only'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS external_analysis_immutable_consent
        BEFORE UPDATE OF project_id,config_id,recipient_json,scope_json,limits_json,rule_version,actor,request_key,created_at
        ON external_analysis_authorizations BEGIN SELECT RAISE(ABORT, 'new scope requires new consent'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS external_analysis_revocation_final
        BEFORE UPDATE OF status ON external_analysis_authorizations WHEN OLD.status='revoked'
        BEGIN SELECT RAISE(ABORT, 'revoked consent is final'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS external_analysis_immutable_input
        BEFORE UPDATE OF project_id,job_key,packet_json,input_digest,created_at ON external_analysis_jobs
        BEGIN SELECT RAISE(ABORT, 'analysis input is immutable'); END''')
    db.execute('CREATE INDEX IF NOT EXISTS external_analysis_jobs_state ON external_analysis_jobs(state,created_at)')
    analysis_recovery.ensure_recovery_schema(db)


class ExternalAnalysisStore:
    def __init__(self, repo):
        self.repo = repo
        with closing(repo._connect()) as db, db:
            ensure_external_analysis_schema(db)
        from project_watch.public_article_store import ArticleStore
        self.articles = ArticleStore(repo)

    def _plans(self, project_id, plan_ids):
        if (not isinstance(plan_ids, list) or not 1 <= len(plan_ids) <= 20
                or any(not isinstance(v, str) or not 1 <= len(v) <= 100 for v in plan_ids)):
            raise ReadingDenied('explicit public scope required')
        result = {}
        for plan in self.repo.list_monitoring_plans(project_id):
            if plan.id in plan_ids and plan.project_id == project_id:
                result[plan.id] = {'kind': plan.kind, 'target': plan.target}
        if set(result) != set(plan_ids):
            raise ReadingDenied('public scope outside project')
        return result

    def preview_scope(self, project_id, *, plan_ids, selections, include_future_news, autopilot_id=None, read_articles=False):
        if type(include_future_news) is not bool or type(read_articles) is not bool:
            raise ReadingDenied('explicit future news choice required')
        plans = self._plans(project_id, plan_ids)
        refs = selected_material_references(self.repo, project_id, selections)
        versions = {}
        if not include_future_news:
            for group in external_reading(self.repo, project_id)['categories']:
                for item in group['items']:
                    if item['plan_id'] in plans:
                        packet = build_external_packet(self.repo, project_id, item['id'])
                        versions[item['id']] = packet['signal_version']
            if not versions:
                raise ReadingDenied('no current story in selected scope')
        scope = dict(plan_scopes=plans, selections=[Selection.model_validate(s).model_dump() for s in selections],
                     material_refs=refs, include_future_news=include_future_news, signal_versions=versions)
        if autopilot_id is not None:
            scope['autopilot_id'] = autopilot_id
        if read_articles:
            scope['read_articles'] = True
        return scope

    def authorize(self, project_id, *, scope, config_id, recipient, actor, request_key, limits):
        if (not re.fullmatch(r'[0-9a-f]{64}', config_id) or not actor or len(actor) > 128
                or not 16 <= len(request_key) <= 100 or not isinstance(limits, ReadingLimits)
                or limits.max_output_tokens != OUTPUT_TOKENS):
            raise ReadingDenied('invalid consent or limits')
        config = ModelProviderConfig(**recipient)
        current = self.preview_scope(project_id, plan_ids=list(scope['plan_scopes']),
                                     selections=scope['selections'], include_future_news=scope['include_future_news'],
                                     autopilot_id=scope.get('autopilot_id'), read_articles=scope.get('read_articles', False))
        if current != scope:
            raise ReadingDenied('disclosed scope changed')
        expected = dict(config_id=config_id, recipient_json=_json(asdict(config)), scope_json=_json(scope),
                        limits_json=_json(asdict(limits)),
                        rule_version=ARTICLE_RULE_VERSION if scope.get('read_articles') else RULE_VERSION, actor=actor)
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._parent(db, scope, project_id, config_id)
            previous = db.execute('SELECT * FROM external_analysis_authorizations WHERE project_id=? AND request_key=?',
                                  (project_id, request_key)).fetchone()
            if previous:
                if any(previous[k] != v for k, v in expected.items()):
                    raise ReadingDenied('request key has different consent')
                grant_id = previous['id']
            else:
                grant_id, now = str(uuid4()), time.time()
                db.execute('INSERT INTO external_analysis_authorizations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (grant_id, project_id, *expected.values(), request_key, now, 'active'))
                db.execute('''INSERT INTO external_analysis_authorization_events
                    (authorization_id,status,actor,created_at) VALUES(?,?,?,?)''', (grant_id, 'active', actor, now))
        return self.status(grant_id)

    @staticmethod
    def _grant(db, grant_id, active=True):
        row = db.execute('SELECT * FROM external_analysis_authorizations WHERE id=?', (grant_id,)).fetchone()
        if row is None or (active and row['status'] != 'active'):
            raise ReadingDenied('external authorization inactive')
        return dict(row)

    def status(self, grant_id):
        with closing(self.repo._connect()) as db:
            grant = self._grant(db, grant_id, active=False)
            charges = db.execute('SELECT reserved_cny FROM external_analysis_dispatches WHERE authorization_id=?',
                                 (grant_id,)).fetchall()
        for field in ('scope', 'limits', 'recipient'):
            grant[field] = json.loads(grant.pop(field+'_json'))
        grant['requests_used'] = len(charges)
        grant['reserved_cny'] = str(sum((Decimal(r[0]) for r in charges), Decimal(0)))
        return grant

    def list_authorizations(self, project_id=None):
        with closing(self.repo._connect()) as db:
            ids = [r[0] for r in db.execute('''SELECT id FROM external_analysis_authorizations
                WHERE (? IS NULL OR project_id=?) ORDER BY created_at DESC,id''', (project_id, project_id))]
        return [self.status(v) for v in ids]

    def set_status(self, grant_id, status, *, actor):
        if status not in {'active', 'paused', 'revoked'} or not actor or len(actor) > 128:
            raise ReadingDenied('invalid status change')
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            grant = self._grant(db, grant_id, active=False)
            if grant['status'] == 'revoked':
                raise ReadingDenied('revocation is final')
            if grant['status'] != status:
                db.execute('UPDATE external_analysis_authorizations SET status=? WHERE id=?', (status, grant_id))
                db.execute('''INSERT INTO external_analysis_authorization_events
                    (authorization_id,status,actor,created_at) VALUES(?,?,?,?)''', (grant_id, status, actor, time.time()))

    @staticmethod
    def _parent(db, scope, project_id, config_id):
        if scope.get('autopilot_id'):
            from project_watch.monitor_auto_analysis import parent_current
            parent_current(db, scope['autopilot_id'], project_id, config_id)

    def _scope(self, grant, packet, db):
        scope = json.loads(grant['scope_json'])
        self._parent(db, scope, grant['project_id'], grant['config_id'])
        expected_version = ARTICLE_RULE_VERSION if scope.get('read_articles') else RULE_VERSION
        if (packet['project_id'] != grant['project_id'] or grant['rule_version'] != expected_version
                or packet['rule_version'] != grant['rule_version']):
            raise ReadingDenied('project or rule changed')
        actual = self._plans(grant['project_id'], list(scope['plan_scopes']))
        if actual != scope['plan_scopes']:
            raise ReadingDenied('public query scope changed')
        signals = self.repo.list_signals(grant['project_id'])
        signal = next((s for s in signals if s.id == packet['signal_id']), None)
        active_ids = {p.id for p in self.repo.list_monitoring_plans(grant['project_id']) if p.enabled}
        if signal is None or signal.plan_id not in actual or signal.plan_id not in active_ids:
            raise ReadingDenied('story outside active selected query')
        if (not scope['include_future_news']
                and scope['signal_versions'].get(packet['signal_id']) != packet['signal_version']):
            raise ReadingDenied('new story version requires consent')
        if ([r for r in packet['references'] if r['kind'] == 'internal'] != scope['material_refs']
                or selected_material_references(self.repo, grant['project_id'], scope['selections']) != scope['material_refs']
                or not external_packet_current(self.repo, packet)):
            raise ReadingDenied('selected evidence changed')

    def enqueue(self, grant_id, packet):
        packet = parse_packet(packet).model_dump()
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            grant = self._grant(db, grant_id)
            self._scope(grant, packet, db)
            key = digest([grant['project_id'], grant['config_id'], packet['rule_version'], digest(packet)])
            prior = db.execute('SELECT * FROM external_analysis_jobs WHERE job_key=?', (key,)).fetchone()
            if prior:
                root_id = prior['id']
                job_id = analysis_recovery.latest_job(db, root_id)
                prior = db.execute('SELECT * FROM external_analysis_jobs WHERE id=?', (job_id,)).fetchone()
                if job_id == root_id and prior['state'] == 'queued' and not db.execute(
                        'SELECT 1 FROM external_analysis_dispatches WHERE job_id=?', (job_id,)).fetchone():
                    db.execute('UPDATE external_analysis_jobs SET authorization_id=? WHERE id=?', (grant_id, job_id))
            else:
                job_id, now = str(uuid4()), time.time()
                db.execute('''INSERT INTO external_analysis_jobs
                    (id,project_id,authorization_id,job_key,packet_json,input_digest,state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'queued',?,?)''',
                    (job_id, grant['project_id'], grant_id, key, _json(packet), digest(packet), now, now))
        return self.get(job_id)

    def get(self, job_id):
        with closing(self.repo._connect()) as db:
            row = db.execute('SELECT * FROM external_analysis_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ReadingDenied('job missing')
        job = dict(row)
        job['packet'] = json.loads(job.pop('packet_json'))
        return job

    def claim(self, job_id):
        now = time.time()
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM external_analysis_jobs WHERE id=?', (job_id,)).fetchone()
            if (row is None or row['state'] != 'queued' or row['attempts'] >= MAX_ATTEMPTS
                    or (row['lease_until'] is not None and row['lease_until'] > now)):
                return None
            self._grant(db, row['authorization_id'])
            db.execute('''UPDATE external_analysis_jobs SET state='running',attempts=attempts+1,
                lease_token=?,lease_until=?,updated_at=? WHERE id=?''',
                (secrets.token_hex(24), now+LEASE_SECONDS, now, job_id))
        return self.get(job_id)

    def pending_retries(self, grant_id):
        """Only explicit, still-current retry packets; never widen their evidence."""
        with closing(self.repo._connect()) as db:
            grant = self._grant(db, grant_id)
            rows = db.execute('''SELECT j.id,j.packet_json FROM external_analysis_jobs j
                JOIN external_analysis_retries r ON r.child_id=j.id
                WHERE j.authorization_id=? AND j.state='queued'
                ORDER BY j.created_at,j.id''', (grant_id,)).fetchall()
            result = []
            for row in rows:
                try:
                    self._scope(grant, json.loads(row['packet_json']), db)
                except (ReadingDenied, ValueError):
                    continue
                result.append(row['id'])
            return result

    def reserve(self, job_id, token, config_id, body):
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM external_analysis_jobs WHERE id=?', (job_id,)).fetchone()
            if (row is None or row['state'] != 'running' or row['lease_token'] != token
                    or row['lease_until'] < time.time()):
                raise ReadingDenied('analysis lease expired')
            grant = self._grant(db, row['authorization_id'])
            if grant['config_id'] != config_id:
                raise ReadingDenied('receiver changed')
            self._scope(grant, json.loads(row['packet_json']), db)
            limits = ReadingLimits(**json.loads(grant['limits_json']))
            gate = BudgetGate(limits.policy(json.loads(grant['recipient_json'])['model']))
            gate.admit(body)
            reserved = Decimal(gate.receipt()['estimated_reserved_cny'] or '0')
            prior = db.execute('SELECT reserved_cny FROM external_analysis_dispatches WHERE authorization_id=?',
                               (grant['id'],)).fetchall()
            total = sum((Decimal(r[0]) for r in prior), Decimal(0))+reserved
            if len(prior) >= limits.max_requests or (limits.max_cost_cny is not None and total > Decimal(limits.max_cost_cny)):
                raise ReadingDenied('external analysis budget exhausted')
            db.execute('INSERT INTO external_analysis_dispatches VALUES(?,?,?,?,?,?)',
                       (job_id, grant['id'], len(body), json.loads(body)['max_tokens'], str(reserved), time.time()))
            db.execute("UPDATE external_analysis_jobs SET state='dispatched',updated_at=? WHERE id=?", (time.time(), job_id))

    def publish(self, job_id, token, result):
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT * FROM external_analysis_jobs WHERE id=?', (job_id,)).fetchone()
            if (job is None or job['state'] != 'dispatched' or job['lease_token'] != token
                    or job['lease_until'] < time.time()):
                raise ReadingDenied('analysis no longer owned')
            # Pausing stops new dispatches, not the completion already sent.
            grant = self._grant(db, job['authorization_id'], active=False)
            if grant['status'] == 'revoked':
                raise ReadingDenied('consent revoked during analysis')
            packet = json.loads(job['packet_json'])
            self._scope(grant, packet, db)
            raw = {k: result[k] for k in ('source_statements', 'relationship', 'implications', 'unknowns')}
            raw['source_statements'] = [{k: v for k, v in s.items() if k != 'kind'} for s in raw['source_statements']]
            raw['implications'] = [{k: v for k, v in s.items() if k != 'kind'} for s in raw['implications']]
            if validate_external_analysis(packet, raw) != result:
                raise ReadingDenied('analysis result is not bound to this input')
            db.execute('INSERT INTO external_analysis_results VALUES(?,?,?,?)',
                       (job_id, job['project_id'], _json(result), time.time()))
            db.execute("UPDATE external_analysis_jobs SET state='succeeded',lease_until=NULL,updated_at=? WHERE id=?",
                       (time.time(), job_id))

    def fail(self, job_id, token, code='unknown'):
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('''UPDATE external_analysis_jobs SET state=CASE
                WHEN state='dispatched' THEN 'uncertain' WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
                lease_token=NULL,lease_until=?,updated_at=? WHERE id=? AND lease_token=?
                AND state IN ('running','dispatched')''', (MAX_ATTEMPTS, time.time()+15, time.time(), job_id, token))
            if changed.rowcount:
                db.execute('INSERT INTO external_analysis_failures(job_id,code,created_at) VALUES(?,?,?)',
                           (job_id, code if code in CODES else 'unknown', time.time()))

    def recover(self, *, now=None):
        now = time.time() if now is None else now
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO external_analysis_failures(job_id,code,created_at)
                SELECT id,'worker_interrupted',? FROM external_analysis_jobs
                WHERE state IN ('running','dispatched') AND lease_until<?''', (now, now))
            db.execute('''UPDATE external_analysis_jobs SET state=CASE WHEN state='dispatched' THEN 'uncertain'
                WHEN attempts>=? THEN 'failed' ELSE 'queued' END, lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE state IN ('running','dispatched') AND lease_until<?''', (MAX_ATTEMPTS, now, now))

    def retry_preview(self, project_id, job_id, config_id):
        return analysis_recovery.retry_preview(self, project_id, job_id, config_id)

    def retry(self, project_id, job_id, config_id, disclosure_digest):
        return analysis_recovery.retry(self, project_id, job_id, config_id, disclosure_digest)

    def failures(self, project_id):
        return analysis_recovery.failures(self, project_id)

    def read(self, project_id):
        items, stale = [], 0
        with closing(self.repo._connect()) as db:
            rows = db.execute('''SELECT r.*,j.packet_json,a.scope_json FROM external_analysis_results r
                JOIN external_analysis_jobs j ON j.id=r.job_id AND j.state='succeeded'
                JOIN external_analysis_authorizations a ON a.id=j.authorization_id
                WHERE r.project_id=? ORDER BY r.created_at DESC,r.job_id DESC''', (project_id,)).fetchall()
        for row in rows:
            packet = json.loads(row['packet_json'])
            parent_id = json.loads(row['scope_json']).get('autopilot_id')
            if parent_id:
                from project_watch.monitor_activation import current_packet
                with closing(self.repo._connect()) as db:
                    parent = db.execute('SELECT packet_json FROM monitor_autopilots WHERE id=?', (parent_id,)).fetchone()
                    try:
                        if parent is None:
                            raise ValueError('parent missing')
                        current_packet(db, json.loads(parent[0]))
                    except ValueError:
                        stale += 1
                        continue
            if not external_packet_current(self.repo, packet):
                stale += 1
                continue
            items.append(dict(id=row['job_id'], recorded_at=row['created_at'], packet=packet,
                              analysis=json.loads(row['output_json'])))
        return dict(project_id=project_id, items=items, stale_count=stale)


class ExternalAnalysisWorker:
    """Driven by the existing single reading loop; no second competing model thread."""
    def __init__(self, store, settings, *, article_reader=None):
        self.store, self.settings = store, settings
        self._stop = threading.Event()
        from project_watch.public_article import ArticleReader
        self.article_reader = article_reader or ArticleReader()

    def start(self):
        self._stop.clear()

    def stop(self):
        self._stop.set()

    def status(self, project_id):
        """Read-only explanation; never creates jobs or changes their ownership."""
        model = self.settings.get()
        config_id = model.authorization_id() if model and hasattr(model, 'authorization_id') else None
        grants = [g for g in self.store.list_authorizations(project_id)
                  if g['config_id'] == config_id and g['rule_version'] in {RULE_VERSION, ARTICLE_RULE_VERSION}]
        active = [g for g in grants if g['status'] == 'active']
        usable = [g for g in active if g['requests_used'] < g['limits']['max_requests']
                  and (g['limits']['max_cost_cny'] is None
                       or Decimal(g['reserved_cny']) < Decimal(g['limits']['max_cost_cny']))]
        reason = ('model_missing' if config_id is None or not hasattr(model, 'analyze_external') else
                  'consent_required' if not grants or all(g['status'] == 'revoked' for g in grants) else
                  'paused' if not active else
                  'budget_reached' if not usable else
                  'model_limit' if model.disclosure().get('remaining_runs', 0) <= 0 else 'ready')
        if reason == 'ready':
            valid = False
            for grant in usable:
                scope = grant['scope']
                try:
                    with closing(self.store.repo._connect()) as db:
                        self.store._parent(db, scope, project_id, grant['config_id'])
                    valid = (self.store._plans(project_id, list(scope['plan_scopes'])) == scope['plan_scopes']
                             and selected_material_references(self.store.repo, project_id, scope['selections'])
                             == scope['material_refs'])
                except (ReadingDenied, ValueError):
                    continue
                if valid:
                    break
            if not valid:
                reason = 'scope_changed'
            elif not any(p.enabled and p.id in g['scope']['plan_scopes']
                         for g in usable for p in self.store.repo.list_monitoring_plans(project_id)):
                reason = 'news_paused'
        with closing(self.store.repo._connect()) as db:
            counts = dict(db.execute('''SELECT state,count(*) FROM external_analysis_jobs j WHERE project_id=?
                AND NOT EXISTS (SELECT 1 FROM external_analysis_retries r WHERE r.parent_id=j.id) GROUP BY state''',
                                     (project_id,)))
        return dict(reason=reason, poll=reason == 'ready', counts=counts)

    def process_one(self):
        self.store.recover()
        model = self.settings.get()
        if (self._stop.is_set() or model is None or not hasattr(model, 'analyze_external')
                or not hasattr(model, 'authorization_id') or model.disclosure().get('remaining_runs', 0) <= 0):
            return None
        config_id = model.authorization_id()
        grants = self.store.list_authorizations()
        for grant in grants:
            limits = grant['limits']
            if (grant['status'] != 'active' or grant['config_id'] != config_id
                    or grant['rule_version'] not in {RULE_VERSION, ARTICLE_RULE_VERSION}
                    or grant['requests_used'] >= limits['max_requests']
                    or (limits['max_cost_cny'] is not None and Decimal(grant['reserved_cny']) >= Decimal(limits['max_cost_cny']))):
                continue
            # Explicit retries retain the approved packet and have priority over
            # ranking. A disappearing homepage item must not strand a job.
            try:
                for job_id in self.store.pending_retries(grant['id']):
                    claimed = self.store.claim(job_id)
                    if claimed is not None:
                        return self._execute(claimed, model, config_id)
            except (ReadingDenied, ValueError):
                continue
            reading = external_reading(self.store.repo, grant['project_id'])
            # Automatic interpretation follows the visible, deduplicated brief;
            # pure market commentary stays available without consuming this budget.
            brief_ids = ({i['id'] for i in reading.get('briefing', {}).get('items', [])}
                         if grant['scope'].get('autopilot_id') else None)
            for group in reading['categories']:
                for story in group['items']:
                    if (story['plan_id'] not in grant['scope']['plan_scopes']
                            or (brief_ids is not None and story['id'] not in brief_ids)):
                        continue
                    # Explicit article consent supersedes title-only dispatch for
                    # the same receiver/background/query. Keep old history and
                    # counters; do not charge both contracts for each new story.
                    if grant['rule_version'] == RULE_VERSION and any(
                            g['rule_version'] == ARTICLE_RULE_VERSION and g['status'] != 'revoked'
                            and g['project_id'] == grant['project_id'] and g['config_id'] == config_id
                            and g['scope']['material_refs'] == grant['scope']['material_refs']
                            and g['scope']['plan_scopes'].get(story['plan_id']) == grant['scope']['plan_scopes'][story['plan_id']]
                            and (g['scope']['include_future_news'] or story['id'] in g['scope']['signal_versions'])
                            for g in grants):
                        continue
                    try:
                        if grant['scope'].get('read_articles'):
                            # Validate live private/parent scope before any network
                            # work. Sending still rechecks the exact final packet.
                            with closing(self.store.repo._connect()) as db:
                                self.store._parent(db, grant['scope'], grant['project_id'], config_id)
                            if (self.store.status(grant['id'])['status'] != 'active'
                                    or self._stop.is_set()
                                    or self.store._plans(grant['project_id'], list(grant['scope']['plan_scopes'])) != grant['scope']['plan_scopes']
                                    or selected_material_references(self.store.repo, grant['project_id'], grant['scope']['selections']) != grant['scope']['material_refs']):
                                continue
                            if (not grant['scope']['include_future_news'] and
                                    build_external_packet(self.store.repo, grant['project_id'], story['id'])['signal_version']
                                    != grant['scope']['signal_versions'].get(story['id'])):
                                continue
                            if not any(p.id == story['plan_id'] and p.enabled for p in self.store.repo.list_monitoring_plans(grant['project_id'])):
                                continue
                            snapshot = self.store.articles.read_once(grant['project_id'], story['id'], self.article_reader)
                            if snapshot is None:
                                continue
                        packet = build_external_packet(self.store.repo, grant['project_id'], story['id'],
                                                       selections=grant['scope']['selections'],
                                                       read_articles=grant['scope'].get('read_articles', False))
                        job = self.store.enqueue(grant['id'], packet)
                        claimed = self.store.claim(job['id'])
                    except (ReadingDenied, ValueError):
                        continue
                    if claimed is None:
                        continue
                    return self._execute(claimed, model, config_id)
        return None

    def _execute(self, claimed, model, config_id):
        job_id, token = claimed['id'], claimed['lease_token']
        def before_send(body):
            active = self.settings.get()
            if (self._stop.is_set() or active is None or not hasattr(active, 'authorization_id')
                    or active.authorization_id() != config_id):
                raise ReadingDenied('active external receiver changed')
            self.store.reserve(job_id, token, config_id, body)
        try:
            result = model.analyze_external(claimed['packet'], before_send=before_send)
            self.store.publish(job_id, token, result)
        except Exception as exc:
            with suppress(ReadingDenied, GateRejected):
                self.store.fail(job_id, token, failure_code(exc))
            _log.warning('external_analysis_not_completed',
                         extra={'job_id': job_id, 'failure_code': failure_code(exc)})
        return self.store.get(job_id)
