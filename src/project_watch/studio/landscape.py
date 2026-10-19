"""Competition and industry views, regenerated weekly.

Evidence is gathered first (stored events plus, when a search service is configured, a few
targeted searches) and numbered E1…En. The model may only cite those numbers; anything it
says without a valid citation is dropped. Legacy peer suggestions are pending candidates,
not verified competitive relationships. News topics do not establish event attribution.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from project_watch.studio import source_digest
from project_watch.studio.source_digest import _COMPETITION_SYSTEM as _COMPETITION_SYSTEM
from project_watch.studio.source_digest import _QUOTE_RULE as _QUOTE_RULE
from project_watch.studio.source_digest import DIMENSIONS as DIMENSIONS
from project_watch.studio.source_digest import MOVE_TYPES as MOVE_TYPES
from project_watch.studio.source_digest import Evidence as Evidence
from project_watch.studio.source_digest import _cells as _cells
from project_watch.studio.source_digest import _clean as _clean
from project_watch.studio.source_digest import _core as _core
from project_watch.studio.source_digest import _date as _date
from project_watch.studio.source_digest import _numbers as _numbers
from project_watch.studio.source_digest import _supported as _supported
from project_watch.studio.source_digest import cited_rows
from project_watch.studio.source_digest import classify_move as classify_move
from project_watch.studio.source_digest import competition as competition
from project_watch.studio.source_digest import competitor_moves as competitor_moves
from project_watch.studio.source_digest import complete_competitors as complete_competitors
from project_watch.studio.source_digest import gather as gather

_log = logging.getLogger(__name__)

THREAT = ("high", "medium", "low")
VERDICTS = ("supported", "questioned", "challenged", "unknown")


_INDUSTRY_SYSTEM = """你是投资机构的行业研究员，为投资人整理一家被投公司所在赛道的行业环境。
只能使用给出的“证据”（编号 E1…），每条结论在 refs 中引用编号；证据不足的项留空或给空列表，不要用常识编造数字。
输出 JSON：
{"industry":"赛道名称","heat":"升温|中性|降温","heat_reason":"一句话，依据融资、政策、需求、价格等证据",
 "overview":"3-4 句行业概况",
 "facts":[{"kind":"规模与增速|需求驱动|政策|价格趋势|技术路线|产业链","text":"一句话","refs":["E1"]}],
 "metrics":[{"name":"可跟踪的行业指标，如装车量/出货量/市场规模/单价","period":"2025 或 2025Q3","value":0,"unit":"万台/亿元/%","refs":["E4"]}],
 "funding":[{"company":"","round":"","amount":"原文金额，未知留空","date":"","refs":["E6"]}],
 "implications":[{"text":"这些行业变化对我方公司意味着什么","refs":["E2"]}]}
