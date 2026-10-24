"""Durable, local exploration control. No model calls and no project scheduler changes.

An exploration may sleep while ordinary monitoring keeps collecting evidence. Only
new, routed evidence wakes it; replaying a job or touching an event timestamp does not.
"""
from __future__ import annotations

import hashlib
import json

from project_watch.studio.store import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_explorations (
    watch_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version TEXT NOT NULL,
    generation INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
    no_gain INTEGER NOT NULL DEFAULT 0, gain INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_exploration_seen (
    watch_id TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY(watch_id,token)
);
CREATE TABLE IF NOT EXISTS studio_exploration_cycles (
    watch_id TEXT NOT NULL, cycle TEXT NOT NULL, generation INTEGER NOT NULL,
    version TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(watch_id,cycle)
);
CREATE TABLE IF NOT EXISTS studio_exploration_history (
    watch_id TEXT NOT NULL, generation INTEGER NOT NULL, state TEXT NOT NULL,
    reason TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

NO_GAIN_ROUNDS = 3
REASONS = {
    'exploring': '探索中', 'new_evidence': '新证据唤醒', 'answered': '本次问题已回答',
    'no_gain': '连续三轮无新线索', 'relation_limit': '已到关联边界',
    'capacity_limit': '已到关注点容量', 'source_failure': '来源失败，等待重试',
    'request_budget': '本轮调用额度已到，等待后续运行',
    'waiting_recovery': '等待确认恢复', 'waiting_event': '等待新证据',
}


class Explorations:
    def __init__(self, monitor):
        self.monitor = monitor

    @staticmethod
    def managed(watch):
        return (watch.get('scope_kind') != 'relation'
                and (watch.get('tier') == 'topic' or watch.get('cadence') == 'event'))

    def view(self, project_id):
        with self.monitor._db() as db:
            return {r['watch_id']: {**dict(r), 'label': REASONS.get(r['reason'], r['reason'])}
                    for r in db.execute('SELECT * FROM studio_explorations WHERE project_id=?', (project_id,))}

    def tokens(self, watch, ledger=None, *, event_filter=None, legacy_aliases=None):
        """Content, not collection clocks. Only already-routed, non-dismissed inputs.

        Material routing is revalidated against consent/binding/current versions by
        the question ledger. Raw commercial records never enter this path.
        """
        tokens = set()
        # The DSH project's routing policy survives restarts and other evidence
        # arrival paths. Internal questions/materials retain their old behavior.
        if (event_filter is None and watch.get('visibility') == 'public'
                and (watch.get('scope_kind') in ('relation', 'question')
                     or self.monitor.store.get_state('dsh-public-routing:' + watch['project_id']) == 'focus-v1')):
            from project_watch.studio.questions import QuestionLedger

            project = self.monitor.store.get_project(watch['project_id']) or {}
            def event_filter(w, row):
                return QuestionLedger.public_row_match(w, project, row)
        with self.monitor._db() as db:
            public_rows = None
            if watch.get('visibility') == 'public':
                from project_watch.studio.questions import QuestionLedger
                public_rows = {r["id"]: r for r in QuestionLedger.public_event_rows(db, watch["project_id"])}
            for row in db.execute('''SELECT DISTINCT i.id,i.title,i.summary,i.snippet,i.body,i.body_status,ev.content_hash,
                e.title AS event_title,e.summary AS event_summary
                FROM studio_watch_hits h JOIN studio_items i ON i.id=h.item_id
                JOIN studio_events e ON e.id=i.event_id
                LEFT JOIN studio_item_evidence ie ON ie.item_id=i.id
                LEFT JOIN studio_evidence_versions ev ON ev.id=ie.evidence_version_id AND ev.project_id=i.project_id
                WHERE h.watch_id=? AND i.project_id=? AND e.project_id=? AND i.relevant=1 AND e.status!='dismissed' ''',
                (watch['id'], watch['project_id'], watch['project_id'])):
                candidate = dict(row) if public_rows is None else public_rows.get(row["id"])
                if candidate is None:
                    continue
                if event_filter is not None and watch.get('visibility') == 'public' and not event_filter(watch, candidate):
                    continue
                payload = row['content_hash'] or json.dumps(
                    [' '.join(str(v or '').split()) for v in (row['title'], row['snippet'],
                     row['body'] if row['body_status'] in ('article_body', 'partial_body', 'search_summary') else '')], ensure_ascii=False)
                legacy = 'event:' + hashlib.sha256(payload.encode()).hexdigest()
                if watch.get('visibility') != 'public':
                    tokens.add(legacy)
                    continue  # Internal evidence/disclosure semantics are unchanged.
                # EvidenceStore hashes include provenance/locators. Agent intake
                # has no such binding and truncates its snippet differently.
                # Neither representation difference is new research evidence.
                # Use all retained source text (not the 12k item preview), and
                # never a generated event summary or collection timestamp.
                body = candidate.get('body') if candidate.get('body_status') in (
                    'article_body', 'partial_body', 'search_summary') else ''
                text = ' '.join(str(body or candidate.get('snippet') or candidate.get('title') or '').split())
                token = 'event-text-v2:' + hashlib.sha256(text.encode()).hexdigest()
                tokens.add(token)
                # Upgrade existing seen ledgers lazily, without waking all old
                # stopped branches or swallowing genuinely changed source text.
                # Only aliases of currently routed, eligible rows may bridge.
                if legacy_aliases is not None:
                    legacy_aliases.setdefault(token, set()).add(legacy)
            if ledger is not None:
                for material in ledger._material_rows(db, watch['project_id'], watch):
                    version = material['ref']['evidence_version_id']
                    row = db.execute("SELECT json_extract(record,'$.raw_content_hash') FROM studio_evidence_versions WHERE id=? AND project_id=?",
                                     (version, watch['project_id'])).fetchone()
                    tokens.add('material:' + (row[0] if row and row[0] else version))
        return tokens

    @staticmethod
    def _valid(db, watch):
        row = db.execute('SELECT status,lifecycle_version,tier,cadence,scope_kind FROM studio_watchpoints WHERE id=? AND project_id=?',
                         (watch['id'], watch['project_id'])).fetchone()
        return (row and row['status'] != 'retired' and Explorations.managed(dict(row))
                and str(row['lifecycle_version']) == str(watch.get('lifecycle_version', '0')))

    @staticmethod
    def _history(db, row, evidence=()):
        db.execute('INSERT INTO studio_exploration_history VALUES (?,?,?,?,?,?)',
                   (row['watch_id'], row['generation'], row['state'], row['reason'],
                    json.dumps(sorted(evidence)), now()))

    def refresh(self, project_id, ledger=None, *, event_filter=None):
        """Called at evidence/collection boundaries, never by a status GET."""
        for watch in self.monitor.watchpoints(project_id):
            if watch['status'] == 'retired' or not self.managed(watch):
                continue
            aliases = {}
            tokens = self.tokens(watch, ledger, event_filter=event_filter, legacy_aliases=aliases)
            version = str(watch.get('lifecycle_version', '0'))
            with self.monitor._db() as db, db:
                db.execute('BEGIN IMMEDIATE')
                if not self._valid(db, watch):
                    continue
                found = db.execute('SELECT * FROM studio_explorations WHERE watch_id=?', (watch['id'],)).fetchone()
                if not found or found['version'] != version:
                    generation = found['generation'] + 1 if found else 1
                    state, reason = ('waiting', 'waiting_event') if watch['cadence'] == 'event' else ('active', 'exploring')
                    db.execute('INSERT OR REPLACE INTO studio_explorations VALUES (?,?,?,?,?,?,0,0,?)',
                               (watch['id'], project_id, version, generation, state, reason, now()))
                    db.execute("UPDATE studio_watchpoints SET completed_queries='[]' WHERE id=?", (watch['id'],))
                    row = {'watch_id': watch['id'], 'generation': generation, 'state': state, 'reason': reason}
                    self._history(db, row)
                    # Existing evidence is the baseline, not an event that just arrived.
                    db.executemany('INSERT OR IGNORE INTO studio_exploration_seen VALUES (?,?)',
                                   [(watch['id'], t) for t in tokens])
                    continue
                row = dict(found)
                seen = {r[0] for r in db.execute('SELECT token FROM studio_exploration_seen WHERE watch_id=?', (watch['id'],))}
                migrated = {token for token, old in aliases.items() if old & seen} - seen
                db.executemany('INSERT OR IGNORE INTO studio_exploration_seen VALUES (?,?)',
                               [(watch['id'], t) for t in migrated])
                seen.update(migrated)
                fresh = tokens - seen
                if not fresh:
                    continue
                db.executemany('INSERT OR IGNORE INTO studio_exploration_seen VALUES (?,?)', [(watch['id'], t) for t in fresh])
                # Unknown paid outcomes must be explicitly recovered, even if new data arrives.
                if row['reason'] == 'waiting_recovery':
                    db.execute('UPDATE studio_explorations SET gain=1 WHERE watch_id=?', (watch['id'],))
                    continue
                if row['state'] != 'active':
                    row.update(generation=row['generation'] + 1, state='active', reason='new_evidence')
                    self._history(db, row, fresh)
                    db.execute("UPDATE studio_watchpoints SET completed_queries='[]' WHERE id=?", (watch['id'],))
                db.execute('UPDATE studio_explorations SET generation=?,state=?,reason=?,no_gain=0,gain=1,updated_at=? WHERE watch_id=?',
                           (row['generation'], row['state'], row['reason'], now(), watch['id']))

    def start(self, watch, cycle):
        with self.monitor._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not self._valid(db, watch):
                return False
            row = db.execute('SELECT * FROM studio_explorations WHERE watch_id=?', (watch['id'],)).fetchone()
            receipt = db.execute('SELECT * FROM studio_exploration_cycles WHERE watch_id=? AND cycle=?', (watch['id'], cycle)).fetchone()
            if receipt:
                return (receipt['state'] == 'collecting' and row is not None
                        and receipt['generation'] == row['generation'] and receipt['version'] == row['version'])
            if not row or (row['state'] != 'active' and row['reason'] not in ('source_failure', 'request_budget')):
                return False
            db.execute("INSERT OR IGNORE INTO studio_exploration_cycles VALUES (?,?,?,?,'collecting',?)",
                       (watch['id'], cycle, row['generation'], row['version'], now()))
            return True

    def runnable(self, watch, cycle=''):
        with self.monitor._db() as db:
            row = db.execute('SELECT * FROM studio_explorations WHERE watch_id=?', (watch['id'],)).fetchone()
            if not row or row['version'] != str(watch.get('lifecycle_version', '0')):
                return False
            if row['state'] == 'active' or row['reason'] in ('source_failure', 'request_budget'):
                return True
            if row['reason'] == 'waiting_recovery' and cycle:
                return db.execute("SELECT 1 FROM studio_exploration_cycles WHERE watch_id=? AND cycle=? AND state='collecting' AND generation=? AND version=?",
                                  (watch['id'], cycle, row['generation'], row['version'])).fetchone() is not None
            return False

    def collected(self, watch_id, cycle):
        with self.monitor._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE studio_exploration_cycles SET state='collected' WHERE watch_id=? AND cycle=? AND state='collecting'",
                       (watch_id, cycle))
            # A successful receipt from the held cycle is explicit recovery, not
            # a new job silently retrying an unknown paid request.
            db.execute("""UPDATE studio_explorations SET state='active',reason='exploring',updated_at=?
                WHERE watch_id=? AND reason IN ('source_failure','waiting_recovery','request_budget') AND EXISTS (
                SELECT 1 FROM studio_watchpoints w WHERE w.id=studio_explorations.watch_id
                AND w.lifecycle_version=studio_explorations.version AND w.status!='retired') AND EXISTS (
                SELECT 1 FROM studio_exploration_cycles c WHERE c.watch_id=studio_explorations.watch_id
                AND c.cycle=? AND c.state='collected' AND c.version=studio_explorations.version
                AND c.generation=studio_explorations.generation)""", (now(), watch_id, cycle))

    def immediate(self, watch, cycle=''):
        """Continue an original query set, or consume a new generation's wake once.

        Exploration eligibility is not permission to ignore reduced cadence.
        Starting the first cycle durably consumes the immediate wake, including
        across restarts; partial work in that same cycle may still continue.
        """
        with self.monitor._db() as db:
            row = db.execute('SELECT * FROM studio_explorations WHERE watch_id=?', (watch['id'],)).fetchone()
            if not row or row['version'] != str(watch.get('lifecycle_version', '0')):
                return False
            cycles = db.execute('SELECT cycle,state FROM studio_exploration_cycles WHERE watch_id=? AND version=? AND generation=?',
                                (watch['id'], row['version'], row['generation'])).fetchall()
            if cycle and any(r['cycle'] == cycle and r['state'] == 'collecting' for r in cycles):
                return True
            # The per-round query budget can split one query set across jobs.
            # Successful siblings are durable; allow only its unfinished tail.
            # runnable() still blocks a new job around an unknown paid outcome.
            pending = set(json.loads(watch.get('completed_queries') or '[]')) & set(watch['queries'])
            if pending and pending != set(watch['queries']) and any(r['state'] == 'collecting' for r in cycles):
                return True
            return row['reason'] == 'new_evidence' and not cycles

    def hold(self, watch, reason):
        if reason not in ('source_failure', 'waiting_recovery', 'request_budget'):
            raise ValueError('invalid hold reason')
        self.stop(watch, reason, state='waiting')

    def hold_cycle(self, project_id, cycle, reason):
        for watch in self.monitor.watchpoints(project_id):
            with self.monitor._db() as db:
                receipt = db.execute("SELECT * FROM studio_exploration_cycles WHERE watch_id=? AND cycle=? AND state!='finished'",
                                     (watch['id'], cycle)).fetchone()
            if receipt and receipt['version'] == str(watch.get('lifecycle_version', '0')):
                self.stop(watch, reason, state='waiting', generation=receipt['generation'])

    def resume_collected(self, project_id, cycle):
        """Resume only the original durable job. The HTTP journal still gates resend."""
        with self.monitor._db() as db, db:
            db.execute("""UPDATE studio_explorations SET state='active',reason='exploring',updated_at=?
                WHERE project_id=? AND reason IN ('waiting_recovery','source_failure','request_budget') AND EXISTS (
                SELECT 1 FROM studio_watchpoints w WHERE w.id=studio_explorations.watch_id
                AND w.lifecycle_version=studio_explorations.version AND w.status!='retired') AND EXISTS (
                SELECT 1 FROM studio_exploration_cycles c WHERE c.watch_id=studio_explorations.watch_id
                AND c.cycle=? AND c.state='collected' AND c.version=studio_explorations.version
                AND c.generation=studio_explorations.generation)""", (now(), project_id, cycle))

    def stop(self, watch, reason, *, state='stopped', evidence=(), generation=None):
        if reason not in REASONS or state not in ('stopped', 'waiting'):
            raise ValueError('invalid exploration outcome')
        with self.monitor._db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not self._valid(db, watch):
                return False
            found = db.execute('SELECT * FROM studio_explorations WHERE watch_id=?', (watch['id'],)).fetchone()
            if not found or (generation is not None and found['generation'] != generation):
                return False
            row = dict(found)
            if row['state'] == state and row['reason'] == reason:
                return False
            row.update(state=state, reason=reason)
            count = 0 if reason in ('source_failure', 'waiting_recovery', 'request_budget') else row['no_gain']
            db.execute('UPDATE studio_explorations SET state=?,reason=?,no_gain=?,updated_at=? WHERE watch_id=?',
                       (state, reason, count, now(), watch['id']))
            self._history(db, row, evidence)
            return True

    def finish(self, project_id, cycle, ledger=None, *, event_filter=None):
        """One successful complete query set + downstream routing = one round.

        Idempotent across process restarts. A partial/failing query set is never
        evidence of exhaustion. Explicit answer/boundary outcomes win over counts.
        """
        self.refresh(project_id, ledger, event_filter=event_filter)
        for watch in self.monitor.watchpoints(project_id):
            with self.monitor._db() as db, db:
                db.execute('BEGIN IMMEDIATE')
                if not self._valid(db, watch):
                    continue
                receipt = db.execute('SELECT * FROM studio_exploration_cycles WHERE watch_id=? AND cycle=?', (watch['id'], cycle)).fetchone()
                row = db.execute('SELECT * FROM studio_explorations WHERE watch_id=?', (watch['id'],)).fetchone()
                if not receipt or receipt['state'] != 'collected' or not row:
                    continue
                if row['state'] == 'waiting':
                    continue
                db.execute("UPDATE studio_exploration_cycles SET state='finished' WHERE watch_id=? AND cycle=?", (watch['id'], cycle))
                if row['version'] != receipt['version'] or row['generation'] != receipt['generation']:
                    continue
                if row['state'] != 'active':
                    continue
                row = dict(row)
                count = 0 if row['gain'] else row['no_gain'] + 1
                reason = 'waiting_event' if watch['cadence'] == 'event' else 'no_gain' if count >= NO_GAIN_ROUNDS else 'exploring'
                state = 'active' if reason == 'exploring' else 'stopped'
                db.execute('UPDATE studio_explorations SET state=?,reason=?,no_gain=?,gain=0,updated_at=? WHERE watch_id=?',
                           (state, reason, count, now(), watch['id']))
                row.update(state=state, reason=reason)
                self._history(db, row)
