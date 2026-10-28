"""Monitoring depth: official-site changes, feedback rules, watch points and event storylines.

Kept in its own module (own tables, created on first use) so the core store stays small.
Every rule and watch point is visible to the user and can be removed; nothing is learned
silently. Storylines use a fixed stage vocabulary so progress is comparable across companies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from contextlib import closing, nullcontext
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from project_watch.studio import sitewatch as sw
from project_watch.studio.exploration import SCHEMA as EXPLORATION_SCHEMA

_log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_site_pages (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, url TEXT NOT NULL, label TEXT NOT NULL,
    kind TEXT NOT NULL, added_by TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
    last_checked TEXT, last_changed TEXT, last_hash TEXT, last_blocks TEXT, created_at TEXT NOT NULL,
    UNIQUE (project_id, url)
);
CREATE TABLE IF NOT EXISTS studio_site_changes (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, page_id TEXT NOT NULL, url TEXT NOT NULL, label TEXT NOT NULL,
    detected_at TEXT NOT NULL, added TEXT NOT NULL, removed TEXT NOT NULL, item_id TEXT
);
CREATE INDEX IF NOT EXISTS studio_site_changes_project ON studio_site_changes(project_id, detected_at);
CREATE TABLE IF NOT EXISTS studio_rules (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', hits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    UNIQUE (project_id, kind, value)
);
CREATE TABLE IF NOT EXISTS studio_feedback (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, before TEXT NOT NULL DEFAULT '', after TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_watchpoints (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, text TEXT NOT NULL, label TEXT NOT NULL,
    queries TEXT NOT NULL, criteria TEXT NOT NULL, alert INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, last_hit_at TEXT,
    origin TEXT NOT NULL DEFAULT 'human', visibility TEXT NOT NULL DEFAULT 'internal'
);
CREATE TABLE IF NOT EXISTS studio_metric_points (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, watch_id TEXT NOT NULL, period TEXT NOT NULL,
    value REAL NOT NULL, unit TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'human', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS studio_metric_points_watch ON studio_metric_points(watch_id, period);
CREATE TABLE IF NOT EXISTS studio_plan_changes (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, watch_id TEXT NOT NULL, action TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', before TEXT NOT NULL DEFAULT '{}', after TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, reverted_at TEXT
);
CREATE INDEX IF NOT EXISTS studio_plan_changes_project ON studio_plan_changes(project_id, created_at);
CREATE TABLE IF NOT EXISTS studio_watch_hits (
    watch_id TEXT NOT NULL, item_id TEXT NOT NULL, created_at TEXT NOT NULL, notified_at TEXT,
    PRIMARY KEY (watch_id, item_id)
);
CREATE TABLE IF NOT EXISTS studio_threads (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    steps TEXT NOT NULL, next_step TEXT NOT NULL DEFAULT '', query TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS studio_threads_project ON studio_threads(project_id, updated_at);
"""

RULE_KINDS = ("block_source", "exclude_entity", "exclude_term")
FEEDBACK_KINDS = ("irrelevant", "wrong_entity", "bad_source", "importance")

# Storyline stages: order matters; the last stage closes the thread.
THREAD_STAGES: dict[str, tuple[str, ...]] = {
    "融资": ("传闻", "官宣", "交割"),
    "客户与订单": ("接触/试用", "定点/中标", "小批量", "量产/交付"),
    "产品": ("研发/预告", "发布", "获证/上市", "规模销售"),
    "审评与临床": ("申报/受理", "临床推进", "获批"),
    "合作": ("意向/签约", "落地"),
    "产能": ("规划", "开工", "投产"),
    "人事": ("传闻", "任命/离任"),
    "诉讼与监管": ("立案/调查", "判决/处罚", "执行/整改"),
}
STALL_DAYS = 90


def now() -> str:
    return datetime.now(UTC).isoformat()


