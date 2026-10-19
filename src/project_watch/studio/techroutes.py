"""技术路线与新面孔: which technical route is gaining, and who is new in the track. Rules only.

* Route terms are suggested by the system from stored text (the industry view's technology facts,
  report and paper titles): measured units ("905nm"), technical acronyms ("MEMS", "FMCW") and
  "<X>路线/方案/技术" phrases, scored higher where they appear in route comparisons. The user may
  replace them; the user's list is kept until changed.
* Counts are mentions per quarter in collected papers, patents and reports — a signal of attention
  and activity, not market share.
* New faces: paper institutions and financed companies first seen in the last 90 days.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from project_watch.studio import race, track

MAX_ROUTES = 4
STATE_KEY = "tech_routes:{}"
NEW_DAYS = 90

_UNIT = re.compile(r"(?<![\dA-Za-z.])\d+(?:\.\d+)?\s?(?:nm|μm|um|GHz|MHz|kV|kWh|Wh|mAh|英寸)(?![A-Za-z])")
_ACRONYM = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9]{1,7}(?:-[A-Z0-9]{1,4})?(?![A-Za-z0-9])")
_PHRASE = re.compile(r"([一-龥A-Za-z0-9]{2,5})(?:路线|技术路线|方案|架构|工艺)")
_COMPARE = re.compile(r"路线|之争|对比|vs|VS|技术路径|替代|并存|取代")
_STOP = {"AI", "CEO", "CTO", "CFO", "COO", "IPO", "SOP", "ASIL", "OEM", "ODM", "PE", "VC", "GDP", "PPT", "APP", "OK",
         "Tier", "Tier1", "Tier2", "Gen", "Gen2", "Gen3", "Pro", "Max", "Plus", "Mini", "China", "The", "And",
         "Inc", "Ltd", "Co", "LLC", "BP", "ToB", "ToC", "SaaS", "ARR", "NDA", "BLA", "IND", "FDA", "NMPA", "CDE",
         "Q1", "Q2", "Q3", "Q4", "H1", "H2", "A", "B", "C", "D", "E", "L2", "L3", "L4", "L5", "PCT"}
_PHRASE_STOP = {"解决", "整体", "一体化", "技术", "产品", "发布", "推出", "新一代", "公司", "合作", "行业", "国产", "核心", "全新", "创新"}
_TECHNICAL = {'FMCW', 'MEMS', 'SPAD', 'OPA', 'TOF', 'dToF', 'iToF', 'SiPM', 'VCSEL', 'LiDAR',
              'RAG', 'LLM', 'MoE', 'Agent', 'MCP', 'OCR', 'NPU', 'GPU', 'ASIC', 'FPGA', 'SoC',
              'GaN', 'SiC', 'EUV', 'DUV', 'CMOS', 'FinFET', 'GAA', 'ADC', 'CAR-T', 'TCR-T',
              'mRNA', 'RNAi', 'PROTAC', 'AAV', 'CRISPR', 'PD-1', 'PD-L1'}


def _texts(e: dict) -> str:
    title, _url, _source = race._event_title(e)
    return " ".join(x for x in (title, e.get("summary") or "", e.get("snippet") or "") if x)


def _terms(text: str) -> set[str]:
    found = {m.group(0).replace(" ", "") for m in _UNIT.finditer(text)}
    # Capitalisation alone also matches brands, product models and paper acronyms.
    found |= {t for t in _TECHNICAL if re.search(r'(?<![A-Za-z0-9])' + re.escape(t) + r'(?![A-Za-z0-9])', text)}
    found |= {m.group(1) for m in _PHRASE.finditer(text) if m.group(1) not in _PHRASE_STOP
              and (re.search(r'[一-龥]', m.group(1)) or m.group(1) in _TECHNICAL)}
    return found


def suggest(*, industry: dict | None, docs: list[dict], self_texts: list[str], exclude: list[str]) -> list[dict]:
    """Up to MAX_ROUTES route terms with the evidence count behind each, the company's own first."""
    score: Counter = Counter()
    seen_in: Counter = Counter()
    for fact in (industry or {}).get("facts") or []:
        if "技术" in (fact.get("kind") or ""):
            for t in _terms(fact.get("text") or ""):
                score[t] += 3
    for d in docs:
        text = _texts(d)
        terms = _terms(text)
        for t in terms:
            seen_in[t] += 1
            score[t] += 2 if _COMPARE.search(text) else 1
    excluded = {race._core(x).lower() for x in exclude if x} | {x.lower() for x in exclude if x}
    ranked = [t for t, s in score.most_common() if s >= 3 and seen_in[t] >= 1 and t.lower() not in excluded
              and not any(t.lower() in x or x in t.lower() for x in excluded if len(x) >= 2)]
    chosen = ranked[:MAX_ROUTES]
    mine = Counter(t for text in self_texts for t in chosen if t.lower() in text.lower())
    own = mine.most_common(1)[0][0] if mine else None
    out = [{"term": t, "own": t == own, "evidence": seen_in[t]} for t in chosen]
    return sorted(out, key=lambda r: not r["own"])


def series(routes: list[dict], docs: list[dict], today: date) -> list[dict]:
    rows = []
    for r in routes:
        term = r["term"].lower()
        hits = [d for d in docs if term in _texts(d).lower()]
        kinds = Counter({"paper": "论文", "patent": "专利"}.get(d.get("category"), "报道") for d in hits)
        rows.append({"term": r["term"], "own": r.get("own", False),
                     "values": [x["value"] for x in track._series([race._day(d.get("event_at") or d.get("published_at")
                                                                             or d.get("created_at") or d.get("collected_at")) for d in hits], today)],
                     "kinds": dict(kinds), "total": len(hits)})
    return rows


