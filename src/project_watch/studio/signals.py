"""Monitoring signals beyond single news items.

- 内外对照: public reports checked against internal numbers and milestones (both sides cited);
- 沉默信号: things that stopped happening (no news, site frozen, hiring page emptied, storyline
  stalled, company stopped sending materials) — deterministic, no model;
- 自适应频率: busy projects are checked more often, quiet ones less, with the reason shown;
- 发现延迟: how long after publication an item was found, so coverage gaps are visible;
- 招投标与资质: government procurement / licence pages that name the company, reached only through
  the user's own search service (site: filters), never by scraping those portals.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from project_watch.studio import review

_log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_crosschecks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
    severity TEXT NOT NULL, topic TEXT NOT NULL, public_says TEXT NOT NULL, internal_says TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '', public_refs TEXT NOT NULL, internal_refs TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (project_id, fingerprint)
);
"""

CROSS_KINDS = {"mismatch": "数字或事实不一致", "undisclosed": "已提供片段未见提及", "off_plan": "与计划进度不符"}
FACT_KINDS = frozenset({"mismatch", "off_plan"})

TENDER_SITES = ("ccgp.gov.cn", "ggzy.gov.cn", "cebpubservice.com")
LICENSE_SITES = ("nmpa.gov.cn", "miit.gov.cn", "cnca.gov.cn", "mee.gov.cn")

MIN_HOURS, MAX_HOURS = 6, 72


def now() -> str:
    return datetime.now(UTC).isoformat()


def _days_ago(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - then).days)