facts 最多 8 条，metrics 只收录证据中明确出现的数字，funding 只收录本赛道一级市场融资事件。"""

_INDUSTRY_SYSTEM += _QUOTE_RULE


def industry(llm, evidence: Evidence, project: dict, profile: dict) -> dict:
    user = (f"被投公司：{project.get('legal_name') or project.get('name')}\n主营：{profile.get('one_liner') or project.get('description') or '—'}\n"
            f"行业：{project.get('industry') or '—'}\n\n证据：\n" + (evidence.listing(("industry", "competitor")) or "（无）"))
    data = llm.json(_INDUSTRY_SYSTEM, user, max_tokens=8000)

    def cited(rows, fields: tuple[str, ...], limit: int) -> list[dict]:
        return cited_rows(rows, fields, limit, evidence)

    heat = {"平稳": "中性", "不确定": "中性"}.get(data.get("heat"), data.get("heat"))
    heat = heat if heat in ("升温", "中性", "降温") else "中性"
    heat_refs = evidence.resolve(data.get("heat_refs"))
    overview_refs = evidence.resolve(data.get("overview_refs"))
    reason = str(data.get("heat_reason") or "")[:200]
    overview = str(data.get("overview") or "")[:600]
    return {"industry": str(project.get("industry") or data.get("industry") or "")[:40],
            "heat": heat if _supported(reason, heat_refs) else "中性",
            "heat_reason": reason if _supported(reason, heat_refs) else "", "heat_refs": heat_refs,
            "overview": overview if _supported(overview, overview_refs) else "", "overview_refs": overview_refs,
            "facts": cited(data.get("facts"), ("kind", "text"), 8),
            "metrics": [m for m in cited(data.get("metrics"), ("name", "period", "value", "unit"), 10)
                        if isinstance(m.get("value"), (int, float))],
            "funding": cited(data.get("funding"), ("company", "round", "amount", "date"), 12),
            "implications": cited(data.get("implications"), ("text",), 4)}


# ---------------------------------------------------------------- industry heat, from counts not adjectives

HEAT_WINDOW, HEAT_BASELINE = 30, 90  # recent days vs. the 90 days before them


def _window_counts(store, project_id: str, categories: tuple[str, ...], *, notable_only: bool = False) -> tuple[int, float]:
    """Events in the last 30 days, and the average per 30 days over the 90 days before that."""
    from contextlib import closing
    from datetime import timedelta

    now = datetime.now(UTC)
    recent_from = (now - timedelta(days=HEAT_WINDOW)).isoformat()
    base_from = (now - timedelta(days=HEAT_WINDOW + HEAT_BASELINE)).isoformat()
    marks = ",".join("?" for _ in categories)
    extra = " AND importance IN ('major','notable')" if notable_only else ""
    with closing(store._connect()) as db:
        rows = db.execute(f"""SELECT COALESCE(event_at, created_at) FROM studio_events WHERE project_id=? AND status!='dismissed'
                              AND category IN ({marks}){extra} AND COALESCE(event_at, created_at) >= ?""",
                          (project_id, *categories, base_from)).fetchall()
    dates = [r[0] for r in rows]
    recent = sum(1 for d in dates if d >= recent_from)
    return recent, round((len(dates) - recent) / (HEAT_BASELINE / HEAT_WINDOW), 1)


def _trend(recent: float, baseline: float) -> str:
    if recent + baseline < 2:
        return "few"
    if baseline == 0 and recent < 5:
        return "few"  # nothing to compare with yet (e.g. monitoring started recently)
    if recent >= max(2, baseline * 1.5):
        return "up"
    if recent <= baseline * 0.6:
        return "down"
    return "flat"


def heat_signals(store, project_id: str, funding: list[dict], moves: list[dict]) -> dict:
    """Industry heat from four countable signals. The label is a rule over these counts; the
    model's narrative is shown separately and does not set the label."""
    from datetime import timedelta

    since = (datetime.now(UTC) - timedelta(days=HEAT_WINDOW + HEAT_BASELINE)).date().isoformat()
    recent_cut = (datetime.now(UTC) - timedelta(days=HEAT_WINDOW)).date().isoformat()
    news = _window_counts(store, project_id, ("industry",))
    policy = _window_counts(store, project_id, ("policy",), notable_only=True)
    rivals = _window_counts(store, project_id, ("competitor",))
    deals = [d for d in (f.get("date") or "" for f in funding) if d >= since]
    deals += [m["date"] for m in moves if m.get("type") == "融资" and (m.get("date") or "") >= since]
    deal_recent = sum(1 for d in deals if d >= recent_cut)
    deal_base = round((len(deals) - deal_recent) / (HEAT_BASELINE / HEAT_WINDOW), 1)
    signals = [
        {"key": "news", "name": "行业资讯量", "recent": news[0], "baseline": news[1], "trend": _trend(*news)},
        {"key": "funding", "name": "融资事件", "recent": deal_recent, "baseline": deal_base, "trend": _trend(deal_recent, deal_base)},
        {"key": "rivals", "name": "相关公司报道", "recent": rivals[0], "baseline": rivals[1], "trend": _trend(*rivals)},
        {"key": "policy", "name": "重要政策", "recent": policy[0], "baseline": policy[1], "trend": _trend(*policy)},
    ]
    counted = [x for x in signals if x["trend"] != "few"]
    ups = sum(1 for x in counted if x["trend"] == "up")
    downs = sum(1 for x in counted if x["trend"] == "down")
    total = sum(x["recent"] + x["baseline"] * 3 for x in signals)
    if total < 6 or not counted:
        label, note = "中性", "近四个月收集到的行业信息较少，暂按中性处理"
    elif ups >= 2 and ups > downs:
        label, note = "升温", f"{ups} 项指标明显高于此前水平"
    elif downs >= 2 and downs > ups:
        label, note = "降温", f"{downs} 项指标明显低于此前水平"
    else:
        label, note = "中性", "各项指标与此前大致持平或方向不一"
    return {"label": label, "note": note, "signals": signals, "window_days": HEAT_WINDOW, "baseline_days": HEAT_BASELINE}


def build(store, llm, collectors, project_id: str, stage: Callable[[str], None]) -> dict:
    return source_digest.build(store, llm, collectors, project_id, stage,
                               industry_builder=industry, heat_builder=heat_signals,
                               company_builder=competition, gatherer=gather)