class MonitorStore:
    def __init__(self, store) -> None:
        self.store = store
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)
            db.executescript(EXPLORATION_SCHEMA)
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            change_columns = {r[1] for r in db.execute("PRAGMA table_info(studio_plan_changes)")}
            if "state" not in change_columns:  # applied | proposed | accepted | rejected
                db.execute("ALTER TABLE studio_plan_changes ADD COLUMN state TEXT NOT NULL DEFAULT 'applied'")
            if "evidence" not in change_columns:
                db.execute("ALTER TABLE studio_plan_changes ADD COLUMN evidence TEXT NOT NULL DEFAULT '[]'")
            columns = {r[1] for r in db.execute("PRAGMA table_info(studio_watchpoints)")}
            if "match_fingerprint" not in {r[1] for r in db.execute("PRAGMA table_info(studio_watch_hits)")}:
                db.execute("ALTER TABLE studio_watch_hits ADD COLUMN match_fingerprint TEXT NOT NULL DEFAULT ''")
            # Monitoring-plan fields (see monitoring_plan.py); older rows read as active core questions.
            for name, default in (("origin", "human"), ("visibility", "internal"), ("axis", "self"), ("objects", "[]"),
                                  ("reason", ""), ("current_state", ""), ("sources", ""), ("cadence", "medium"),
                                  ("trigger_rule", ""), ("exit_rule", ""), ("tier", "core"), ("status", "active"),
                                  ("plan_origin", ""), ("updated_at", ""), ("last_run_at", ""),
                                  ("view", "evidence"), ("view_params", "{}"),
                                  ("reviewed_at", ""), ("last_attempt_at", ""), ("completed_queries", "[]"),
                                  ("hop", "0"), ("parent_id", ""), ("lifecycle_version", "0"),
                                  ("scope_kind", ""), ("relation_type", ""), ("relation_target", ""),
                                  ("relation_aliases", "[]")):
                if name not in columns:
                    db.execute(f"ALTER TABLE studio_watchpoints ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            if "reviewed_at" not in columns:
                # Upgrade existing confirmations as well as future ones, without rewriting provenance.
                db.execute("UPDATE studio_watchpoints SET reviewed_at=COALESCE((SELECT MAX(c.created_at) "
                           "FROM studio_plan_changes c WHERE c.watch_id=studio_watchpoints.id "
                           "AND c.actor='human' AND c.state IN ('applied','accepted')), '')")

    def _db(self):
        return closing(self.store._connect())

    # ------------------------------------------------------------ site pages
    def pages(self, project_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM studio_site_pages WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()
        return [{k: v for k, v in dict(r).items() if k not in ("last_blocks", "last_hash")} for r in rows]

    def add_page(self, project_id: str, url: str, label: str, kind: str, added_by: str) -> dict:
        normalised = sw.normalise_url(url)
        if not normalised:
            raise ValueError("网址格式不正确")
        if len(self.pages(project_id)) >= sw.MAX_PAGES:
            raise ValueError("监测页面已达上限")
        page_id = uuid.uuid4().hex
        with self._db() as db, db:
            db.execute("""INSERT OR IGNORE INTO studio_site_pages (id, project_id, url, label, kind, added_by, created_at)
                          VALUES (?,?,?,?,?,?,?)""", (page_id, project_id, normalised, label[:40] or sw.KIND_LABEL.get(kind, "页面"),
                                                      kind, added_by, now()))
            row = db.execute("SELECT * FROM studio_site_pages WHERE project_id=? AND url=?", (project_id, normalised)).fetchone()
        return dict(row)

    def remove_page(self, page_id: str) -> None:
        with self._db() as db, db:
            if db.execute("DELETE FROM studio_site_pages WHERE id=?", (page_id,)).rowcount == 0:
                raise KeyError(page_id)

    def due_pages(self, project_id: str, hours: int = sw.CHECK_HOURS) -> list[dict]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        with self._db() as db:
            rows = db.execute("""SELECT * FROM studio_site_pages WHERE project_id=?
                                 AND (last_checked IS NULL OR last_checked < ?)""", (project_id, cutoff)).fetchall()
        return [dict(r) for r in rows]

    def save_check(self, page_id: str, *, status: str, error: str = "", blocks: list[str] | None = None,
                   changed: bool = False) -> None:
        with self._db() as db, db:
            if blocks is None:
                db.execute("UPDATE studio_site_pages SET status=?, error=?, last_checked=? WHERE id=?",
                           (status, error[:120], now(), page_id))
            else:
                digest = hashlib.sha256("\n".join(blocks).encode()).hexdigest()
                db.execute("""UPDATE studio_site_pages SET status=?, error='', last_checked=?, last_hash=?, last_blocks=?,
                              last_changed=CASE WHEN ? THEN ? ELSE last_changed END WHERE id=?""",
                           (status, now(), digest, json.dumps(blocks, ensure_ascii=False), 1 if changed else 0, now(), page_id))

    def add_change(self, project_id: str, page: dict, added: list[str], removed: list[str]) -> dict:
        change_id = uuid.uuid4().hex
        title = sw.change_title(page["label"], added, removed)
        body = sw.change_text(page["label"], added, removed)
        item_id = uuid.uuid4().hex
        ts = now()
        with self._db() as db, db:
            db.execute("INSERT INTO studio_site_changes VALUES (?,?,?,?,?,?,?,?,?)",
                       (change_id, project_id, page["id"], page["url"], page["label"], ts,
                        json.dumps(added, ensure_ascii=False), json.dumps(removed, ensure_ascii=False), item_id))
            # The change enters the normal pipeline as an item: triage judges and explains it.
            db.execute("""INSERT INTO studio_items (id, project_id, key, category, title, url, source, snippet,
                          published_at, collected_at, extra, body, body_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (item_id, project_id, f"site:{change_id}", "company", title[:300], page["url"], "公司官网",
                        body[:1200], None, ts, json.dumps({"site_change": change_id, "page_kind": page["kind"]}),
                        body[:12000], "site_diff"))
        return {"id": change_id, "item_id": item_id, "title": title}

    def changes(self, project_id: str, limit: int = 20) -> list[dict]:
        with self._db() as db:
            rows = db.execute("""SELECT c.*, e.id AS event_id, i.importance, i.impact FROM studio_site_changes c
                                 LEFT JOIN studio_items i ON i.id=c.item_id LEFT JOIN studio_events e ON e.id=i.event_id
                                 WHERE c.project_id=? ORDER BY c.detected_at DESC LIMIT ?""", (project_id, limit)).fetchall()
        return [{**dict(r), "added": json.loads(r["added"]), "removed": json.loads(r["removed"])} for r in rows]

    # ------------------------------------------------------------ rules & feedback
    def rules(self, project_id: str) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM studio_rules WHERE project_id=? ORDER BY created_at DESC",
                                                (project_id,))]

    def add_rule(self, project_id: str, kind: str, value: str, note: str = "") -> dict:
        value = " ".join((value or "").split())[:80]
        if kind not in RULE_KINDS or len(value) < 2:
            raise ValueError("规则无效")
        with self._db() as db, db:
            db.execute("INSERT OR IGNORE INTO studio_rules (id, project_id, kind, value, note, created_at) VALUES (?,?,?,?,?,?)",
                       (uuid.uuid4().hex, project_id, kind, value, note[:120], now()))
            return dict(db.execute("SELECT * FROM studio_rules WHERE project_id=? AND kind=? AND value=?",
                                   (project_id, kind, value)).fetchone())

    def remove_rule(self, rule_id: str) -> None:
        with self._db() as db, db:
            if db.execute("DELETE FROM studio_rules WHERE id=?", (rule_id,)).rowcount == 0:
                raise KeyError(rule_id)

    def count_hit(self, rule_id: str) -> None:
        with self._db() as db, db:
            db.execute("UPDATE studio_rules SET hits=hits+1 WHERE id=?", (rule_id,))

    def add_feedback(self, project_id: str, event_id: str, kind: str, title: str, before: str = "", after: str = "") -> None:
        with self._db() as db, db:
            db.execute("INSERT INTO studio_feedback VALUES (?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, project_id, event_id, kind, title[:200], before, after, now()))

    def feedback_examples(self, project_id: str, limit: int = 10) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute("""SELECT kind, title, before, after FROM studio_feedback WHERE project_id=?
                                                   ORDER BY created_at DESC LIMIT ?""", (project_id, limit))]

    # ------------------------------------------------------------ watch points
    def watchpoints(self, project_id: str, *, db=None) -> list[dict]:
        with self._db() if db is None else nullcontext(db) as db:
            rows = db.execute("""SELECT w.*, (SELECT count(*) FROM studio_watch_hits h WHERE h.watch_id=w.id) AS hits
                                 FROM studio_watchpoints w WHERE project_id=? ORDER BY created_at""", (project_id,)).fetchall()
        return [{**dict(r), "queries": json.loads(r["queries"]), "alert": bool(r["alert"]),
                 "objects": json.loads(r["objects"] or "[]"), "view_params": json.loads(r["view_params"] or "{}"),
                 "relation_aliases": json.loads(r["relation_aliases"] or "[]")} for r in rows]

    def add_watchpoint(self, project_id: str, text: str, label: str, queries: list[str], criteria: str, alert: bool,
                       *, origin: str = "human", visibility: str = "internal", db=None) -> dict:
        if origin not in ("human", "ai"):
            raise ValueError("unknown watch point origin")
        if visibility not in ("public", "internal"):
            raise ValueError("unknown watch point visibility")
        watch_id = uuid.uuid4().hex
        with (self._db() if db is None else nullcontext(db)) as connection, (connection if db is None else nullcontext()):
            connection.execute("""INSERT INTO studio_watchpoints (id, project_id, text, label, queries, criteria, alert, created_at, origin, visibility)
                          VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       (watch_id, project_id, text[:200], label[:24], json.dumps(queries[:4], ensure_ascii=False),
                        criteria[:300], 1 if alert else 0, now(), origin, visibility))
            return next(w for w in self.watchpoints(project_id, db=connection) if w["id"] == watch_id)

    def update_watchpoint(self, watch_id: str, *, alert: bool | None = None) -> None:
        with self._db() as db, db:
            if alert is not None and db.execute("UPDATE studio_watchpoints SET alert=? WHERE id=?",
                                                (1 if alert else 0, watch_id)).rowcount == 0:
                raise KeyError(watch_id)

    def remove_watchpoint(self, watch_id: str) -> None:
        with self._db() as db, db:
            if db.execute("DELETE FROM studio_watchpoints WHERE id=?", (watch_id,)).rowcount == 0:
                raise KeyError(watch_id)
            db.execute("DELETE FROM studio_watch_hits WHERE watch_id=?", (watch_id,))
            # Judgements stay as history only while the question exists.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_question_assessments'").fetchone():
                db.execute("DELETE FROM studio_question_assessments WHERE watch_id=?", (watch_id,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_question_consumed_hits'").fetchone():
                db.execute("DELETE FROM studio_question_consumed_hits WHERE watch_id=?", (watch_id,))
            for table in ('studio_question_consumed_materials', 'studio_question_material_hits',
                          'studio_question_assessment_attempts'):
                if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                    db.execute(f'DELETE FROM {table} WHERE watch_id=?', (watch_id,))

    def add_hits(self, hits: list[tuple[str, str]]) -> None:
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            hits = [(w, i) for w, i in hits if db.execute(
                "SELECT 1 FROM studio_watchpoints WHERE id=? AND status!='retired'", (w,)).fetchone()]
            db.executemany("INSERT OR IGNORE INTO studio_watch_hits (watch_id, item_id, created_at) VALUES (?,?,?)",
                           [(w, i, now()) for w, i in hits])
            db.executemany("UPDATE studio_watchpoints SET last_hit_at=? WHERE id=?", [(now(), w) for w, _ in hits])

    def watch_event_ids(self, watch_id: str) -> list[str]:
        with self._db() as db:
            return [r[0] for r in db.execute("""SELECT DISTINCT i.event_id FROM studio_watch_hits h JOIN studio_items i
                                                ON i.id=h.item_id WHERE h.watch_id=? AND i.event_id IS NOT NULL""", (watch_id,))]

    def event_watch_labels(self, project_id: str) -> dict[str, list[str]]:
        with self._db() as db:
            rows = db.execute("""SELECT i.event_id, w.label FROM studio_watch_hits h JOIN studio_items i ON i.id=h.item_id
                                 JOIN studio_watchpoints w ON w.id=h.watch_id WHERE w.project_id=? AND i.event_id IS NOT NULL""",
                              (project_id,)).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r[0], [])
            if r[1] not in out[r[0]]:
                out[r[0]].append(r[1])
        return out

    def pending_watch_alerts(self, days: int = 3) -> list[dict]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._db() as db:
            rows = db.execute("""SELECT h.watch_id, h.item_id, w.label, w.project_id, i.title, i.summary, i.url, i.impact
                                 FROM studio_watch_hits h JOIN studio_watchpoints w ON w.id=h.watch_id
                                 JOIN studio_items i ON i.id=h.item_id
                                 JOIN studio_projects p ON p.project_id=w.project_id
                                 WHERE w.alert=1 AND h.notified_at IS NULL AND h.created_at >= ? AND i.relevant=1
                                 AND p.consent_at IS NOT NULL AND p.status='active'""",
                              (since,)).fetchall()
        return [dict(r) for r in rows]

    def mark_watch_notified(self, pairs: list[tuple[str, str]]) -> None:
        with self._db() as db, db:
            db.executemany("UPDATE studio_watch_hits SET notified_at=? WHERE watch_id=? AND item_id=?",
                           [(now(), w, i) for w, i in pairs])

    # ------------------------------------------------------------ threads
    def threads(self, project_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM studio_threads WHERE project_id=? ORDER BY updated_at DESC", (project_id,)).fetchall()
        return [thread_view(dict(r)) for r in rows]

    def save_thread(self, thread: dict) -> None:
        with self._db() as db, db:
            db.execute("""INSERT OR REPLACE INTO studio_threads (id, project_id, kind, title, steps, next_step, query,
                          created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (thread["id"], thread["project_id"], thread["kind"], thread["title"][:60],
                        json.dumps(thread["steps"], ensure_ascii=False), thread.get("next_step", "")[:80],
                        thread.get("query", "")[:80], thread.get("created_at") or now(), now()))

    def threaded_event_ids(self, project_id: str) -> set[str]:
        return {s["event_id"] for t in self.threads(project_id) for s in t["steps"]}


def thread_view(row: dict) -> dict:
    steps = json.loads(row["steps"]) if isinstance(row["steps"], str) else row["steps"]
    stages = THREAD_STAGES.get(row["kind"], ())
    latest_date = max((s.get('date') or '' for s in steps if s['stage'] in stages), default='')
    latest_stages = {s['stage'] for s in steps if s['stage'] in stages and (s.get('date') or '') == latest_date}
    reached = stages.index(next(iter(latest_stages))) if len(latest_stages) == 1 else -1
    done = bool(stages) and reached == len(stages) - 1
    last = max((s.get("date") or "" for s in steps), default="") or row["updated_at"]
    stalled = not done and last and last[:10] < (datetime.now(UTC) - timedelta(days=STALL_DAYS)).date().isoformat()
    return {**row, "steps": steps, "stages": list(stages), "reached": reached,
            "status": "done" if done else "stalled" if stalled else "open", "last_at": last,
            "support": "legacy_unverified", "current_stage": stages[reached] if reached >= 0 else None,
            "stage_conflict": len(latest_stages) > 1, "business_silence": "not_evaluated"}


# ---------------------------------------------------------------- rules applied before the model

def host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")


def rule_match(item: dict, rules: list[dict], names: list[str]) -> dict | None:
    """First rule an item violates. An item naming the company itself is never excluded by name rules."""
    text = f"{item.get('title') or ''} {item.get('snippet') or ''}"
    host = host_of(item.get("url") or "")
    mentions_us = any(n and n in text for n in names)
    for rule in rules:
        value = rule["value"]
        if rule["kind"] == "block_source" and (host == value or host.endswith("." + value) or (item.get("source") or "") == value):
            return rule
        if rule["kind"] == "exclude_entity" and value in text and not mentions_us:
            return rule
        if rule["kind"] == "exclude_term" and value in text:
            return rule
    return None


def feedback_prompt(examples: list[dict]) -> str:
    if not examples:
        return ""
    label = {"major": "重大", "notable": "关注", "routine": "一般"}
    lines = []
    for e in examples:
        if e["kind"] == "importance":
            lines.append(f"- 《{e['title']}》 用户把重要性从{label.get(e['before'], e['before'])}改为{label.get(e['after'], e['after'])}")
        else:
            lines.append(f"- 《{e['title']}》 用户判断为不相关（{ {'irrelevant': '与公司无关', 'wrong_entity': '同名其他主体', 'bad_source': '来源质量差'}.get(e['kind'], '')}）")
    return "用户过往校正（请据此调整判断尺度）：\n" + "\n".join(lines)


def watch_prompt(watchpoints: list[dict]) -> str:
    """Monitoring-plan focus points for triage: the question plus its section, objects and why it matters,
    so news about a competitor, a disease, a standard, a raw material or a customer can be judged against
    the question even when the monitored company is not named."""
    if not watchpoints:
        return ""
    from project_watch.studio.monitoring_plan import AXIS_LABEL, OWN

    lines = []
    for n, w in enumerate(watchpoints):
        parts = [f"[W{n + 1}] {w['text']}"]
        if w.get("axis") and w["axis"] not in OWN:
            parts.append(f"分类：{AXIS_LABEL.get(w['axis'], w['axis'])}")
        if w.get("objects"):
            parts.append(f"对象：{'、'.join(w['objects'])}")
        if w.get("reason"):
            parts.append(f"与公司的关系：{w['reason'][:120]}")
        if w.get("criteria"):
            parts.append(f"判断标准：{w['criteria']}")
        lines.append("；".join(parts))
    return ("监控方案关注点（命中时在该条结果中加 \"watch\":[\"W1\"]，并且重要性至少为 notable）。\n"
            "关于竞品、需求、技术、供应链、政策类关注点：信息即使不提被监控公司，只要直接涉及该关注点的对象和问题，"
            "也判为 relevant 并标注 watch；impact 写明经由这个关注点对公司意味着什么。仅字面提到对象但与问题无关的不算命中。\n"
            + "\n".join(lines))


_WATCH_SYSTEM = """你是投资机构的研究助理。投资人用一句话描述想重点盯住的事情，请把它转成监控设置。
输出 JSON：{"label":"不超过8个字的短名","queries":["2-3 个中文检索词，包含公司名或简称"],"criteria":"一句话：什么样的信息算命中"}"""


def build_watchpoints(llm, project: dict, texts: list[str]) -> list[dict]:
    """Compile the initial watch points in one call, preserving every input on failure."""
    fallbacks = [build_watchpoint(None, project, text) for text in texts]
    if not texts or not llm or not getattr(llm, "ready", False):
        return fallbacks
    system = (_WATCH_SYSTEM + '\n一次处理所有关注点，按 id 对应输入。输出 JSON：'
              '{"watchpoints":[{"id":"W1","label":"短名","queries":["检索词"],"criteria":"命中标准"}]}')
    user = f"公司：{project.get('legal_name') or project.get('name') or ''}\n" + "\n".join(
        f"[W{n + 1}] {text}" for n, text in enumerate(texts))
    try:
        data = llm.json(system, user, max_tokens=1800)
        rows = data.get("watchpoints", [])
        specs = {r["id"]: r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)}
        result = []
        for n, fallback in enumerate(fallbacks):
            row = specs.get(f"W{n + 1}", {})
            queries = row.get("queries")
            queries = [q.strip()[:60] for q in queries if isinstance(q, str) and q.strip()][:3] if isinstance(queries, list) else []
            result.append({"label": str(row.get("label") or fallback["label"])[:12],
                           "queries": queries or fallback["queries"],
                           "criteria": str(row.get("criteria") or fallback["criteria"])[:200]})
        return result
    except Exception:
        return fallbacks


def build_watchpoint(llm, project: dict, text: str) -> dict:
    name = project.get("short_name") or project.get("name") or project.get("legal_name") or ""
    fallback = {"label": text[:8], "queries": [f"{name} {text}"[:60]], "criteria": f"与“{text}”直接相关的消息"}
    if not llm or not getattr(llm, "ready", False):
        return fallback
    try:
        data = llm.json(_WATCH_SYSTEM, f"公司：{project.get('legal_name') or name}（简称 {name}）\n关注点：{text}", max_tokens=800)
    except Exception:  # model failures fall back to a plain query; the watch point still works
        return fallback
    queries = [str(q)[:60] for q in data.get("queries") or [] if str(q).strip()][:3] or fallback["queries"]
    return {"label": str(data.get("label") or fallback["label"])[:12], "queries": queries,
            "criteria": str(data.get("criteria") or fallback["criteria"])[:200]}


# ---------------------------------------------------------------- storylines

_THREAD_SYSTEM = """你是投资机构的研究助理，负责把公司的事件串成“事件线索”，判断每条新事件属于哪条进行中的线索、推进到了哪一阶段。
线索类型与阶段（只能使用这些名称，按先后顺序）：
{vocab}
规则：同一笔融资、同一个客户/订单、同一款产品、同一项审评、同一个合作、同一个基地才属于同一线索；
与已有线索无关的重大/关注事件可以新建线索；纯行业背景、观点访谈不建线索。
next_step 写下一阶段要看到的具体事实；query 写用于检索下一步进展的中文检索词（含公司名）。
输出 JSON：{{"assign":[{{"event":"N1","thread":"T2 或 new","kind":"融资","stage":"官宣","title":"线索名，如 B轮融资","next_step":"","query":""}}]}}"""


def thread_system() -> str:
    vocab = "\n".join(f"- {k}：{' → '.join(v)}" for k, v in THREAD_STAGES.items())
    return _THREAD_SYSTEM.format(vocab=vocab)


def update_threads(monitor: MonitorStore, store, llm, project_id: str, project: dict) -> int:
    """Assign recent important events to storylines. Returns the number of events placed."""
    if not llm or not getattr(llm, "ready", False):
        return 0
    placed = monitor.threaded_event_ids(project_id)
    events = [e for e in store.list_events(project_id=project_id, since_days=30, limit=80)
              if e["id"] not in placed and e["importance"] in ("major", "notable") and e["category"] in ("company", "trial", "patent")]
    if not events:
        return 0
    threads = [t for t in monitor.threads(project_id) if t["status"] != "done"][:12]
    t_ids = {f"T{n + 1}": t for n, t in enumerate(threads)}
    n_ids = {f"N{n + 1}": e for n, e in enumerate(events[:30])}
    user = (f"公司：{project.get('legal_name') or project.get('name')}\n\n进行中的线索：\n"
            + ("\n".join(f"[{k}] {t['kind']}｜{t['title']}｜已到：{' → '.join(s['stage'] for s in t['steps'])}" for k, t in t_ids.items()) or "（无）")
            + "\n\n新事件：\n" + "\n".join(f"[{k}] {(e.get('event_at') or e['created_at'])[:10]} {e.get('summary') or e['title']}"
                                          for k, e in n_ids.items()))
    try:
        data = llm.json(thread_system(), user, max_tokens=2500)
    except Exception as exc:  # storylines are an enhancement; monitoring continues without them
        _log.info("thread update failed: %s", exc)
        return 0
    count = 0
    created: dict[str, dict] = {}
    assigned = set()
    for a in data.get("assign") or []:
        if not isinstance(a, dict):
            continue
        event = n_ids.get(str(a.get("event", "")).upper())
        kind = a.get("kind") if a.get("kind") in THREAD_STAGES else None
        if not event or event["id"] in assigned or not kind or a.get("stage") not in THREAD_STAGES[kind]:
            continue
        key = str(a.get("thread") or "new")
        if key.lower() != "new" and key.upper() not in t_ids:
            continue
        thread = t_ids.get(key.upper()) or created.get(str(a.get("title") or ""))
        from project_watch.studio.events import conflicting

        if thread and any(conflicting(event.get("summary") or event["title"], s["title"]) for s in thread["steps"]):
            thread = None  # A model cannot join different financing amounts/rounds or patent identities.
        if thread is None or thread["kind"] != kind:
            thread = {"id": uuid.uuid4().hex, "project_id": project_id, "kind": kind,
                      "title": str(a.get("title") or event.get("summary") or event["title"])[:40], "steps": [], "created_at": now()}
            created[thread["title"]] = thread
        thread["steps"] = [*thread["steps"], {"stage": a["stage"], "event_id": event["id"],
                                             "date": (event.get("event_at") or event["created_at"])[:10],
                                             "title": event.get("summary") or event["title"], "url": event.get("url") or ""}]
        thread["steps"].sort(key=lambda s: (THREAD_STAGES[kind].index(s["stage"]), s["date"]))
        thread["next_step"] = str(a.get("next_step") or "")[:80]
        thread["query"] = str(a.get("query") or "")[:80]
        monitor.save_thread(thread)
        assigned.add(event["id"])
        count += 1
    return count


def thread_queries(monitor: MonitorStore, project_id: str, limit: int = 4) -> list[str]:
    """Searches for the next step of open storylines ("盯着下一步")."""
    return [t["query"] for t in monitor.threads(project_id) if t["status"] != "done" and t.get("query")][:limit]


# ---------------------------------------------------------------- website checks

SITE_RETRY_DAYS = 7


def site_discovery(store, project_id: str) -> dict:
    """How the official site was (or was not) found during a monitoring round; {} before the first try."""
    raw = store.get_state(f"site_discovery:{project_id}")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def check_sites(monitor: MonitorStore, transport, project_id: str, website: str | None,
                stage=lambda _t: None, *, execution=None, candidates: list[str] | None = None,
                names: list[str] | None = None, recovery_key: str | None = None,
                force: bool = False, record_changes: bool = True, on_snapshot=None, guard=None,
                journal_requests: bool = True) -> dict:
    """Discover pages (first run), then read due pages and record changes. Returns a small report.

    Without a website, ``candidates`` are possible homepages from a search: one is adopted only when
    its page source carries one of ``names``; the report then says which (``found``) and why.
    Host collection can force snapshots without diff events. It disables this
    logical-request journal only when its transport journals each physical GET."""
    from project_watch.feeds import FeedError
    from project_watch.studio.http import HttpError, Response

    report = {"checked": 0, "changed": 0, "errors": []}
    fetched = {}
    existing = monitor.pages(project_id)
    finding_site = bool(not website and not existing and candidates)
    # Only this guarded, credential-free public GET path may turn a source
    # failure into a durable unreadable receipt. This is NOT an HTTP success
    # or permission to retry; paid model/search/commercial journals are unchanged.

    def fetch(url: str):
        if guard:
            guard()
        if url in fetched:
            return fetched[url]
        def request():
            from project_watch.feeds import GuardedHTTPS
            from project_watch.host_network import HostArticleTransport

            try:
                if isinstance(transport, HostArticleTransport):
                    raise FeedError("robots_redirect_guard_unavailable")
                if isinstance(transport, GuardedHTTPS):
                    # Keep public-IP pinning and robots-before-redirect enforcement.
                    def allowed(target):
                        if urlsplit(url).path == "/robots.txt":
                            return urlsplit(target).netloc == urlsplit(url).netloc
                        return robots.allowed(target)

                    response = transport.get_response(url, allow_redirect=allowed)
                else:
                    response = transport.get_response(url)
            except FeedError as exc:
                if str(exc) == 'http_failure' and exc.status is not None:
                    return Response(exc.status, {}, b'')
                if str(exc) == 'rate_limited':
                    return Response(429, {}, b'')
                code = str(exc) if re.fullmatch(r'[a-z_]{1,80}', str(exc)) else 'source_unavailable'
                return Response(599, {}, code.encode('ascii'))
            except OSError:
                return Response(599, {}, b'connection_failed')
            except ValueError:
                return Response(599, {}, b'invalid_source')
            return Response(200, {'content-type': response.headers.get('content-type', '')}, response.body)
        if execution:
            import hashlib

            try:
                response = (execution.call('site', hashlib.sha256(('site:' + url).encode()).hexdigest(),
                                           request, provider='guarded_site:' + urlsplit(url).netloc)
                            if journal_requests else request())
            except Exception as exc:
                if isinstance(exc, HttpError) and exc.code in ('provider_backoff', 'request_budget'):
                    if finding_site or recovery_key:
                        raise  # Candidate loop records refusal or ends discovery.
                    # A refused site or exhausted site-only read budget must not
                    # consume/block the independent search and analysis stages.
                    response = Response(599, {}, exc.code.encode('ascii'))
                elif any(row['unknown'] for row in execution.usage().values()):
                    raise HttpError('request_outcome_unknown') from exc
                else:
                    raise
        else:
            response = request()
        if response.status == 599:
            code = response.body.decode('ascii', errors='replace')
            code = code if re.fullmatch(r'[a-z_]{1,80}', code) else 'source_unavailable'
            report['errors'].append(f'官网读取：{code}')
        fetched[url] = response.body, response.headers.get("content-type", ""), response.status
        return fetched[url]

    robots = sw.Robots(fetch, fatal_errors=(RuntimeError, HttpError) if execution else ())
    if recovery_key:
        # Reconstruct only persisted targets. Do not search or follow newly found links.
        import hashlib

        urls = {p['url'] for p in existing}
        if website and (normalised := sw.normalise_url(website)):
            urls.add(normalised)
        urls |= {f'{urlsplit(u).scheme}://{urlsplit(u).netloc}/robots.txt' for u in urls}
        matches = [u for u in urls if hashlib.sha256(('site:' + hashlib.sha256(
            ('site:' + u).encode()).hexdigest()).encode()).hexdigest() == recovery_key]
        if len(matches) != 1 or execution is None:
            raise HttpError('recovery_request_unavailable')
        target = matches[0]
        if urlsplit(target).path != '/robots.txt' and not robots.allowed(target):
            raise HttpError('recovery_robots_unavailable')
        _body, _ctype, status = fetch(target)
        # This receipt is not a claim of successful collection or a company update.
        return {**report, 'recovered': True, 'response_status': status}
    if not website and not existing and candidates:
        stage("确认公司官网")
        report["tried"], report["rejected"] = [], {}
        for home in candidates:
            report["tried"].append(home)
            try:
                if not robots.allowed(home):
                    report["rejected"][home] = "robots"
                    continue
                body, ctype, status = fetch(home)
                html = sw.decode(body, ctype) if status == 200 else None
                matched = sw.names_company(html or "", names or [])
                if matched:
                    website, report["found"], report["matched"] = home, home, matched
                    break
                report["rejected"][home] = "no_name" if html else "unreadable"
            except HttpError as exc:
                # A candidate host refusing earlier (backoff) is one more rejected candidate; out of
                # site budget ends the search quietly. Only an unknown outcome stops the job.
                if exc.code == "provider_backoff":
                    report["rejected"][home] = "refused"
                    continue
                if exc.code == "request_budget":
                    break
                if execution:
                    raise
                report["errors"].append(f"确认官网：{exc.code}")
            except Exception as exc:
                if execution and isinstance(exc, RuntimeError) and not isinstance(exc, FeedError):
                    raise
                report["errors"].append(f"确认官网：{type(exc).__name__}")
        finding_site = False
    discover_due = not existing or (len(existing) == 1 and existing[0]["added_by"] == "auto" and monitor.due_pages(project_id))
    if website and discover_due:
        home = sw.normalise_url(website)
        if home:
            if not existing:
                monitor.add_page(project_id, home, "首页", "home", "auto")
            try:
                if robots.allowed(home):
                    body, ctype, _ = fetch(home)
                    html = sw.decode(body, ctype)
                    if html:
                        _blocks, links = sw.snapshot(html)
                        for found in sw.discover(home, links):
                            monitor.add_page(project_id, found["url"], found["label"], found["kind"], "auto")
            except Exception as exc:  # discovery is best effort; the homepage itself is still watched
                if execution and isinstance(exc, (RuntimeError, HttpError)) and not isinstance(exc, FeedError):
                    raise
                report["errors"].append(f"发现页面：{type(exc).__name__}")
    due = monitor.pages(project_id) if force else monitor.due_pages(project_id)
    if due:
        stage(f"检查官网 {len(due)} 个页面")
    for page in due:
        try:
            if not robots.allowed(page["url"]):
                monitor.save_check(page["id"], status="robots_disallowed", error="robots.txt 未获允许或暂不可读取，本轮未访问页面")
                report['errors'].append(f"{page['label']}：robots_disallowed")
                continue
            body, ctype, status = fetch(page["url"])
            if status != 200:
                if status == 599:
                    code = body.decode('ascii', errors='replace')
                    raise FeedError(code if re.fullmatch(r'[a-z_]{1,80}', code) else 'source_unavailable')
                raise FeedError("http_failure", status=status)
            html = sw.decode(body, ctype)
            if html is None:
                monitor.save_check(page["id"], status="unreadable", error="不是网页内容")
                continue
            blocks, _links = sw.snapshot(html)
            if len(blocks) < 3:
                monitor.save_check(page["id"], status="unreadable", error="页面内容需要脚本加载，无法读取")
                continue
            report["checked"] += 1
            if on_snapshot:
                on_snapshot(page, blocks)
            previous = _previous_blocks(monitor, page["id"])
            if previous is None:
                monitor.save_check(page["id"], status="ok", blocks=blocks)
                continue
            added, removed = sw.diff(previous, blocks)
            changed = bool(added or removed)
            if changed and record_changes:
                monitor.add_change(project_id, page, added, removed)
                report["changed"] += 1
            monitor.save_check(page["id"], status="ok", blocks=blocks, changed=changed)
        except Exception as exc:  # one site failure must not stop external monitoring
            if execution and isinstance(exc, (RuntimeError, HttpError)) and not isinstance(exc, FeedError):
                raise
            code = str(exc) if re.fullmatch(r"[a-z_]+", str(exc) or "") else type(exc).__name__
            monitor.save_check(page["id"], status="error", error=code)
            report["errors"].append(f"{page['label']}：{code}")
    return report


def _previous_blocks(monitor: MonitorStore, page_id: str) -> list[str] | None:
    with monitor._db() as db:
        row = db.execute("SELECT last_blocks FROM studio_site_pages WHERE id=?", (page_id,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None