def insight(rows: list[dict]) -> str:
    """Describe collected mention counts only, never technical merit or consensus."""
    if len(rows) < 2 or sum(r["total"] for r in rows) < 4:
        return ""
    early = [sum(r["values"][:3]) for r in rows]
    late = [sum(r["values"][3:]) for r in rows]
    if not sum(early) or not sum(late):
        return ""
    shares = [(r, e / sum(early), lt / sum(late)) for r, e, lt in zip(rows, early, late, strict=True)]
    riser = max(shares, key=lambda x: x[2] - x[1])
    own = next((r for r in rows if r["own"]), None)
    parts = []
    if riser[2] - riser[1] >= 0.1:
        parts.append(f"{riser[0]['term']} 的提及占比从前三个季度的 {riser[1]:.0%} 升到近三个季度的 {riser[2]:.0%}")
    if own:
        # The running quarter is partial: compare the last complete one unless the current is already comparable.
        idx = -2  # always compare complete quarters; volume cannot make a partial quarter comparable
        ahead = [r for r in rows if r is not own and r["values"][idx] > own["values"][idx]
                 and all(a <= b for a, b in zip(r["values"][:idx], own["values"][:idx], strict=True))]
        if ahead:
            parts.append(f"上个完整季度，{'、'.join(r['term'] for r in ahead)} 的提及次数首次高于 {own['term']}")
    return "已收录材料中，" + "；".join(parts) + "。" if parts else ""


def entrants(*, papers: list[dict], deals_docs: list[dict], known: list[str], today: date,
             patents: list[dict] | None = None) -> list[dict]:
    """Institutions and financed companies whose first appearance is within NEW_DAYS."""
    cut = (today - timedelta(days=NEW_DAYS)).isoformat()
    known_l = {race._core(k).lower() for k in known if k}
    first: dict[str, dict] = {}
    for p in papers:
        day = race._day(p.get("published_at") or p.get("collected_at"))
        for inst in (p.get("extra") or {}).get("institutions") or []:
            row = first.setdefault(inst, {"name": inst, "kind": "论文机构", "first": day, "count": 0, "title": p.get("title") or "", "url": p.get("url") or ""})
            row["first"] = min(row["first"], day)
            row["count"] += 1
    for p in patents or []:
        day = race._day(p.get('published_at') or p.get('collected_at'))
        for name in (p.get('extra') or {}).get('applicants') or []:
            row = first.setdefault(name, {'name': name, 'kind': '专利申请人', 'first': day, 'count': 0,
                'title': p.get('title') or '', 'url': p.get('url') or ''})
            row['first'] = min(row['first'], day)
            row['count'] += 1
    for e in deals_docs:
        title, url, _source = race._event_title(e)
        found = race._funding_from_title(title)
        if not found:
            continue
        name = re.split(r"完成|获得|获|宣布|再获|拿下", title)[0].strip()
        name = re.sub(r"^.*?(厂商|企业|公司|初创)", "", name)[:16]
        if len(name) < 2:
            continue
        day = race._day(e.get("event_at") or e.get("created_at"))
        row = first.setdefault(name, {"name": name, "kind": "赛道融资", "first": day, "count": 0, "title": title, "url": url})
        row["first"] = min(row["first"], day)
        row["count"] += 1
        row["what"] = f"{found['round']}{(' ' + found['amount']) if found['amount'] else ''}"
    out = []
    for row in first.values():
        if not row['first'] or not cut <= row['first'] <= today.isoformat() or race._core(row["name"]).lower() in known_l:
            continue
        what = (f"首次出现，{row['count']} 篇相关论文" if row["kind"] == "论文机构"
                else f"首次出现，{row['count']} 条专利申请人摘录" if row['kind'] == '专利申请人'
                else f"首次出现在赛道融资：{row.get('what', '')}")
        out.append({**row, "what": what})
    return sorted(out, key=lambda r: r["first"], reverse=True)[:6]


def load(store, project_id: str) -> dict | None:
    raw = store.get_state(STATE_KEY.format(project_id))
    return json.loads(raw) if raw else None


def save(store, project_id: str, terms: list[str], own: str | None) -> dict:
    clean = []
    for t in terms:
        t = " ".join(str(t).split())[:20]
        if t and t.lower() not in {c.lower() for c in clean}:
            clean.append(t)
    clean = clean[:MAX_ROUTES]
    data = {"routes": [{"term": t, "own": t == own} for t in clean], "updated_at": datetime.now(UTC).isoformat()}
    store.set_state(STATE_KEY.format(project_id), json.dumps(data, ensure_ascii=False))
    return data


def view(*, project: dict, saved: dict | None, industry: dict | None, docs: list[dict], self_texts: list[str],
         papers: list[dict], peers: list[str], today: date | None = None, patents: list[dict] | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    exclude = [project.get("name"), project.get("short_name"), project.get("legal_name"), *peers]
    suggested = suggest(industry=industry, docs=docs, self_texts=self_texts, exclude=exclude)
    routes = saved["routes"] if saved and saved.get("routes") else suggested
    rows = series(routes, docs, today)
    deals_docs = [d for d in docs if d.get("category") in ("industry", "competitor")]
    return {"routes": routes, "source": "user" if saved and saved.get("routes") else "auto", "suggested": suggested,
            "quarters": track._quarters(today), "rows": rows, "insight": insight(rows),
            "entrants": entrants(papers=papers, deals_docs=deals_docs, patents=patents,
                                 known=[*(x for x in exclude if x)], today=today),
            "generated_at": datetime.now(UTC).isoformat()}
