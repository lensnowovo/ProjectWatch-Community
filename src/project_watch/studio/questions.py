"""Question ledger on top of watch points: append-only judgements by system, human or Agent.

Core: storage, validation and the advisor seam. Generating or assessing questions with a
model is an optional Insight advisor injected by the composition root; without one the
ledger, human judgements and Agent write-back still work. A system refresh never
overwrites a human or Agent judgement; every system judgement cites permitted inputs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from contextlib import closing
from datetime import date
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from project_watch.studio.exploration import Explorations

_log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_question_assessments (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, watch_id TEXT NOT NULL, actor TEXT NOT NULL,
    status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', event_ids TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL DEFAULT '', idempotency_key TEXT, created_at TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'internal',
    UNIQUE (watch_id, actor, idempotency_key)
);
CREATE INDEX IF NOT EXISTS studio_question_assessments_watch ON studio_question_assessments(watch_id, created_at);
CREATE TABLE IF NOT EXISTS studio_question_consumed_hits (
    watch_id TEXT NOT NULL, item_id TEXT NOT NULL, hit_created_at TEXT NOT NULL,
    event_id TEXT NOT NULL, event_updated_at TEXT NOT NULL,
    PRIMARY KEY (watch_id,item_id,hit_created_at,event_id,event_updated_at)
);
CREATE TABLE IF NOT EXISTS studio_question_material_hits (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, watch_id TEXT NOT NULL,
    question_fingerprint TEXT NOT NULL, binding TEXT NOT NULL, source_id TEXT NOT NULL,
    evidence_version_id TEXT NOT NULL, part_index INTEGER NOT NULL,
    ref TEXT NOT NULL, matched_terms TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(watch_id,question_fingerprint,binding,evidence_version_id,part_index,ref)
);
CREATE INDEX IF NOT EXISTS studio_question_material_watch ON studio_question_material_hits(watch_id,created_at);
CREATE TABLE IF NOT EXISTS studio_question_consumed_materials (
    watch_id TEXT NOT NULL, hit_id TEXT NOT NULL, disposition TEXT NOT NULL DEFAULT 'processed',
    PRIMARY KEY(watch_id,hit_id)
);
CREATE TABLE IF NOT EXISTS studio_question_assessment_attempts (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, watch_id TEXT NOT NULL,
    question_fingerprint TEXT NOT NULL, binding TEXT NOT NULL, fingerprint TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT NOT NULL, event_count INTEGER NOT NULL,
    material_count INTEGER NOT NULL, visibility TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS studio_question_attempt_watch ON studio_question_assessment_attempts(watch_id,created_at);
"""

ACTORS = ('system', 'human', 'agent')
STATUSES = {'progress': '有进展', 'setback': '出现反面信息', 'unchanged': '无新进展', 'insufficient': '证据不足'}
MAX_QUESTIONS = 8
SUGGEST_COUNT = 5
ASSESS_EVENTS = 6  # per-request input shape, never a total per-round work ceiling


def validate_advisor_results(results, allowed):
    """Validate an advisor envelope before writes, without accepting its claims.

    An explicit empty list means all sent questions were intentionally omitted.
    Returned rows still require per-question status, citation and figure checks.
    """
    if not isinstance(results, list):
        raise ValueError('question assessment requires a results list')
    returned = set()
    for row in results:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or row['id'] not in allowed or row['id'] in returned):
            raise ValueError('invalid or duplicate question assessment id')
        if not isinstance(row.get('status'), str):
            raise ValueError('question assessment status must be text')
        if row.get('note') is not None and not isinstance(row['note'], str):
            raise ValueError('question assessment note must be text')
        for key in ('event_ids', 'material_ids'):
            ids = row.get(key)
            if ids is not None and (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)):
                raise ValueError('question assessment references must be lists of ids')
        returned.add(row['id'])
    return returned