class SignalStore:
    def __init__(self, store) -> None:
        self.store = store
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)

    def _db(self):
        return closing(self.store._connect())

    def crosschecks(self, project_id: str, include_closed: bool = False) -> list[dict]:
        sql = "SELECT * FROM studio_crosschecks WHERE project_id=?" + ("" if include_closed else " AND status='open'")
        with self._db() as db:
            rows = db.execute(sql + " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, updated_at DESC",
                              (project_id,)).fetchall()
        return [{**dict(r), "public_refs": json.loads(r["public_refs"]), "internal_refs": json.loads(r["internal_refs"]),
                 "kind_label": CROSS_KINDS.get(r["kind"], r["kind"])} for r in rows]

    def replace_crosschecks(self, project_id: str, found: list[dict]) -> int:
        """Open findings are replaced each run; ones the user resolved or dismissed stay closed."""
        ts = now()
        added = 0
        with self._db() as db, db:
            closed = {r[0] for r in db.execute("SELECT fingerprint FROM studio_crosschecks WHERE project_id=? AND status!='open'",
                                               (project_id,))}
            db.execute("DELETE FROM studio_crosschecks WHERE project_id=? AND status='open'", (project_id,))
            for f in found:
                if f["fingerprint"] in closed:
                    continue
                db.execute("""INSERT OR IGNORE INTO studio_crosschecks (id, project_id, fingerprint, kind, severity, topic,
                              public_says, internal_says, question, public_refs, internal_refs, created_at, updated_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (uuid.uuid4().hex, project_id, f["fingerprint"], f["kind"], f["severity"], f["topic"],
                            f["public_says"], f["internal_says"], f["question"],
                            json.dumps(f["public_refs"], ensure_ascii=False), json.dumps(f["internal_refs"], ensure_ascii=False),
                            ts, ts))
                added += 1
        return added

    def set_status(self, check_id: str, status: str) -> dict:
        if status not in ("open", "resolved", "dismissed"):
            raise ValueError("状态无效")
        with self._db() as db, db:
            if db.execute("UPDATE studio_crosschecks SET status=?, updated_at=? WHERE id=?", (status, now(), check_id)).rowcount == 0:
                raise KeyError(check_id)
            return dict(db.execute("SELECT project_id, status FROM studio_crosschecks WHERE id=?", (check_id,)).fetchone())


# ---------------------------------------------------------------- 内外对照

_CROSS_SYSTEM = """逐条对照同一公司的公开原文与内部指标、里程碑，列出有双侧依据的事实差异。
只输出 mismatch（同口径数字或事实不一致）和 off_plan（内部计划与公开进展不符）。
每条必须引用至少一个公开编号 N… 和至少一个内部编号 M… 或 K…。
不要把同行或行业信息当成公司自身事实。主体/合并范围、期间、币种、含税口径、
合同额与收入不同不算矛盾；万元与亿元先换算，实际数与预算/预测不算事实矛盾。
public_quotes 和 internal_quotes 是编号到连续原文摘录的对象，不改写数字或拼接原文。
只记录事实差异，不推断重要程度或提出沟通建议。没有足够依据则返回空列表，最多 5 条。
输出 JSON：{"checks":[{"kind":"mismatch|off_plan","topic":"不超过12字",
"public":["N1"],"internal":["M2"],"public_quotes":{"N1":"连续原文"},
"internal_quotes":{"M2":"连续原文"}}]}"""


def _metric_line(m: dict) -> str:
    value = m.get("value_norm") if m.get("value_norm") is not None else m.get("value")
    unit = m.get("norm_unit") or m.get("unit") or ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    basis = {"actual": "实际", "forecast": "预测", "budget": "预算"}.get(m.get("basis") or "", "口径未明")
    return (f"{m.get('name')} {m.get('period')} {value if value is not None else '—'}{unit}（{basis}）"
            f"；主体/范围：{m.get('scope') or '未明'}；币种：{m.get('currency') or '未明'}"
            + (f"；原文：{m['quote']}" if m.get('quote') else ""))


def crosscheck(signals: SignalStore, store, llm, project_id: str, *, policy=None) -> int:
    """Run the comparison when there is both internal material and recent public news. Returns findings kept."""
    profile = store.get_profile(project_id) or {}
    metrics = store.list_metrics(project_id)
    events = [e for e in store.list_events(project_id=project_id, since_days=120, limit=80)
              if e["category"] in ("company", "trial", "patent") and e["importance"] in ("major", "notable")][:30]
    if not events or not (metrics or profile.get("milestones")):
        return 0
    replaced = {id(s["plan"]) for s in review.superseded(metrics)}
    current = [m for m in metrics if id(m) not in replaced]
    current.sort(key=lambda m: (m.get("basis") == "actual", str(m.get("period_key") or m.get("period"))), reverse=True)
    internal: dict[str, dict] = {}
    for n, m in enumerate(current[:40]):
        internal[f"M{n + 1}"] = {"identity": m["id"], "label": _metric_line(m), "refs": [{"source_id": m["source_id"], "source_name": m.get("source_name") or "资料",
                                                                     "locator": m.get("locator") or ""}]}
    for n, k in enumerate((profile.get("milestones") or [])[:12]):
        status = "已完成" if k.get("status") == "done" else "计划"
        internal[f"K{n + 1}"] = {"identity": json.dumps(k, sort_keys=True, ensure_ascii=False), "label": f"{status} {k.get('date') or '时间未定'} {k.get('text', '')}",
                                 "refs": [{kk: r.get(kk, "") for kk in ("source_id", "source_name", "locator")} for r in k.get("refs") or []]}
    public = {f"N{n + 1}": e for n, e in enumerate(events)}
    public_sources = {}
    with closing(store._connect()) as db:
        for key, event in public.items():
            rows = db.execute("SELECT id, title, url, source, published_at, snippet, body FROM studio_items "
                              "WHERE event_id=? AND relevant=1 LIMIT 6", (event["id"],)).fetchall()
            public_sources[key] = [dict(row) for row in rows]
    excerpts = {key: "\n".join(" ".join(str(s.get(field) or "").split()) for s in sources
                                for field in ("title", "snippet", "body") if s.get(field))[:6000]
                for key, sources in public_sources.items()}
    company = store.get_project(project_id) or {}
    text = (f"公司：{company.get('legal_name') or company.get('name')}\n\n内部资料（公司提供给投资人）：\n"
            + "\n".join(f"[{k}] {v['label']}" for k, v in internal.items())
            + "\n\n公开信息（近 120 天）：\n"
            + "\n".join(f"[{k}] {(e.get('event_at') or e['created_at'])[:10]} 原文：{excerpts[k]}"
                        for k, e in public.items()))
    data = llm.json(policy.SYSTEM if policy is not None else _CROSS_SYSTEM, text, max_tokens=1800)
    kinds = policy.KINDS if policy is not None else FACT_KINDS
    found = []
    for c in (data.get("checks") or [])[:5] if isinstance(data, dict) else []:
        if not isinstance(c, dict) or c.get("kind") not in kinds or c.get("kind") not in CROSS_KINDS:
            continue
        pq, iq = c.get("public_quotes") or {}, c.get("internal_quotes") or {}
        if not isinstance(pq, dict) or not isinstance(iq, dict):
            continue
        def quoted(key, quotes, evidence):
            quote = quotes.get(key)
            return isinstance(quote, str) and len(quote.strip()) >= 4 and quote.strip() in evidence

        pub_ids = [r for r in dict.fromkeys(str(x).strip().upper() for x in c.get("public") or [])
                   if r in public and quoted(r, pq, excerpts[r])]
        int_ids = [r for r in dict.fromkeys(str(x).strip().upper() for x in c.get("internal") or [])
                   if r in internal and internal[r]["refs"] and quoted(r, iq, internal[r]["label"])]
        pubs = [public[r] for r in pub_ids]
        ints = [internal[r] for r in int_ids]
        if not pubs or (c["kind"] != "undisclosed" and not ints):
            continue  # evidence on both sides, or nothing
        topic = str(c.get("topic") or "")[:24]
        if c["kind"] == "mismatch" and any(not _same_scope(pq[p], internal[i]["label"])
                                          or _equivalent_amounts(pq[p], internal[i]["label"]) for p in pub_ids for i in int_ids):
            continue
        identities = sorted(internal[key]["identity"] for key in int_ids)
        fingerprint = hashlib.sha1(f"{c['kind']}|{sorted(p['id'] for p in pubs)}|{identities}".encode()).hexdigest()[:16]
        severity, question = policy.annotations(c) if policy is not None else ("unrated", "")
        found.append({
            "fingerprint": fingerprint, "kind": c["kind"],
            "severity": severity,
            "topic": topic or CROSS_KINDS[c["kind"]], "public_says": "；".join(pq[p].strip() for p in pub_ids)[:160],
            "internal_says": "；".join(iq[i].strip() for i in int_ids)[:160] if ints else "本次内部片段未见提及，未覆盖完整资料，系统暂不判断是否遗漏",
            "question": question,
            "public_refs": [{"id": public[key]["id"], "title": source["title"], "url": source["url"], "source": source["source"],
                             "date": (source.get("published_at") or public[key]["created_at"])[:10]}
                            for key in pub_ids for source in public_sources[key]
                            if any(pq[key].strip() in " ".join(str(source.get(field) or "").split()) for field in ("title", "snippet", "body"))][:6],
            "internal_refs": [{"label": i["label"], "refs": i["refs"][:2]} for i in ints],
        })
    return signals.replace_crosschecks(project_id, found)


def _same_scope(public: str, internal: str) -> bool:
    """Reject explicit incompatible scopes; absence of an explicit scope is not proof of equality."""
    pairs = ((('合同額', '合同额', '合同金额'), ('营业收入', '营收', '收入')),
             (('集团', '合并'), ('子公司', '公司本体')),
             (('预测', '预算'), ('实际',)), (('美元', 'USD'), ('人民币', 'CNY')))
    for left, right in pairs:
        pl, pr = any(x in public for x in left), any(x in public for x in right)
        il, ir = any(x in internal for x in left), any(x in internal for x in right)
        if (pl and ir and not il) or (pr and il and not ir):
            return False
    def tax(text):
        return 'net' if '不含税' in text else 'gross' if '含税' in text else None
    if tax(public) and tax(internal) and tax(public) != tax(internal):
        return False
    years_p, years_i = set(re.findall(r"20\d{2}", public)), set(re.findall(r"20\d{2}", internal))
    return not (years_p and years_i and years_p.isdisjoint(years_i))


def _equivalent_amounts(public: str, internal: str) -> bool:
    from project_watch.studio.normalize import normalize_amount

    def amounts(text):
        return {normalize_amount(float(value.replace(",", "")), unit)[0]
                for value, unit in re.findall(r"([\d,]+(?:\.\d+)?)\s*(万亿元|亿元|万元|千元|元)", text)}

    a, b = amounts(public), amounts(internal)
    return bool(a and b and a == b)


# ---------------------------------------------------------------- 沉默信号

def _own_event_dates(store, project_id: str, days: int) -> list[str]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with closing(store._connect()) as db:
        rows = db.execute("""SELECT COALESCE(event_at, created_at) FROM studio_events WHERE project_id=? AND status!='dismissed'
                             AND category IN ('company','trial','patent') AND COALESCE(event_at, created_at) >= ?""",
                          (project_id, since)).fetchall()
    return [r[0] for r in rows]


def silence(store, monitor, project_id: str, documents: list[dict] | None = None, *, base_hours=24) -> list[dict]:
    """Things that stopped happening. Each signal says why it fired; nothing here uses a model."""
    out: list[dict] = []
    project = store.get_project(project_id) or {}
    monitored_days = _days_ago(project.get("created_at")) or 0
    dates = _own_event_dates(store, project_id, 240)
    cutoff = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    recent = sum(1 for d in dates if d >= cutoff)
    before = len(dates) - recent  # the 180 days before
    from project_watch.studio.health import silence_coverage

    covered = silence_coverage(store, project_id, base_hours=base_hours)
    if covered and monitored_days >= 60 and before >= 6 and recent == 0:
        out.append({"kind": "news_quiet", "level": "watch", "title": "公开动态明显减少",
                    "detail": f"此前 180 天有 {before} 条公司自身动态，近 60 天已观察来源中未发现新动态；不代表业务变化。"})
    elif covered and monitored_days >= 60 and before >= 9 and recent * 3 <= before / 3:
        out.append({"kind": "news_quiet", "level": "info", "title": "公开动态减少",
                    "detail": f"近 60 天 {recent} 条，低于此前平均（约 {before / 3:.0f} 条 / 60 天）。"})
    pages = monitor.pages(project_id)
    page_kinds = {p["id"]: p["kind"] for p in pages}
    for page in pages:
        checked = page.get('last_checked')
        if page["status"] != "ok" or not checked or datetime.fromisoformat(checked) < datetime.now(UTC)-timedelta(hours=max(24, base_hours)*2):
            continue
        if page["kind"] == "news":
            last = page.get("last_changed") or page.get("created_at")
            idle = _days_ago(last)
            if idle is not None and idle >= 120 and (_days_ago(page.get("created_at")) or 0) >= 120:
                out.append({"kind": "site_frozen", "level": "watch", "title": "官网新闻长期未更新",
                            "detail": f"“{page['label']}”已 {idle} 天没有变化。"})
    for change in monitor.changes(project_id, 40):
        page_kind = page_kinds.get(change["page_id"], "")
        if page_kind == "careers" and len(change["removed"]) >= 3 and len(change["added"]) <= len(change["removed"]) // 3 \
                and (_days_ago(change["detected_at"]) or 0) <= 90:
            out.append({"kind": "hiring_cut", "level": "watch", "title": "招聘页大幅缩减",
                        "detail": f"{change['detected_at'][:10]} 招聘页删除 {len(change['removed'])} 条、新增 {len(change['added'])} 条。"
                                  "页面文字变化不等同于职位减少或裁员。"})
            break
    for t in monitor.threads(project_id):
        if covered and t["status"] == "stalled":
            stage = t["stages"][t["reached"]] if 0 <= t["reached"] < len(t["stages"]) else "—"
            out.append({"kind": "thread_stalled", "level": "watch", "title": f"{t['kind']}线索停滞：{t['title']}",
                        "detail": f"已观察来源中未见“{stage}”后的进展（最近记录 {t['last_at'][:10]}），不代表项目已停止。"
                                  + (f"预期下一步：{t['next_step']}。" if t.get("next_step") else "")})
    if documents is not None and monitored_days >= 120:
        last_doc = max((d["created_at"] for d in documents), default=None)
        idle = _days_ago(last_doc) if last_doc else monitored_days
        if idle is not None and idle >= 120:
            out.append({"kind": "materials_quiet", "level": "watch", "title": "很久没有收到经营资料",
                        "detail": f"距最近一次上传资料已 {idle} 天；仅反映本机收录间隔，不代表公司未报送。"})
    return out


# ---------------------------------------------------------------- adaptive cadence & discovery latency

def cadence(store, monitor, project_id: str, base_hours: int) -> dict:
    """How often external sources are read for this project, and why."""
    base_hours = max(MIN_HOURS, base_hours)  # a slower user setting (e.g. weekly) is respected, never sped up
    dates = _own_event_dates(store, project_id, 90)
    week = (datetime.now(UTC) - timedelta(days=14)).isoformat()
    with closing(store._connect()) as db:
        hot = db.execute("""SELECT count(*) FROM studio_events WHERE project_id=? AND status!='dismissed'
                            AND category IN ('company','trial','patent')
                            AND importance IN ('major','notable') AND COALESCE(event_at, created_at) >= ?""",
                         (project_id, week)).fetchone()[0]
    open_threads = [t for t in monitor.threads(project_id) if t["status"] == "open"
                    and (age := _days_ago(t["last_at"])) is not None and age <= 30]
    alerts = [w for w in monitor.watchpoints(project_id) if w["alert"]]
    if hot >= 2 or (open_threads and alerts):
        hours = base_hours if base_hours > MAX_HOURS else max(MIN_HOURS, base_hours // 2)
        reason = f"近 14 天有 {hot} 条重要动态" if hot >= 2 else "有进行中的事件线索和开启推送的关注点"
        return {"hours": hours, "mode": "active", "reason": reason}
    age = _days_ago((store.get_project(project_id) or {}).get("created_at")) or 0
    from project_watch.studio.health import silence_coverage

    if not dates and age >= 90 and silence_coverage(store, project_id, base_hours=base_hours):
        return {"hours": max(base_hours, min(MAX_HOURS, base_hours * 2)), "mode": "quiet", "reason": "近 90 天没有公司自身动态"}
    return {"hours": base_hours, "mode": "normal", "reason": "按设置的更新频率"}


def due_external(store, monitor, base_hours: int) -> list[str]:
    """Like store.due_external_projects, but each project's interval follows its cadence."""
    candidates = store.due_external_projects(MIN_HOURS)
    due = []
    for project_id in candidates:
        last = max((store.get_project(project_id) or {}).get("last_external_at") or "",
                   store.get_state(f"external_attempt:{project_id}") or "") or None
        hours = cadence(store, monitor, project_id, base_hours)["hours"]
        if not last or last < (datetime.now(UTC) - timedelta(hours=hours)).isoformat():
            due.append(project_id)
    return due


def latency(store, project_id: str, days: int = 30) -> dict:
    """Median hours between publication and discovery for the company's own items (published date known)."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with closing(store._connect()) as db:
        rows = db.execute("""SELECT published_at, collected_at FROM studio_items WHERE project_id=? AND category='company'
                             AND relevant=1 AND published_at IS NOT NULL AND collected_at >= ?""", (project_id, since)).fetchall()
    gaps = []
    for published, collected in rows:
        try:
            p = datetime.fromisoformat(published.replace("Z", "+00:00"))
            c = datetime.fromisoformat(collected.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        p = p if p.tzinfo else p.replace(tzinfo=UTC)
        c = c if c.tzinfo else c.replace(tzinfo=UTC)
        hours = (c - p).total_seconds() / 3600
        if 0 <= hours <= 24 * 60:  # older back-fill is not "latency"
            gaps.append(hours)
    if len(gaps) < 3:
        return {"median_hours": None, "n": len(gaps)}
    return {"median_hours": round(statistics.median(gaps), 1), "n": len(gaps)}


# ---------------------------------------------------------------- tenders & licences (search service only)

def _host_in(url: str, sites: tuple[str, ...]) -> bool:
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or parts.username or parts.password:
            return False
    except ValueError:
        return False
    return any(host == s or host.endswith("." + s) for s in sites)


def public_records(collectors, names: list[str], *, report: dict | None = None,
                   biomed: bool = False) -> dict[str, list[dict]]:
    """招投标 (government procurement portals) and 资质/许可 (regulator sites) results that name the company.

    One query per site with the shortest listed name that the longer names contain
    (a "上海X科技" query also finds "上海X科技有限公司"); the drug regulator only for
    biomedical projects. R44: 16 site searches per round, half of them redundant.
    """
    out: dict[str, list[dict]] = {"tender": [], "license": []}
    if not getattr(collectors, "has_search", False):
        return out
    names = [n for n in dict.fromkeys(n for n in names if n and len(n) >= 2)][:2]
    queried = [n for n in names if not any(o != n and o in n for o in names)][:1]
    seen: set[str] = set()
    license_sites = LICENSE_SITES if biomed else tuple(s for s in LICENSE_SITES if s != "nmpa.gov.cn")
    for key, sites, label in (("tender", TENDER_SITES, "招投标"), ("license", license_sites, "资质/许可")):
        for name in queried:
            for site in sites:
                from project_watch.studio.http import HttpError

                entry = report.setdefault(key, {"checked": 0, "added": 0, "errors": []}) if report is not None else None
                if entry is not None:
                    entry["checked"] += 1
                try:
                    rows = collectors.web_search(f"site:{site} {name}", count=6)
                except (HttpError, ValueError, KeyError) as exc:
                    if entry is not None:
                        entry["errors"].append(f"{site}: {getattr(exc, 'code', type(exc).__name__)}")
                    if isinstance(exc, HttpError) and exc.status in (401, 403, 429):
                        return out  # Do not replay access refusals or consume more quota.
                    continue
                for r in rows:
                    text = f"{r.get('title', '')} {r.get('snippet', '')}"
                    if not _host_in(r.get("url", ""), sites) or r["key"] in seen or not any(n in text for n in names):
                        continue
                    seen.add(r["key"])
                    out[key].append(dict(r, source=f"{label} · {r.get('source') or site}"))
    return out
