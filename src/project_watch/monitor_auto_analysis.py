"""Explicit BP-to-news consent bridge. Never upgrades old consent on a read."""
import json
import time
from contextlib import closing
from dataclasses import asdict

from project_watch.external_analysis import OUTPUT_TOKENS, selected_material_references
from project_watch.external_analysis_jobs import ExternalAnalysisStore
from project_watch.monitor_activation import current_packet
from project_watch.monitor_strategy import RULE_VERSION, SUPPORTED_VERSIONS, digest
from project_watch.reading_jobs import ReadingDenied, ReadingLimits

MAX_REQUESTS = 20


def parent_current(db, pilot_id, project_id, config_id):
    row = db.execute('''SELECT a.*,g.status,g.config_id,g.rule_version,g.purpose
        FROM monitor_autopilots a JOIN reading_authorizations g ON g.id=a.grant_id
        WHERE a.id=?''', (pilot_id,)).fetchone()
    if (row is None or row['project_id'] != project_id or row['status'] != 'active'
            or row['config_id'] != config_id or row['purpose'] != 'monitor_strategy'):
        raise ReadingDenied('parent monitoring consent changed')
    if row['rule_version'] != RULE_VERSION:
        saved = db.execute('SELECT strategy_json FROM monitor_autopilot_results WHERE id=?', (pilot_id,)).fetchone()
        if (row['rule_version'] not in SUPPORTED_VERSIONS or saved is None
                or json.loads(saved[0]).get('rule_version') != row['rule_version']):
            raise ReadingDenied('parent monitoring strategy changed')
    current_packet(db, json.loads(row['packet_json']))
    return row


class AutoAnalysisBridge:
    def __init__(self, repo):
        self.repo = repo
        self.store = ExternalAnalysisStore(repo)
        with closing(repo._connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS monitor_autopilot_analysis (
                pilot_id TEXT PRIMARY KEY REFERENCES monitor_autopilots(id),
                consent_json TEXT NOT NULL, created_at REAL NOT NULL)''')
            for op in ('UPDATE', 'DELETE'):
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS monitor_auto_analysis_no_{op.lower()}
                    BEFORE {op} ON monitor_autopilot_analysis
                    BEGIN SELECT RAISE(ABORT,'analysis consent is immutable'); END''')
            db.execute('''CREATE TABLE IF NOT EXISTS monitor_autopilot_analysis_v2 (
                pilot_id TEXT PRIMARY KEY REFERENCES monitor_autopilots(id),
                consent_json TEXT NOT NULL, created_at REAL NOT NULL)''')
            for op in ('UPDATE', 'DELETE'):
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS monitor_auto_analysis_v2_no_{op.lower()}
                    BEFORE {op} ON monitor_autopilot_analysis_v2
                    BEGIN SELECT RAISE(ABORT,'article analysis consent is immutable'); END''')

    def preview(self, packet):
        with closing(self.repo._connect()) as db:
            current_packet(db, packet)
        fragments = self.repo.list_fragments(packet['source_id'])
        selections = []
        # A disclosed subset, never an inferred lookup across the material library.
        for excerpt in packet['excerpts'][:3]:
            fragment = next(f for f in fragments if f.locator == excerpt['locator'] and excerpt['text'] in f.text)
            text = excerpt['text'][:3000]
            selections.append(dict(source_id=packet['source_id'], ordinal=fragment.ordinal,
                                   offset=fragment.text.index(excerpt['text']), length=len(text)))
        refs = selected_material_references(self.repo, packet['project_id'], selections)
        return dict(selections=selections, material_refs=refs, include_future_news=True,
                    max_requests=MAX_REQUESTS, max_output_tokens=OUTPUT_TOKENS, read_articles=True,
                    public_source_note='读取已发现新闻链接的公开正文，发送其片段与选定 BP 背景；读取失败时用标题与摘要。不登录、不绕过付费限制。')

    def approve(self, pilot_id, packet, model):
        if not hasattr(model, 'analyze_external'):
            raise ReadingDenied('model cannot interpret news')
        consent = dict(**self.preview(packet), config_id=model.authorization_id(), recipient=asdict(model.config))
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            parent = parent_current(db, pilot_id, packet['project_id'], consent['config_id'])
            if json.loads(parent['packet_json']) != packet:
                raise ReadingDenied('different BP packet')
            old = db.execute('SELECT consent_json FROM monitor_autopilot_analysis_v2 WHERE pilot_id=?', (pilot_id,)).fetchone()
            if old and json.loads(old[0]) != consent:
                raise ReadingDenied('analysis scope changed')
            db.execute('INSERT OR IGNORE INTO monitor_autopilot_analysis_v2 VALUES(?,?,?)',
                       (pilot_id, json.dumps(consent, ensure_ascii=False), time.time()))

    def connect(self, row, strategy):
        key = digest([strategy, row['provider'], sorted(s['anchor'] for s in strategy['scopes'])])
        with closing(self.repo._connect()) as db:
            intent = db.execute('SELECT consent_json FROM monitor_autopilot_analysis WHERE pilot_id=?', (row['id'],)).fetchone()
            article_intent = db.execute('SELECT consent_json FROM monitor_autopilot_analysis_v2 WHERE pilot_id=?', (row['id'],)).fetchone()
            intent = article_intent or intent
            if intent is None:
                return
            consent = json.loads(intent[0])
            parent_current(db, row['id'], row['project_id'], consent['config_id'])
            plan_ids = [r[0] for r in db.execute('''SELECT p.plan_id FROM monitor_activation_plans p
                JOIN monitor_activations a ON a.id=p.activation_id WHERE a.activation_key=?''', (key,))]
        if not plan_ids:
            return
        scope = self.store.preview_scope(row['project_id'], plan_ids=plan_ids, selections=consent['selections'],
                                         include_future_news=True, autopilot_id=row['id'], read_articles=consent.get('read_articles', False))
        if scope['material_refs'] != consent['material_refs']:
            raise ReadingDenied('approved BP subset changed')
        return self.store.authorize(row['project_id'], scope=scope, config_id=consent['config_id'],
            recipient=consent['recipient'], actor='bp-autopilot',
            request_key=digest(['auto-analysis-v2' if consent.get('read_articles') else 'auto-analysis-v1', row['id']]),
            limits=ReadingLimits(max_requests=consent['max_requests'], max_output_tokens=OUTPUT_TOKENS))

    def status(self, pilot_id, project_id):
        with closing(self.repo._connect()) as db:
            intent = db.execute('SELECT 1 FROM monitor_autopilot_analysis WHERE pilot_id=?', (pilot_id,)).fetchone()
            article_intent = db.execute('SELECT 1 FROM monitor_autopilot_analysis_v2 WHERE pilot_id=?', (pilot_id,)).fetchone()
            intent = article_intent or intent
        if not intent:
            return dict(enabled=False, requests_used=0, max_requests=MAX_REQUESTS)
        grant = next((g for g in self.store.list_authorizations(project_id)
                      if g['scope'].get('autopilot_id') == pilot_id), None)
        return dict(enabled=True, requests_used=grant['requests_used'] if grant else 0,
                    max_requests=MAX_REQUESTS, status=grant['status'] if grant else 'waiting', read_articles=bool(article_intent))