class Assessment(BaseModel):
    """The one write contract shared by the web page, the system and external Agents."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    status: Literal['progress', 'setback', 'unchanged', 'insufficient']
    note: str = Field(default='', max_length=500)
    event_ids: tuple[str, ...] = Field(default=(), max_length=12)
    material_ids: tuple[str, ...] = Field(default=(), max_length=12)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)
    visibility: Literal['public', 'internal'] | None = None


class QuestionAdvisor(Protocol):
    """Optional Insight seam. Implementations call a model; Core never imports them."""

    def suggest(self, llm, project: dict, plan: dict, profile: dict | None, existing: list[str]) -> list[dict]:
        """Return up to SUGGEST_COUNT {text, label, queries, criteria} for the company."""

    def assess(self, llm, project: dict, questions: list[dict]) -> list[dict]:
        """questions contain bounded events and scoped material excerpts;
        return [{id, status, note, event_ids, material_ids}] citing only those inputs."""


def _now():
    from project_watch.studio.store import now
    return now()


class QuestionLedger:
    def __init__(self, store, monitor, *, binding_provider=None) -> None:
        self.store, self.monitor = store, monitor
        self.binding_provider = binding_provider or (lambda: '')
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)
            if not db.in_transaction:
                db.execute('BEGIN IMMEDIATE')
            hit_schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='studio_question_material_hits'").fetchone()[0]
            if 'part_index,ref)' not in hit_schema.replace(' ', ''):
                # Preserve every historical ID/ref/consumption receipt. Only the
                # uniqueness key changes so later windows of a part can coexist.
                db.execute('ALTER TABLE studio_question_material_hits RENAME TO studio_question_material_hits_legacy')
                table = SCHEMA[SCHEMA.index('CREATE TABLE IF NOT EXISTS studio_question_material_hits'):]
                db.execute(table.split(';', 1)[0])
                db.execute('INSERT INTO studio_question_material_hits SELECT * FROM studio_question_material_hits_legacy')
                db.execute('DROP TABLE studio_question_material_hits_legacy')
                db.execute('CREATE INDEX studio_question_material_watch ON studio_question_material_hits(watch_id,created_at)')
            columns = {r[1] for r in db.execute('PRAGMA table_info(studio_watchpoints)')}
            if 'origin' not in columns:
                db.execute("ALTER TABLE studio_watchpoints ADD COLUMN origin TEXT NOT NULL DEFAULT 'human'")
            if 'visibility' not in columns:
                db.execute("ALTER TABLE studio_watchpoints ADD COLUMN visibility TEXT NOT NULL DEFAULT 'internal'")
            cols = {r[1] for r in db.execute('PRAGMA table_info(studio_question_assessments)')}
            if 'visibility' not in cols:
                db.execute("ALTER TABLE studio_question_assessments ADD COLUMN visibility TEXT NOT NULL DEFAULT 'internal'")
            if 'material_ids' not in cols:
                db.execute("ALTER TABLE studio_question_assessments ADD COLUMN material_ids TEXT NOT NULL DEFAULT '[]'")
            consumed_cols = {r[1] for r in db.execute('PRAGMA table_info(studio_question_consumed_materials)')}
            if 'disposition' not in consumed_cols:
                db.execute("ALTER TABLE studio_question_consumed_materials ADD COLUMN disposition TEXT NOT NULL DEFAULT 'processed'")
                # Old consumption receipts cannot distinguish omission from an
                # assessment. Only explicit historical citations prove a judgement.
                db.execute('''UPDATE studio_question_consumed_materials SET disposition='assessed'
                    WHERE EXISTS (SELECT 1 FROM studio_question_assessments a,json_each(a.material_ids) cited
                        WHERE a.watch_id=studio_question_consumed_materials.watch_id
                        AND cited.value=studio_question_consumed_materials.hit_id)''')
            # Reuse only explicitly cited legacy inputs, not a whole time window.
            # Current event revisions/new hits stay pending. This runs once per DB.
            if not db.execute("SELECT 1 FROM studio_state WHERE key='question_hit_migration_v1'").fetchone():
                for row in db.execute("SELECT watch_id,event_ids,created_at FROM studio_question_assessments WHERE actor='system'").fetchall():
                    for event_id in json.loads(row[1]):
                        db.execute("""INSERT OR IGNORE INTO studio_question_consumed_hits
                            SELECT h.watch_id,h.item_id,h.created_at,i.event_id,e.updated_at
                            FROM studio_watch_hits h JOIN studio_items i ON i.id=h.item_id
                            JOIN studio_events e ON e.id=i.event_id JOIN studio_watchpoints w ON w.id=h.watch_id
                            WHERE h.watch_id=? AND i.event_id=? AND h.created_at<=? AND e.updated_at<=?
                            AND i.project_id=w.project_id AND e.project_id=w.project_id""",
                            (row[0], event_id, row[2], row[2]))
                db.execute("INSERT INTO studio_state(key,value) VALUES ('question_hit_migration_v1','done')")

    # ------------------------------------------------------------ writes
    def record(self, project_id: str, watch_id: str, actor: str, assessment: Assessment, *,
               fingerprint: str = '', include_internal: bool = True, consumed_events: tuple[dict, ...] = (),
               consumed_materials: tuple[dict, ...] = (), expected_question_fingerprint: str = '') -> dict:
        if actor not in ACTORS:
            raise ValueError('unknown actor')
        if actor == 'system' and not (assessment.event_ids or assessment.material_ids):
            raise ValueError('a system judgement must cite at least one event or material')
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._check_question(db, project_id, watch_id, expected_question_fingerprint)
            if actor == 'system':
                state = db.execute('SELECT status FROM studio_watchpoints WHERE id=?', (watch_id,)).fetchone()
                if state and state[0] == 'retired':
                    raise ValueError('question closed before assessment commit')
            owner = db.execute('SELECT project_id,visibility,scope_kind FROM studio_watchpoints WHERE id=?', (watch_id,)).fetchone()
            if not owner or owner[0] != project_id:
                raise KeyError('question not found')
            if owner[2] == 'relation':
                raise ValueError('standing relation is not a research question')
            if not include_internal and owner[1] != 'public':
                raise KeyError('question not found')
            visibility = assessment.visibility or ('public' if actor == 'system' or not include_internal else 'internal')
            if not include_internal and visibility != 'public':
                raise PermissionError('internal judgement requires internal scope')
            if owner[1] != 'public':
                visibility = 'internal'
            ids = list(dict.fromkeys(assessment.event_ids))
            material_ids = list(dict.fromkeys(assessment.material_ids))
            if material_ids:
                if not include_internal:
                    raise PermissionError('material judgements require internal scope')
                self._check_materials(db, project_id, watch_id, material_ids)
                if assessment.visibility == 'public':
                    raise PermissionError('material judgements cannot be public')
                visibility = 'internal'
            if ids:
                found = {r[0] for r in db.execute(
                    f"SELECT id FROM studio_events WHERE project_id=? AND id IN ({','.join('?' * len(ids))})",
                    (project_id, *ids))}
                if found != set(ids):
                    raise ValueError('cited events must belong to this project')
            if assessment.idempotency_key:
                old = db.execute('SELECT id,visibility FROM studio_question_assessments WHERE watch_id=? AND actor=? AND idempotency_key=?',
                                 (watch_id, actor, assessment.idempotency_key)).fetchone()
                if old:
                    if not include_internal and old[1] != 'public':
                        raise PermissionError('stored judgement requires internal scope')
                    previous = self._get(db, old[0])
                    self._check_materials(db, project_id, watch_id, previous['material_ids'])
                    self._consume(db, project_id, watch_id, consumed_events)
                    self._consume_materials(db, project_id, watch_id, consumed_materials)
                    return previous
            row_id = uuid.uuid4().hex
            db.execute('''INSERT INTO studio_question_assessments
                (id,project_id,watch_id,actor,status,note,event_ids,fingerprint,idempotency_key,created_at,visibility,material_ids)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (row_id, project_id, watch_id, actor, assessment.status, assessment.note.strip(),
                        json.dumps(ids), fingerprint, assessment.idempotency_key, _now(), visibility, json.dumps(material_ids)))
            self._consume(db, project_id, watch_id, consumed_events)
            self._consume_materials(db, project_id, watch_id, consumed_materials)
            db.executemany('''INSERT INTO studio_question_consumed_materials(watch_id,hit_id,disposition) VALUES (?,?,'assessed')
                ON CONFLICT(watch_id,hit_id) DO UPDATE SET disposition='assessed' ''',
                           [(watch_id, hit_id) for hit_id in material_ids])
            return self._get(db, row_id)

    @staticmethod
    def _consume(db, project_id, watch_id, events, *, check_only=False):
        for event in events:
            for item_id, hit_at in event['_question_hits']:
                current = db.execute('''SELECT h.match_fingerprint FROM studio_watch_hits h JOIN studio_items i ON i.id=h.item_id
                    JOIN studio_events e ON e.id=i.event_id JOIN studio_watchpoints w ON w.id=h.watch_id
                    WHERE h.watch_id=? AND h.item_id=? AND h.created_at=? AND i.event_id=? AND e.updated_at=?
                    AND i.project_id=? AND e.project_id=? AND w.project_id=? AND i.relevant=1 AND e.status!='dismissed' ''',
                    (watch_id, item_id, hit_at, event['id'], event['updated_at'], project_id, project_id, project_id)).fetchone()
                if not current:
                    raise ValueError('question input changed before commit')
                if current[0]:
                    row = next((r for r in QuestionLedger.public_event_rows(db, project_id, event['id'])
                                if r["id"] == item_id), None)
                    if row is None or QuestionLedger.public_row_fingerprint(row) != current[0]:
                        raise ValueError('question input changed before commit')
                if not check_only:
                    db.execute('INSERT OR IGNORE INTO studio_question_consumed_hits VALUES (?,?,?,?,?)',
                               (watch_id, item_id, hit_at, event['id'], event['updated_at']))

    @staticmethod
    def _get(db, row_id):
        r = db.execute('SELECT * FROM studio_question_assessments WHERE id=?', (row_id,)).fetchone()
        cols = [c[0] for c in db.execute('SELECT * FROM studio_question_assessments LIMIT 0').description]
        row = dict(zip(cols, r, strict=True))
        row['event_ids'] = json.loads(row['event_ids'])
        row['material_ids'] = json.loads(row['material_ids'])
        row['status_label'] = STATUSES.get(row['status'], row['status'])
        return row

    # ------------------------------------------------------------ reads
    def history(self, watch_id: str, limit: int = 50, *, offset: int = 0, include_internal: bool = True) -> list[dict]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError('history pagination exceeds bounds')
        with closing(self.store._connect()) as db:
            ids = [r[0] for r in db.execute('SELECT id FROM studio_question_assessments WHERE watch_id=? '
                                            'AND (? OR visibility=\'public\') ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?',
                                            (watch_id, include_internal, limit, offset))]
            return [self._get(db, i) for i in ids]

    def view(self, project_id: str, *, include_internal: bool = True) -> list[dict]:
        """Questions with the latest judgement per actor and the cited events, read-only."""
        out = []
        for watch in self.monitor.watchpoints(project_id):
            if not include_internal and watch['visibility'] != 'public':
                continue
            if watch.get('status') == 'retired' or watch.get('scope_kind') == 'relation':
                continue
            latest = {}
            with closing(self.store._connect()) as db:
                db.execute('BEGIN')
                materials = self._material_rows(db, project_id, watch) if include_internal else []
                eligible = {m['id'] for m in materials} if include_internal else set()
                for actor in ACTORS:
                    for row in db.execute('''SELECT id FROM studio_question_assessments WHERE watch_id=? AND actor=?
                        AND (? OR visibility='public') ORDER BY created_at DESC,rowid DESC''',
                        (watch['id'], actor, include_internal)):
                        candidate = self._get(db, row[0])
                        if set(candidate['material_ids']) <= eligible:
                            latest[actor] = candidate
                            break
                count = db.execute('''SELECT count(*) FROM studio_question_assessments WHERE watch_id=?
                                      AND (? OR visibility='public')''', (watch['id'], include_internal)).fetchone()[0]
            cited = {e for row in latest.values() for e in row['event_ids']}
            events = {e['id']: {k: e.get(k) for k in ('id', 'title', 'summary', 'importance', 'event_at', 'category')}
                      for e in self.store.list_events(project_id=project_id, event_ids=sorted(cited), include_dismissed=True,
                                                      limit=50)} if cited else {}
            out.append({**watch, 'origin': watch.get('origin') or 'human', 'latest': latest,
                        'history_count': count, 'events': events,
                        'assessment_state': self._assessment_state(project_id, watch, include_internal=include_internal),
                        **({'materials': materials[:ASSESS_EVENTS], 'material_count': len(materials),
                            'materials_truncated': len(materials) > ASSESS_EVENTS} if include_internal else {})})
        return out

    def _assessment_state(self, project_id, watch, *, include_internal):
        with closing(self.store._connect()) as db:
            row = db.execute('''SELECT status,reason,event_count,material_count,created_at
                FROM studio_question_assessment_attempts WHERE project_id=? AND watch_id=?
                AND question_fingerprint=? AND binding=? AND (? OR visibility='public')
                ORDER BY created_at DESC,rowid DESC LIMIT 1''',
                (project_id, watch['id'], self._question_fingerprint(watch), self.binding_provider(), include_internal)).fetchone()
        result = {'pending_events': len(self.new_events(project_id, watch['id'], limit=None)),
                  'latest_attempt': dict(row) if row else None}
        if include_internal:
            result['pending_materials'] = len(self.new_materials(project_id, watch['id'], limit=None))
        return result

    # ------------------------------------------------------------ internal material routing
    @staticmethod
    def _question_fingerprint(watch):
        values = [watch[k] for k in ('text', 'label', 'queries', 'criteria', 'visibility')]
        # Preserve legacy fingerprints until the first lifecycle change; never
        # replay an old answer after close -> restore (the ABA case).
        if int(watch.get('lifecycle_version') or 0):
            values.append(['lifecycle', str(watch['lifecycle_version'])])
        return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _check_question(self, db, project_id, watch_id, expected):
        if not expected:
            return  # Ordinary human/Agent writes retain their existing contract.
        row = db.execute('SELECT * FROM studio_watchpoints WHERE project_id=? AND id=?',
                         (project_id, watch_id)).fetchone()
        if row is None or row['status'] == 'retired':
            raise ValueError('question input changed before commit')
        current = dict(row)
        current['queries'] = json.loads(current['queries'])
        if self._question_fingerprint(current) != expected:
            raise ValueError('question input changed before commit')

    @staticmethod
    def _material_terms(watch, project):
        names = {str(project.get(k) or '') for k in ('name', 'legal_name', 'short_name')}
        generic = {'公司', '项目', '信息', '进展', '情况', '动态', '最新', '跟踪', '材料', '新闻', '公开'}
        terms = []
        for value in [watch['label'], *watch['queries']]:
            for name in sorted(names - {''}, key=len, reverse=True):
                value = value.replace(name, ' ')
            for term in re.split(r'[\s，,。；;:：/、!?！？]+', value):
                # A date or bare number is context, not a topic: annual-report
                # footers must not route every page to an unrelated question.
                # Keep mixed product identifiers such as CAR-T, JWCAR239 or 3D.
                if (2 <= len(term) <= 60 and any(c.isalpha() for c in term)
                        and term not in generic and term not in terms):
                    terms.append(term)
        return terms[:12]

    @staticmethod
    def _term_match(term, text):
        return re.search(r'(?<![A-Za-z0-9_-])' + re.escape(term) + r'(?![A-Za-z0-9_-])', text) if term.isascii() else re.search(re.escape(term), text)

    @classmethod
    def public_focus_matches(cls, watch, project, text):
        """Public topic relevance, not semantic support. Internal routing is unchanged."""
        if watch.get("scope_kind") in ("relation", "question"):
            targets = cls.relation_terms(watch)
            matched = any(re.search(cls.relation_pattern(watch, t), text.casefold()) or
                          (cls.policy_service_pattern(watch, t) and
                           re.search(cls.policy_service_pattern(watch, t), text.casefold())) for t in targets)
            if watch.get("scope_kind") == "relation":
                return matched
            if targets and not matched:
                return False
        question = " ".join(str(watch.get(k) or "") for k in ("text", "label", "criteria"))
        question, text = question.casefold(), text.casefold()
        # Trigger on the question, never its broad search query. Subject/industry
        # words alone cannot match an outcome-specific question.
        families = (
            ("医保", "医保 支付标准 报销 reimbursement"),
            ("IPO 科创板 上市审核", "IPO 科创板 问询 上会 注册发行 上市审核 上市申请 提交注册"),
            ("注册 拿证 许可 获批", "注册 拿证 许可 获批 审批 批准 临床试验 临床注册 创新医疗器械绿色通道 创新医疗器械特别审查"),
            ("销售 收入 订单", "销售 收入 订单 交付 营收 revenue"),
            ("亏损 费用率", "亏损 费用率 盈利 利润 毛利 loss"),
            ("临床结果 临床效果 疗效", "临床结果 临床效果 疗效 试验结果 研究结果 安全性 有效性 clinical"),
        )
        focus = []
        for triggers, alternatives in families:
            if triggers == "注册 拿证 许可 获批" and any(
                    cls._term_match(t, question) for t in ("ipo", "科创板", "上市审核")):
                continue  # Securities registration is not product approval.
            if triggers == "销售 收入 订单" and "费用率" in question:
                continue  # Selling expenses are not a product sales milestone.
            if any(cls._term_match(t.casefold(), question) for t in triggers.split()):
                focus.extend(alternatives.casefold().split())
        if focus:
            return any(cls._term_match(t, text) for t in focus)
        terms = [t.casefold() for t in cls._material_terms(watch, project) if cls._term_match(t.casefold(), question)]
        return any(cls._term_match(t, text) for t in terms)

    @staticmethod
    def relation_terms(watch):
        aliases = watch.get("relation_aliases") or []
        if isinstance(aliases, str):
            aliases = json.loads(aliases)
        return list(dict.fromkeys(str(t).strip() for t in
                    [watch.get("relation_target") or "", *aliases] if len(str(t).strip()) >= 2))

    @staticmethod
    def relation_pattern(watch, term):
        """Bounded lexical variants, never a broad industry/semantic match.

        Pricing-project wording can include the descriptor 医疗服务. Preserve
        the complete subject prefix; do not normalize company/product names or
        silently persist inferred aliases on the watch.
        """
        term = term.casefold()
        policy = watch.get("relation_type") == "policy" or (
            watch.get("scope_kind") == "question" and not watch.get("relation_type")
            and watch.get("axis") == "rules")  # Older children inherit target/aliases, not relation_type.
        if policy:
            parts = re.fullmatch(r"(.{2,}?)相关(?:医疗服务)?价格项目", term)
            if parts:
                return re.escape(parts[1]) + r"相关(?:医疗服务)?价格项目"
        return (r"(?<![A-Za-z0-9_-])" + re.escape(term) + r"(?![A-Za-z0-9_-])"
                if term.isascii() else re.escape(term))

    @staticmethod
    def policy_service_pattern(watch, term):
        """An explicit subject/service/policy construction, not sector co-occurrence.

        The subject comes only from a saved pricing-policy target or alias.
        No arbitrary intervening words, other subjects, or sentence boundaries
        may connect it to the service relation. Plans and negations are relevant
        leads too; matching does not assert that coverage has taken effect.
        """
        policy = watch.get("relation_type") == "policy" or (
            watch.get("scope_kind") == "question" and not watch.get("relation_type")
            and watch.get("axis") == "rules")
        parts = re.fullmatch(r"(.{2,}?)相关(?:医疗服务)?价格项目", term.casefold()) if policy else None
        if not parts:
            return None
        subject = parts[1]
        anchor = (r"(?<![A-Za-z0-9_-])" + re.escape(subject) + r"(?![A-Za-z0-9_-])"
                  if subject.isascii() else re.escape(subject))
        service = r"(?:相关)?(?:医疗|诊疗)?服务(?:项目)?"
        inclusion = r"(?:按规定)?(?:拟|将|计划|暂不|尚未|未|不|已)?(?:纳入|列入)"
        destination = r"(?:医疗服务价格项目|(?:基本)?医保(?:支付|报销)(?:范围)?)"
        pricing = r"(?:等|相关)?医疗服务价格项目|(?:的)?(?:收费|价格)(?:项目|标准|路径|政策)"
        payment = r"(?:的)?(?:基本)?医保(?:支付|报销)(?:范围|类别)?"
        classified = inclusion + r"(?:甲|乙|丙)类(?:基本)?医保"
        return anchor + r"[ \t]{0,4}(?:" + pricing + "|" + payment + "|" + classified + "|" + service + inclusion + destination + ")"

    @classmethod
    def public_row_match(cls, watch, project, row):
        """Literal association with a bounded, attributable excerpt; not a verdict."""
        terms = cls.relation_terms(watch)
        headers = " ".join(str(row.get(k) or "") for k in ("title", "summary", "event_title", "event_summary"))
        candidates = [("summary", headers)]
        scope_basis = {"article_body": "full_body", "partial_body": "partial_body", "search_summary": "search_summary"}
        if watch.get("scope_kind") in ("relation", "question") and row.get("body_status") in scope_basis:
            # A saved search excerpt can route a lead, but is never full-text verification.
            candidates.append((scope_basis[row["body_status"]], str(row.get("body") or "")))
        for basis, text in candidates:
            if not terms:
                if cls.public_focus_matches(watch, project, text):
                    return {"basis": basis, "matched_term": "", "excerpt": text[:500]}
                continue
            # Require the research outcome close to the subject, rather than in
            # an unrelated section elsewhere in a long article/report.
            lower = text.casefold()
            for term in terms:
                pattern = cls.relation_pattern(watch, term)
                for match in re.finditer(pattern, lower):
                    excerpt = text[max(0, match.start() - 180):match.end() + 320]
                    if cls.public_focus_matches(watch, project, excerpt):
                        return {"basis": basis, "matched_term": term, "excerpt": excerpt}
            if basis == "summary":
                continue  # A generated headline/summary cannot establish a new structural link.
            for term in terms:
                pattern = cls.policy_service_pattern(watch, term)
                if not pattern:
                    continue
                for sentence in re.finditer(r"[^。！？!?；;\n]+", text):
                    for match in re.finditer(pattern, sentence[0].casefold()):
                        excerpt = sentence[0][max(0, match.start() - 180):match.end() + 320]
                        if cls.public_focus_matches(watch, project, excerpt):
                            return {"basis": basis, "matched_term": term, "excerpt": excerpt,
                                    "match_rule": "policy_service_link", "matched_text": match[0]}
        return None

    @staticmethod
    def public_row_fingerprint(row):
        return hashlib.sha256(json.dumps(
            [row[k] or "" for k in ("title", "summary", "event_title", "event_summary")]
            + [row["body"] if row["body_status"] in ("article_body", "partial_body", "search_summary") else ""],
            ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def public_event_rows(db, project_id, event_id=None):
        rows = db.execute("""SELECT i.id,i.event_id,i.title,i.summary,i.snippet,i.body,i.body_status,i.url,i.source,
            e.title AS event_title,e.summary AS event_summary,e.event_at,ev.record AS evidence_record,
            EXISTS(SELECT 1 FROM studio_evidence_lifecycle l WHERE l.project_id=i.project_id
                AND l.evidence_version_id=ev.id AND l.status='withdrawn') AS withdrawn
            FROM studio_items i JOIN studio_events e ON e.id=i.event_id
            LEFT JOIN studio_item_evidence ie ON ie.item_id=i.id AND ie.project_id=i.project_id
            LEFT JOIN studio_evidence_versions ev ON ev.id=ie.evidence_version_id AND ev.project_id=i.project_id
                AND ev.visibility='public'
            WHERE i.project_id=? AND e.project_id=? AND i.relevant=1 AND e.status!='dismissed'
            AND (? IS NULL OR e.id=?) ORDER BY COALESCE(e.event_at,e.created_at) DESC,i.id""",
            (project_id, project_id, event_id, event_id)).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            if row.pop("withdrawn"):
                continue
            record = json.loads(row.pop("evidence_record") or "{}")
            if row["body_status"] in ("article_body", "partial_body", "search_summary") and record.get("parts"):
                # studio_items.body is only a 12k preview. Match all saved parts
                # locally, then expose only the relevant bounded excerpt.
                row["body"] = "\n".join(p["text"] for p in record["parts"])
            result.append(row)
        return result

    def public_event_match(self, watch, project, event, *, rows=None):
        if "body_status" in event:
            return self.public_row_match(watch, project, event)
        if watch.get("scope_kind") in ("relation", "question"):
            if rows is None:
                event_id = event.get("event_id") or event.get("id")
                if not event_id:
                    return None
                with closing(self.store._connect()) as db:
                    rows = self.public_event_rows(db, watch["project_id"], event_id)
            for row in rows:
                match = self.public_row_match(watch, project, dict(row))
                if match:
                    return {**match, "source": row["source"], "url": row["url"], "item_id": row["id"]}
            return None
        return self.public_row_match(watch, project, event)

    @staticmethod
    def _material_windows(terms, text):
        """Every literal occurrence gets context; nearby occurrences share a window."""
        spans = []
        for term in terms:
            pattern = r'(?<![A-Za-z0-9_-])' + re.escape(term) + r'(?![A-Za-z0-9_-])' if term.isascii() else re.escape(term)
            for match in re.finditer(pattern, text):
                spans.append((max(0, match.start() - 300), min(len(text), match.end() + 900)))
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] and end - merged[-1][0] <= 12000:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return merged

    def _material_record(self, db, project_id, source_id, version_id, binding):
        if not binding or binding != self.binding_provider():
            return None
        row = db.execute('''SELECT e.record,s.content_sha256,j.id AS intake_id
            FROM studio_evidence_versions e JOIN studio_evidence_heads h
            ON h.project_id=e.project_id AND h.evidence_version_id=e.id AND h.source_key=e.source_key
            JOIN sources s ON s.id=? AND s.project_id=e.project_id AND s.kind='document'
            JOIN intake_jobs j ON j.source_id=s.id AND j.project_id=s.project_id AND j.status IN ('completed','needs_ocr')
            JOIN studio_projects project ON project.project_id=e.project_id AND project.consent_at IS NOT NULL
            LEFT JOIN studio_source_permissions p ON p.source_id=s.id
            WHERE e.project_id=? AND e.id=? AND e.visibility='internal'
            AND (p.source_id IS NULL OR (p.project_id=e.project_id AND p.model_binding=?))
            AND NOT EXISTS (SELECT 1 FROM studio_evidence_lifecycle l
                WHERE l.project_id=e.project_id AND l.evidence_version_id=e.id AND l.status='withdrawn')''',
            (source_id, project_id, version_id, binding)).fetchone()
        if row is None:
            return None
        record = json.loads(row['record'])
        if (record.get('internal_source_id') != source_id or record['source_key'] != 'internal:' + source_id
                or record['source_type'] != 'document' or record['raw_content_hash'] != row['content_sha256']
                or record['extraction_version'] != f"core-intake:{row['intake_id']}:whitespace-v1"):
            return None
        return record

    def route_materials(self, project_id, refs, source_ids, *, binding, watch_ids=None):
        """Local literal relevance only. Never creates an event or judgement."""
        added = 0
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            project = self.store.get_project(project_id) or {}
            watches = [w for w in self.monitor.watchpoints(project_id) if w.get('status') != 'retired']
            if watch_ids is not None:
                watches = [w for w in watches if w['id'] in watch_ids]
            records = {}
            for candidate in refs.values():
                sid, version, index = candidate.get('source_id'), candidate.get('evidence_version_id'), candidate.get('part_index')
                if sid not in source_ids or not version or type(index) is not int:
                    continue
                if (sid, version) not in records:
                    records[sid, version] = self._material_record(db, project_id, sid, version, binding)
                record = records[sid, version]
                if record is None or not 0 <= index < len(record['parts']):
                    continue
                text = record['parts'][index]['text']
                for watch in watches:
                    terms = [t for t in self._material_terms(watch, project) if self._term_match(t, text)]
                    if not terms:
                        continue
                    for start, end in self._material_windows(terms, text):
                        ref = {'evidence_version_id': version, 'part_index': index, 'start': start, 'end': end, 'quote': text[start:end]}
                        added += db.execute('INSERT OR IGNORE INTO studio_question_material_hits VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                            (uuid.uuid4().hex, project_id, watch['id'], self._question_fingerprint(watch), binding,
                             sid, version, index, json.dumps(ref, ensure_ascii=False), json.dumps(terms, ensure_ascii=False), _now())).rowcount
            if binding != self.binding_provider():
                raise ValueError('model binding changed during material routing')
        return added

    def route_existing_materials(self, project_id, watch_ids):
        """A new question can attach already parsed inputs, without reparsing/AI."""
        binding = self.binding_provider()
        refs, source_ids = {}, set()
        with closing(self.store._connect()) as db:
            db.execute('BEGIN')
            rows = db.execute('''SELECT e.id,json_extract(e.record,'$.internal_source_id') AS source_id
                FROM studio_evidence_heads h JOIN studio_evidence_versions e ON e.id=h.evidence_version_id
                WHERE h.project_id=? AND e.project_id=? AND e.visibility='internal' ''', (project_id, project_id)).fetchall()
            for row in rows:
                record = self._material_record(db, project_id, row['source_id'], row['id'], binding)
                if record:
                    source_ids.add(row['source_id'])
                    for index in range(len(record['parts'])):
                        refs[len(refs)] = {'source_id': row['source_id'], 'evidence_version_id': row['id'], 'part_index': index}
        return self.route_materials(project_id, refs, source_ids, binding=binding, watch_ids=watch_ids)

    def route_existing_events(self, project_id, watch_ids, *, focused=False):
        """Attach existing relevant public events to newly-created questions.

        Literal relevance only, like material routing; never a judgement or an
        independent confirmation. Company names alone cannot create a hit.
        """
        project = self.store.get_project(project_id) or {}
        watches = [w for w in self.monitor.watchpoints(project_id) if w['id'] in watch_ids and w.get('status') != 'retired']
        added = 0
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = self.public_event_rows(db, project_id)
            for watch in watches:
                self._check_question(db, project_id, watch['id'], self._question_fingerprint(watch))
                terms = self._material_terms(watch, project)
                for row in rows:
                    text = ' '.join(str(row[k] or '') for k in ('title', 'summary', 'event_title', 'event_summary'))
                    matches = (self.public_row_match(watch, project, dict(row))
                               if watch.get("scope_kind") in ("relation", "question") else
                               self.public_focus_matches(watch, project, text) if focused else
                               any(self._term_match(term, text) for term in terms))
                    if matches:
                        fingerprint = ""
                        if watch.get("scope_kind") in ("relation", "question"):
                            fingerprint = self.public_row_fingerprint(row)
                        old = db.execute("SELECT match_fingerprint FROM studio_watch_hits WHERE watch_id=? AND item_id=?",
                                         (watch["id"], row["id"])).fetchone()
                        count = db.execute('INSERT OR IGNORE INTO studio_watch_hits(watch_id,item_id,created_at) VALUES (?,?,?)',
                                           (watch['id'], row['id'], _now())).rowcount
                        if fingerprint:
                            if old and old[0] and old[0] != fingerprint:
                                db.execute("UPDATE studio_watch_hits SET created_at=? WHERE watch_id=? AND item_id=?",
                                           (_now(), watch["id"], row["id"]))
                                count = 1
                            db.execute("UPDATE studio_watch_hits SET match_fingerprint=? WHERE watch_id=? AND item_id=?",
                                       (fingerprint, watch["id"], row["id"]))
                        added += count
                        if count:
                            db.execute('UPDATE studio_watchpoints SET last_hit_at=? WHERE id=?', (_now(), watch['id']))
        return added

    def _material_rows(self, db, project_id, watch, *, pending_only=False):
        out, records = [], {}
        current = db.execute('SELECT * FROM studio_watchpoints WHERE project_id=? AND id=?', (project_id, watch['id'])).fetchone()
        if current is None:
            return []
        current = dict(current)
        current['queries'] = json.loads(current['queries'])
        if self._question_fingerprint(current) != self._question_fingerprint(watch):
            return []
        rows = db.execute('''SELECT h.*,c.hit_id AS consumed,c.disposition FROM studio_question_material_hits h
            LEFT JOIN studio_question_consumed_materials c ON c.watch_id=h.watch_id AND c.hit_id=h.id
            WHERE h.project_id=? AND h.watch_id=? AND h.question_fingerprint=? AND h.binding=?
            AND (?=0 OR c.hit_id IS NULL) ORDER BY h.created_at DESC,h.rowid DESC''',
            (project_id, watch['id'], self._question_fingerprint(watch), self.binding_provider(), pending_only)).fetchall()
        for row in rows:
            key = row['source_id'], row['evidence_version_id']
            if key not in records:
                records[key] = self._material_record(db, project_id, *key, row['binding'])
            record = records[key]
            ref = json.loads(row['ref'])
            if record is None or not 0 <= ref['part_index'] < len(record['parts']):
                continue
            part = record['parts'][ref['part_index']]
            if part['text'][ref['start']:ref['end']] != ref['quote']:
                continue
            out.append({'id': row['id'], 'source_id': row['source_id'], 'ref': ref, 'locator': part['locator'],
                        'matched_terms': json.loads(row['matched_terms']), 'basis': 'literal_relevance',
                        'status': row['disposition'] if row['consumed'] else 'pending', 'visibility': 'internal'})
        return out

    def _check_materials(self, db, project_id, watch_id, ids):
        if not ids:
            return
        row = db.execute('SELECT * FROM studio_watchpoints WHERE project_id=? AND id=?', (project_id, watch_id)).fetchone()
        watch = dict(row) if row else None
        if watch:
            watch['queries'] = json.loads(watch['queries'])
        if watch is None or not set(ids) <= {r['id'] for r in self._material_rows(db, project_id, watch)}:
            raise ValueError('material references are not current permitted inputs of this question')

    def new_materials(self, project_id, watch_id, *, limit=ASSESS_EVENTS):
        with closing(self.store._connect()) as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM studio_watchpoints WHERE project_id=? AND id=?', (project_id, watch_id)).fetchone()
            if row is None:
                return []
            watch = dict(row)
            watch['queries'] = json.loads(watch['queries'])
            rows = self._material_rows(db, project_id, watch, pending_only=True)
            return rows if limit is None else rows[:limit]

    def _consume_materials(self, db, project_id, watch_id, materials, *, disposition='assessed'):
        ids = [m['id'] for m in materials]
        self._check_materials(db, project_id, watch_id, ids)
        sql = 'INSERT OR IGNORE INTO studio_question_consumed_materials(watch_id,hit_id,disposition) VALUES (?,?,?)'
        if disposition == 'assessed':
            sql = '''INSERT INTO studio_question_consumed_materials(watch_id,hit_id,disposition) VALUES (?,?,?)
                ON CONFLICT(watch_id,hit_id) DO UPDATE SET disposition='assessed' '''
        db.executemany(sql, [(watch_id, hit_id, disposition) for hit_id in ids])

    def new_events(self, project_id: str, watch_id: str, *, limit=ASSESS_EVENTS) -> list[dict]:
        """Unconsumed input versions, bounded after filtering previously consumed hits."""
        with closing(self.store._connect()) as db:
            rows = db.execute('''SELECT DISTINCT e.id FROM studio_watch_hits h
                JOIN studio_items i ON i.id=h.item_id JOIN studio_events e ON e.id=i.event_id
                JOIN studio_watchpoints w ON w.id=h.watch_id
                WHERE h.watch_id=? AND w.project_id=? AND i.project_id=? AND e.project_id=?
                AND i.relevant=1 AND e.status!='dismissed' AND NOT EXISTS (
                    SELECT 1 FROM studio_question_consumed_hits c WHERE c.watch_id=h.watch_id
                    AND c.item_id=h.item_id AND c.hit_created_at=h.created_at AND c.event_id=e.id AND c.event_updated_at=e.updated_at)
                ORDER BY coalesce(e.event_at,e.created_at) DESC,e.id LIMIT ?''',
                (watch_id, project_id, project_id, project_id, -1 if limit is None else limit)).fetchall()
            events = self.store.list_events(project_id=project_id, event_ids=[r[0] for r in rows], limit=len(rows))
            hits = {}
            for row in db.execute('''SELECT i.event_id,h.item_id,h.created_at
                    FROM studio_watch_hits h JOIN studio_items i ON i.id=h.item_id
                    WHERE h.watch_id=? AND i.project_id=? AND i.relevant=1''', (watch_id, project_id)):
                hits.setdefault(row[0], []).append(tuple(row)[1:])
            for event in events:
                event['_question_hits'] = hits.get(event['id'], [])
            return events

    def _numbers_supported(self, project_id: str, note: str, event_ids, material_quotes=()) -> bool:
        return not self._unsupported_numbers(project_id, note, event_ids, material_quotes)

    def _unsupported_numbers(self, project_id: str, note: str, event_ids, material_quotes=()) -> list[str]:
        """Bounded numeric-token/unit existence check, not semantic claim verification."""
        token = re.compile(r'(?<![\w.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])', re.ASCII)
        unit = re.compile(r'\s*(%|％|个百分点|亿元|万元|亿美元|万美元|亿|万|元|台|颗|例|人|家|套|倍|年|月|日)')

        def figures_in(text):
            # Compare a complete calendar date as one value. Never borrow its
            # year/month/day from separate dates or source blocks.
            calendar = re.compile(r'(?<![\w.])((?:19|20)\d{2})(?:年(\d{1,2})月(\d{1,2})日|([-\/])(\d{1,2})\4(\d{1,2}))(?=T\d{2}:|[^\w.]|$)', re.ASCII)
            spans = []
            for match in calendar.finditer(text):
                year, month, day = int(match[1]), int(match[2] or match[5]), int(match[3] or match[6])
                negated = bool(re.search(r'(?:未实现|未达|没有|并未|不是|未|否认)[^，。；;!?！？]{0,8}$',
                                         text[max(0, match.start()-12):match.start()]))
                try:
                    value = date(year, month, day).isoformat()
                except ValueError:
                    value = 'invalid:' + match[0]
                yield value, '日期', negated
                if not value.startswith('invalid:'):
                    yield f'{year:04d}-{month:02d}', '年月', negated
                    yield str(year), '年', negated
                    yield str(month), '月', negated
                    yield str(day), '日', negated
                spans.append(match.span())
            period = re.compile(r'(?<![\w.])((?:19|20)\d{2})(?:年(\d{1,2})月|[-/](\d{1,2}))(?![\w.])', re.ASCII)
            for match in period.finditer(text):
                if any(start <= match.start() < end for start, end in spans):
                    continue
                year, month = int(match[1]), int(match[2] or match[3])
                negated = bool(re.search(r'(?:未实现|未达|没有|并未|不是|未|否认)[^，。；;!?！？]{0,8}$',
                                         text[max(0, match.start()-12):match.start()]))
                value = f'{year:04d}-{month:02d}' if 1 <= month <= 12 else 'invalid:' + match[0]
                yield value, '年月', negated
                if not value.startswith('invalid:'):
                    yield str(year), '年', negated
                    yield str(month), '月', negated
                spans.append(match.span())
            for match in token.finditer(text):
                if any(start <= match.start() < end for start, end in spans):
                    continue
                after = text[match.end():]
                # Do not borrow digits inside product names or identifiers.
                if after[:1].isascii() and after[:1].isalpha():
                    continue
                negated = bool(re.search(r'(?:未实现|未达|没有|并未|不是|未|否认)[^，。；;!?！？]{0,8}$',
                                         text[max(0, match.start()-12):match.start()]))
                suffix = unit.match(after)
                value = match.group().replace(',', '')
                label = suffix[1].replace('％', '%') if suffix else ''
                # Calendar wording only: “2025 全年” / “2025 年度” / “2025年”.
                # Do not convert quantities, infer dates from metadata, or treat
                # a naked four-digit count as a year.
                if re.fullmatch(r'(?:19|20)\d{2}', value) and re.match(r'\s*(?:全年|年度)', after):
                    label = '年'
                yield value, label, negated

        figures = set(figures_in(note))
        if not figures:
            return []
        ids = list(event_ids)
        marks = ','.join('?' * len(ids))
        with closing(self.store._connect()) as db:
            blocks = [v or '' for row in db.execute(
                f'SELECT title, summary FROM studio_events WHERE project_id=? AND id IN ({marks})', (project_id, *ids))
                for v in row]
            blocks += [v or '' for row in db.execute(
                f'SELECT title,snippet,summary,body FROM studio_items WHERE project_id=? AND event_id IN ({marks})',
                (project_id, *ids)) for v in row]
        blocks += list(material_quotes)
        available = {figure for block in blocks for figure in figures_in(block)
                     if not figure[0].startswith('invalid:')}
        return sorted(f"{'否定:' if negated else ''}{f}{u}" for f, u, negated in figures
                      if not ((f, u, negated) in available if u else
                              any(n == f and negative == negated for n, _, negative in available)))

    # ------------------------------------------------------------ advisor rounds
    def suggest(self, advisor, llm, project_id: str, project: dict, plan: dict, profile: dict | None) -> int:
        """Once per project: add AI questions when none exist yet. Returns how many were added."""
        current = self.monitor.watchpoints(project_id)
        # A reviewed monitoring plan (any item with a plan origin) replaces the one-off suggestion round.
        if advisor is None or not getattr(llm, 'ready', False) or any((w.get('origin') == 'ai') or w.get('plan_origin')
                                                                       for w in current):
            return 0
        room = MAX_QUESTIONS - len(current)
        if room <= 0:
            return 0
        rows = advisor.suggest(llm, project, plan, profile, [w['text'] for w in current]) or []
        added = 0
        for row in rows[:min(room, SUGGEST_COUNT)]:
            text = ' '.join(str(row.get('text') or '').split())[:200]
            queries = [str(q).strip()[:60] for q in row.get('queries') or [] if str(q).strip()][:3]
            if len(text) < 4 or not queries:
                continue
            from project_watch.studio.monitoring_plan import MonitoringPlan

            # Suggestions are revertible trials; confidence does not override cadence.
            watch = MonitoringPlan(self.monitor).add(project_id, {
                'text': text, 'label': str(row.get('label') or text[:8])[:12], 'queries': queries, 'alert': False,
                'criteria': str(row.get('criteria') or f'与“{text}”直接相关的消息')[:200],
                'reason': '首轮由 AI 根据公司资料建议，待你确认'}, actor='system', origin='system',
                reason='首轮 AI 建议', visibility='internal' if profile else 'public')
            self.route_existing_materials(project_id, {watch['id']})
            self.route_existing_events(project_id, {watch['id']})
            added += 1
        return added

    def assess(self, advisor, llm, project_id: str, project: dict) -> int:
        """Give all current inputs a chance, once each in bounded requests.

        Snapshot the finite backlog: rejected inputs remain pending but are not
        resent in this invocation; new arrivals wait for the next round. Any
        failure stops dispatch immediately, with completed batches retained.
        """
        if advisor is None or not getattr(llm, 'ready', False):
            return 0
        snapshots = []
        exploration = Explorations(self.monitor)
        states = exploration.view(project_id)
        for watch in self.monitor.watchpoints(project_id):
            if watch.get('status') == 'retired' or watch.get('scope_kind') == 'relation':
                continue
            if exploration.managed(watch) and watch['id'] in states and not exploration.runnable(watch):
                continue
            events = self.new_events(project_id, watch['id'], limit=None)
            materials = self.new_materials(project_id, watch['id'], limit=None)
            if events or materials:
                # Oldest first: the latest judgement after draining the backlog
                # must concern the newest input, not an older overflow page.
                snapshots.append((watch, self._question_fingerprint(watch), self.binding_provider(),
                                  list(reversed(events)), list(reversed(materials))))
        written = 0
        extent = max((max(len(e), len(m)) for _w, _v, _b, e, m in snapshots), default=0)
        for offset in range(0, extent, ASSESS_EVENTS):
            batch = [(w, v, b, e[offset:offset+ASSESS_EVENTS], m[offset:offset+ASSESS_EVENTS])
                     for w, v, b, e, m in snapshots if e[offset:offset+ASSESS_EVENTS] or m[offset:offset+ASSESS_EVENTS]]
            try:
                with closing(self.store._connect()) as db:
                    db.execute('BEGIN')
                    for watch, version, binding, events, materials in batch:
                        if binding != self.binding_provider():
                            raise ValueError('model binding changed before assessment')
                        self._check_question(db, project_id, watch['id'], version)
                        self._consume(db, project_id, watch['id'], events, check_only=True)
                        self._check_materials(db, project_id, watch['id'], [m['id'] for m in materials])
                written += self._assess_batch(advisor, llm, project_id, project, batch)
            except Exception as exc:
                code = getattr(exc, 'code', '')
                known = {'request_outcome_unknown', 'request_budget', 'provider_backoff', 'project_paused', 'model_content_rejected',
                         'model_output_invalid_shape', 'model_output_invalid_json', 'model_access_denied',
                         'model_http_failure', 'model_timeout', 'model_error'}
                reason = code if code in known else 'assessment_failed'
                try:
                    with closing(self.store._connect()) as db, db:
                        for snapshot in batch:
                            self._attempt(db, project_id, snapshot, 'failed', reason)
                except Exception:
                    # Never bypass a lost lease to save diagnostics, and preserve
                    # the original failure. No further batch may be sent.
                    _log.info('question failure receipt unavailable')
                raise
        return written

    @staticmethod
    def _input_fingerprint(snapshot):
        _watch, version, binding, events, materials = snapshot
        return hashlib.sha256(json.dumps([version, binding, events, materials], sort_keys=True).encode()).hexdigest()

    def _attempt(self, db, project_id, snapshot, status, reason):
        watch, version, binding, events, materials = snapshot
        if not db.execute('SELECT 1 FROM studio_watchpoints WHERE project_id=? AND id=?', (project_id, watch['id'])).fetchone():
            return
        visibility = 'internal' if materials or watch['visibility'] != 'public' else 'public'
        db.execute('INSERT INTO studio_question_assessment_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, project_id, watch['id'], version, binding, self._input_fingerprint(snapshot),
                    status, reason, len(events), len(materials), visibility, _now()))

    def _assess_batch(self, advisor, llm, project_id, project, batch):
        by_id = {snapshot[0]['id']: snapshot for snapshot in batch}
        inputs = {wid: tuple(s[3]) for wid, s in by_id.items()}
        material_inputs = {wid: tuple(s[4]) for wid, s in by_id.items()}
        question_versions = {wid: s[1] for wid, s in by_id.items()}
        pending = [{'id': w['id'], 'text': w['text'], 'criteria': w['criteria'],
                    'events': [{'id': e['id'], 'title': e['title'], 'summary': e.get('summary') or '',
                                'date': (e.get('event_at') or '')[:10]} for e in events], 'materials': materials}
                   for w, _v, _b, events, materials in batch]
        allowed = {q['id']: {e['id'] for e in q['events']} for q in pending}
        allowed_materials = {q['id']: {m['id']: m for m in q['materials']} for q in pending}
        written = 0
        results = advisor.assess(llm, project, pending)
        # Validate the whole envelope before publishing any row. A malformed
        # reply must not look like an intentional omission of every question.
        returned = validate_advisor_results(results, allowed)
        for row in results:
            watch_id = row['id']
            cited = tuple(e for e in row.get('event_ids') or [] if e in allowed[watch_id])
            cited_materials = tuple(m for m in row.get('material_ids') or [] if m in allowed_materials[watch_id])
            status = row.get('status')
            if not (cited or cited_materials) or status not in STATUSES:
                reason = 'invalid_status' if status not in STATUSES else 'missing_reference'
                with closing(self.store._connect()) as db, db:
                    self._attempt(db, project_id, by_id[watch_id], 'rejected', reason)
                continue  # never store an uncited or unknown system judgement
            note = str(row.get('note') or '')[:500]
            quotes = [allowed_materials[watch_id][m]['ref']['quote'] for m in cited_materials]
            if not self._numbers_supported(project_id, note, cited, quotes):
                _log.info("question judgement dropped: a figure is not in the cited inputs")
                with closing(self.store._connect()) as db, db:
                    self._attempt(db, project_id, by_id[watch_id], 'rejected', 'unsupported_number')
                continue
            if by_id[watch_id][2] != self.binding_provider():
                raise ValueError('model binding changed before assessment commit')
            fingerprint = self._input_fingerprint(by_id[watch_id])
            self.record(project_id, watch_id, 'system',
                        Assessment(status=status, note=note, event_ids=cited, material_ids=cited_materials,
                                   visibility='internal' if material_inputs[watch_id] else None,
                                   idempotency_key=f'system-{fingerprint}'),
                        fingerprint=fingerprint, consumed_events=inputs[watch_id], consumed_materials=material_inputs[watch_id],
                        expected_question_fingerprint=question_versions[watch_id])
            written += 1
            with closing(self.store._connect()) as db, db:
                self._attempt(db, project_id, by_id[watch_id], 'assessed', 'judgement_recorded')
        # An intentionally omitted question consumes its sent inputs. A returned
        # but rejected judgement does not: its evidence still needs assessment.
        # Unknown/network/model failures raise before this point and consume nothing.
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            for watch_id, events in inputs.items():
                if watch_id not in returned:
                    if by_id[watch_id][2] != self.binding_provider():
                        raise ValueError('model binding changed before assessment commit')
                    self._check_question(db, project_id, watch_id, question_versions[watch_id])
                    self._consume(db, project_id, watch_id, events)
                    self._consume_materials(db, project_id, watch_id, material_inputs[watch_id], disposition='omitted')
                    self._attempt(db, project_id, by_id[watch_id], 'omitted', 'advisor_omitted')
        return written
