"""Durable selected-BP -> model -> public monitoring, with purpose-bound consent.

Selected excerpts must be explicitly approved as public-safe. We do not infer
confidentiality from filenames, keywords, or model promises. No library-wide or
future-version consent. Existing model quota and durable dispatch ledger apply.
"""
import json
import time
from contextlib import closing, suppress
from dataclasses import asdict

from project_watch.monitor_activation import (
    MonitorActivationStore,
    checked_strategy,
    current_packet,
)
from project_watch.monitor_strategy import (
    OUTPUT_TOKENS,
    RULE_VERSION,
    SUPPORTED_VERSIONS,
    Packet,
    digest,
)
from project_watch.reading_jobs import ReadingDenied, ReadingJobs, ReadingLimits

PURPOSE = 'monitor_strategy'


class MonitorAutopilot:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings
        self.stopping = lambda: False
        self.ledger = ReadingJobs(repo)
        self.activation = MonitorActivationStore(repo)
        with closing(repo._connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS monitor_autopilots(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                job_id TEXT NOT NULL UNIQUE REFERENCES reading_jobs(id),
                grant_id TEXT NOT NULL REFERENCES reading_authorizations(id),
                packet_json TEXT NOT NULL, provider TEXT NOT NULL CHECK(provider='bing'),
                created_at REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS monitor_autopilot_results(
                id TEXT PRIMARY KEY REFERENCES monitor_autopilots(id), strategy_json TEXT NOT NULL,
                created_at REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS monitor_autopilot_activation_state(
                id TEXT PRIMARY KEY REFERENCES monitor_autopilots(id), attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0)''')
            for table in ('monitor_autopilots', 'monitor_autopilot_results'):
                for op in ('UPDATE', 'DELETE'):
                    db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()}
                        BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'autopilot history is immutable'); END''')
        from project_watch.monitor_auto_analysis import AutoAnalysisBridge
        self.analysis = AutoAnalysisBridge(repo)

    def authorize(self, packet, *, public_consent, model_consent, expected_config=None, automatic_analysis=False):
        if public_consent is not True or model_consent is not True:
            raise ReadingDenied('selected public-safe excerpts and receiver require consent')
        p = Packet.model_validate(packet).model_dump()
        with closing(self.repo._connect()) as db:
            current_packet(db, p)
        model = self.settings.get()
        if model is None or not hasattr(model, 'plan_monitoring') or not hasattr(model, 'authorization_id'):
            raise ReadingDenied('model unavailable')
        config_id = model.authorization_id()
        if expected_config is not None and config_id != expected_config:
            raise ReadingDenied('receiver changed after disclosure')
        if automatic_analysis:
            if not hasattr(model, 'analyze_external'):
                raise ReadingDenied('model cannot interpret news')
            self.analysis.preview(p)
        key = digest([p, config_id, RULE_VERSION, PURPOSE, 'bing'])
        with closing(self.repo._connect()) as db:
            old = db.execute('SELECT * FROM monitor_autopilots WHERE id=?', (key,)).fetchone()
        if old:
            if automatic_analysis:
                self.analysis.approve(key, p, model)
            return dict(old)  # Retried submission never resumes or resets budget.
        grant = self.ledger.authorize(p['project_id'], config_id=config_id, model=model.config.model,
            purpose=PURPOSE, rule_version=RULE_VERSION, source_hashes=[p['source_sha256']], include_future=False,
            limits=ReadingLimits(max_requests=1, max_request_bytes=300_000, max_output_tokens=OUTPUT_TOKENS),
            actor='local-operator', recipient=asdict(model.config), request_key=key)
        job = self.ledger.enqueue(grant['id'], sources={p['source_id']:p['source_sha256']}, input_digest=digest(p))
        with closing(self.repo._connect()) as db, db:
            current_packet(db, p)
            db.execute('INSERT OR IGNORE INTO monitor_autopilots VALUES(?,?,?,?,?,?,?)',
                (key, p['project_id'], job['id'], grant['id'], json.dumps(p, ensure_ascii=False), 'bing', time.time()))
        if automatic_analysis:
            self.analysis.approve(key, p, model)
        return {'id':key, 'job_id':job['id'], 'grant_id':grant['id']}

    def _current(self, db, row, model):
        if self.stopping():
            raise ReadingDenied('worker stopped')
        grant = self.ledger._grant(db, row['grant_id'])
        saved = db.execute('SELECT strategy_json FROM monitor_autopilot_results WHERE id=?', (row['id'],)).fetchone()
        version_allowed = grant['rule_version'] == RULE_VERSION or (
            grant['rule_version'] in SUPPORTED_VERSIONS and saved is not None
            and json.loads(saved[0]).get('rule_version') == grant['rule_version'])
        if (grant['purpose'] != PURPOSE or not version_allowed
                or model is None or not hasattr(model,'authorization_id')
                or model.authorization_id() != grant['config_id']):
            raise ReadingDenied('receiver or purpose changed')
        current_packet(db, json.loads(row['packet_json']))

    def _activate(self, row, strategy):
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO monitor_autopilot_activation_state(id) VALUES(?)',(row['id'],))
            reserved=db.execute('''UPDATE monitor_autopilot_activation_state SET attempts=attempts+1,next_attempt=?
                WHERE id=? AND attempts<3 AND next_attempt<=?''',(time.time()+30,row['id'],time.time())).rowcount
            if not reserved:
                return None
        return self.activation.activate(json.loads(row['packet_json']), strategy,
            approved_terms=[s['anchor'] for s in strategy['scopes']], provider=row['provider'],
            actor='bp-autopilot', consent=True, allow_partial=True,
            precondition=lambda db:self._current(db,row,self.settings.get()))

    def process_one(self):
        self.ledger.recover()
        model = self.settings.get()
        if model is None or not hasattr(model,'plan_monitoring'):
            return None
        with closing(self.repo._connect()) as db:
            rows = [dict(r) for r in db.execute('''SELECT a.*,r.strategy_json FROM monitor_autopilots a
                LEFT JOIN monitor_autopilot_results r ON r.id=a.id ORDER BY a.created_at,a.id''')]
        for row in rows:
            try:
                with closing(self.repo._connect()) as db:
                    self._current(db,row,model)
            except (ValueError, ReadingDenied):
                continue
            if row['strategy_json']:
                strategy = json.loads(row['strategy_json'])
                key = digest([strategy,row['provider'],sorted(s['anchor'] for s in strategy['scopes'])])
                with closing(self.repo._connect()) as db:
                    done = db.execute('SELECT 1 FROM monitor_activations WHERE activation_key=?',(key,)).fetchone()
                if not done:
                    with suppress(Exception):
                        if self._activate(row,strategy) is not None:
                            return {'id':row['id'], 'state':'activating'}
                else:
                    with suppress(ValueError, ReadingDenied):
                        self.analysis.connect(row, strategy)
                continue
            if model.disclosure().get('remaining_runs',0) <= 0:
                return None
            claimed = self.ledger.claim(row['job_id'])
            if claimed is None:
                continue
            token = claimed['lease_token']
            before_send = self.ledger.before_send(row['job_id'],token,config_id=model.authorization_id(),
                precondition=lambda db,row=row:self._current(db,row,self.settings.get()))

            try:
                packet = json.loads(row['packet_json'])
                strategy = model.plan_monitoring(packet, before_send=before_send)
                _, strategy = checked_strategy(packet,strategy)
                if strategy['rule_version'] != RULE_VERSION:
                    raise ReadingDenied('model strategy version changed')
                # Commit model result and charged-job completion together, before activation.
                with closing(self.repo._connect()) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    self._current(db,row,self.settings.get())
                    job = db.execute('SELECT * FROM reading_jobs WHERE id=?',(row['job_id'],)).fetchone()
                    if (job['state'] != 'dispatched' or job['lease_token'] != token
                            or job['lease_until'] < time.time()):
                        raise ReadingDenied('lost model lease')
                    db.execute('INSERT INTO monitor_autopilot_results VALUES(?,?,?)',
                        (row['id'],json.dumps(strategy,ensure_ascii=False),time.time()))
                    db.execute("UPDATE reading_jobs SET state='succeeded',lease_until=NULL,updated_at=? WHERE id=?",
                        (time.time(),row['job_id']))
                self._activate(row,strategy)
                self.analysis.connect(row, strategy)
            except Exception:
                job = self.ledger.get(row['job_id'])
                if job['state'] == 'running':
                    self.ledger.release_unsent(row['job_id'],token)
                elif job['state'] == 'dispatched':
                    with suppress(ReadingDenied):
                        self.ledger.finish(row['job_id'],token,succeeded=False)
                # No raw provider output, material, credentials or exception messages.
            return self.ledger.get(row['job_id'])
        return None

    def retry_activation(self, pilot_id):
        """Explicit local retry for a saved result; never repeats a model dispatch."""
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM monitor_autopilots WHERE id=?', (pilot_id,)).fetchone()
            if row is None:
                raise ReadingDenied('unknown workflow')
            self._current(db, row, self.settings.get())
            job = db.execute('SELECT state FROM reading_jobs WHERE id=?', (row['job_id'],)).fetchone()
            if job['state'] != 'succeeded':
                raise ReadingDenied('no saved strategy')
            db.execute('UPDATE monitor_autopilot_activation_state SET attempts=0,next_attempt=0 WHERE id=?', (pilot_id,))
            db.execute('INSERT INTO reading_authorization_events(authorization_id,status,actor,created_at) VALUES(?,?,?,?)',
                       (row['grant_id'], 'active', 'local-operator:retry-activation', time.time()))

    def pause(self, pilot_id):
        with closing(self.repo._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM monitor_autopilots WHERE id=?',(pilot_id,)).fetchone()
            if row is None:
                raise ReadingDenied('unknown monitoring workflow')
            grant = self.ledger._grant(db,row['grant_id'],active=False)
            if grant['status'] == 'active':
                db.execute("UPDATE reading_authorizations SET status='paused' WHERE id=?",(grant['id'],))
                db.execute('INSERT INTO reading_authorization_events(authorization_id,status,actor,created_at) VALUES(?,?,?,?)',
                    (grant['id'],'paused','local-operator',time.time()))
            for child in db.execute("SELECT id,scope_json FROM external_analysis_authorizations WHERE project_id=? AND status='active'", (row['project_id'],)).fetchall():
                if json.loads(child['scope_json']).get('autopilot_id') == pilot_id:
                    db.execute("UPDATE external_analysis_authorizations SET status='paused' WHERE id=?", (child['id'],))
                    db.execute('INSERT INTO external_analysis_authorization_events(authorization_id,status,actor,created_at) VALUES(?,?,?,?)',
                               (child['id'], 'paused', 'bp-autopilot', time.time()))
            # Stop only plans this exact workflow created, not shared/human plans.
            result = db.execute('SELECT strategy_json FROM monitor_autopilot_results WHERE id=?',(pilot_id,)).fetchone()
            if result:
                strategy = json.loads(result[0])
                key = digest([strategy,row['provider'],sorted(s['anchor'] for s in strategy['scopes'])])
                db.execute('''UPDATE monitoring_plans SET enabled=0 WHERE id IN (
                    SELECT p.plan_id FROM monitor_activation_plans p JOIN monitor_activations a ON a.id=p.activation_id
                    WHERE a.activation_key=? AND p.owned=1)''',(key,))

    def read(self, project_id):
        with closing(self.repo._connect()) as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM monitor_autopilots WHERE project_id=? ORDER BY created_at DESC,id', (project_id,))]
        result = []
        for row in rows:
            grant = self.ledger.status(row['grant_id'])
            job = self.ledger.get(row['job_id'])
            state = job['state']
            with closing(self.repo._connect()) as db:
                version = db.execute('SELECT rule_version FROM reading_authorizations WHERE id=?', (row['grant_id'],)).fetchone()[0]
                saved = db.execute('SELECT 1 FROM monitor_autopilot_results WHERE id=?', (row['id'],)).fetchone()
            if version != RULE_VERSION and not saved:
                state = 'strategy_changed'
            try:
                with closing(self.repo._connect()) as db:
                    current_packet(db,json.loads(row['packet_json']))
            except ValueError:
                state = 'material_changed'
            if grant['status'] != 'active':
                state = 'paused'
            elif state == 'succeeded':
                state = 'activating'
                with closing(self.repo._connect()) as db:
                    found = db.execute('SELECT strategy_json FROM monitor_autopilot_results WHERE id=?',(row['id'],)).fetchone()
                    if found:
                        strategy=json.loads(found[0])
                        key=digest([strategy,row['provider'],sorted(s['anchor'] for s in strategy['scopes'])])
                        activated=db.execute('SELECT id FROM monitor_activations WHERE activation_key=?',(key,)).fetchone()
                        if activated:
                            skipped = db.execute('SELECT count(*) FROM monitor_activation_skips WHERE activation_id=?', (activated[0],)).fetchone()[0]
                            plans=db.execute('SELECT plan_id FROM monitor_activation_plans WHERE activation_id=?',(activated[0],)).fetchall()
                            current_plans=[self.repo.get_monitoring_plan(r[0]) for r in plans]
                            states={p.last_run_status for p in current_plans if p.enabled}
                            state='collecting' if states & {None,'not_run'} else 'partial_failure' if states & {'unreachable','rate_limited'} else 'checked'
                            if not states:
                                state='paused'
                            elif state=='checked' and (skipped or any(not p.enabled for p in current_plans)):
                                state='partial_coverage'
                        else:
                            retry=db.execute('SELECT attempts FROM monitor_autopilot_activation_state WHERE id=?',(row['id'],)).fetchone()
                            if retry and retry[0] >= 3:
                                state='activation_failed'
            result.append({'id':row['id'],'source_id':json.loads(row['packet_json'])['source_id'],
                'state':state,'requests_used':grant['requests_used'],'provider':row['provider'],
                'analysis':self.analysis.status(row['id'], project_id)})
        return result
