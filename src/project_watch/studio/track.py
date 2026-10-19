"""赛道四格: is the market getting bigger or more crowded? Demand, capital, price, policy.

Rules over stored, cited records only — no model calls here:
* demand: cited market metrics from the industry view (numbers the model could only copy from a quoted
  source), else industry report titles that state growth or decline;
* capital: track financing events — cited industry funding rows, vendor records for tracked peers and
  report titles naming a round — de-duplicated by company and round, last 90 days vs the 90 before;
* price: industry and competitor report titles about price cuts or rises;
* policy: notable policy items, read as supportive or restrictive from their titles.

Each cell carries its direction, a headline, a quarterly series, the evidence behind it and one
rule-written line on what it means for the company. Too little data gives "信息不足", not a guess.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from project_watch.studio import race

QUARTERS = 6
_DEMAND_METRIC = re.compile(r"装车量|出货量|销量|销售额|市场规模|渗透率|需求|装机|用户数|订单量|产量")
_DEMAND_UP = re.compile(r"增长|增速|放量|翻倍|新高|大增|激增|提升|上升|突破")
_DEMAND_DOWN = re.compile(r"下滑|下降|萎缩|放缓|负增长|下跌|减少|低迷")
_PRICE_DOWN = re.compile(r"降价|价格战|价格下探|跌价|价格下降|价格下行|以价换量|杀价")
_PRICE_UP = re.compile(r"涨价|提价|价格上调|上调价格|价格上涨")
_POLICY_UP = re.compile(r"支持|鼓励|试点|补贴|扩围|扩大|放开|加快|促进|推动|规划|行动方案|指导意见|准入")
_POLICY_DOWN = re.compile(r"限制|收紧|整顿|禁止|叫停|暂停|约谈|加强监管|从严")
_PCT = re.compile(r"[-−]?\d+(?:\.\d+)?\s*%")


def _quarter(day: str) -> str:
    y, m = int(day[:4]), int(day[5:7])
    return f"{y % 100:02d}Q{(m - 1) // 3 + 1}"


def _quarters(today: date) -> list[str]:
    out, y, q = [], today.year, (today.month - 1) // 3 + 1
    for _ in range(QUARTERS):
        out.append(f"{y % 100:02d}Q{q}")
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out[::-1]


def _series(days: list[str], today: date) -> list[dict]:
    labels = _quarters(today)
    counts = dict.fromkeys(labels, 0)
    for d in days:
        if re.match(r"\d{4}-\d{2}", d or "") and _quarter(d) in counts:
            counts[_quarter(d)] += 1
    return [{"label": k, "value": v} for k, v in counts.items()]


def _ev(e: dict) -> dict:
    title, url, source = race._event_title(e)
    return {"title": e.get("summary") or title, "raw": title, "url": url, "source": source,
            "date": race._event_day(e), "grade": race.grade_of(url, source),
            "importance": e.get("importance") or "routine"}


def _evidence(rows: list[dict], n: int = 2) -> list[dict]:
    return [{k: r[k] for k in ("title", "url", "source", "date", "grade")} for r in sorted(rows, key=lambda r: r["date"], reverse=True)[:n]]


def _cell(key, label, direction, tone, value, series, evidence, for_us, sources, sub=""):
    return {"key": key, "label": label, "direction": direction, "tone": tone, "value": value, "sub": sub, "series": series,
            "evidence": evidence, "for_us": for_us, "sources": sources}


def _unknown(key, label, today):
    return _cell(key, label, "信息不足", "unknown", "—", _series([], today), [], "", 0)


def demand(industry: dict | None, reports: list[dict], today: date, company: str) -> dict:
    metrics = [m for m in (industry or {}).get("metrics") or [] if _DEMAND_METRIC.search(m.get("name") or "")
               and any(r.get('url') for r in m.get('refs') or []) and isinstance(m.get('value'), (int, float))]
    quantities = re.compile(r'装车|出货|销量|销售额|市场规模|渗透率|需求|装机|用户数|订单量|产量')
    market_reports = [r for r in reports if quantities.search(r['raw']) and not re.search(r'股价|股市|ETF|指数|突破性治疗', r['raw'])]
    up = [r for r in market_reports if _DEMAND_UP.search(r["raw"]) and not _DEMAND_DOWN.search(r["raw"])]
    down = [r for r in market_reports if _DEMAND_DOWN.search(r["raw"])]
    if metrics:
        m = metrics[0]
        growth = "增" in m["name"] or "同比" in m["name"] or "增速" in m["name"]
        value = f"{'+' if growth and m['value'] > 0 else ''}{m['value']:g}{m.get('unit') or ''}"
        rising = m["value"] > 0
        refs = m.get("refs") or []
        ev = [{"title": f"{m['name']} {m.get('period') or ''}：{value}", "url": refs[0].get("url", "") if refs else "",
               "source": refs[0].get("source", "") if refs else "", "date": refs[0].get("date", "") if refs else "",
               "grade": race.grade_of(refs[0].get("url", "") if refs else "", refs[0].get("source", "") if refs else "")}]
        direction, tone = ("变大", "good") if rising else ("变小", "bad")
        if not growth or m['value'] == 0:
            direction, tone = '方向未披露', 'unknown'
        return _cell("demand", "需求", direction, tone, value, _series([r["date"] for r in up], today),
                     ev + _evidence(up, 1), '',
                     len({r["source"] for r in up + down}) + len(refs), sub=f"{m['name']} · {m.get('period') or ''}".strip(" ·"))
    if len(up) + len(down) < 2:
        return _unknown("demand", "需求", today)
    return _cell("demand", "需求", '报道信号', 'neutral', f"{len(up)} 条增长 / {len(down)} 条下滑",
                 _series([r["date"] for r in up], today), _evidence(up + down), '',
                 len({r["source"] for r in up + down}), sub='已收录报道计数')


def capital(industry: dict | None, records: list[dict], reports: list[dict], today: date) -> dict:
    deals: dict[tuple[str, str], dict] = {}
    for f in (industry or {}).get("funding") or []:
        if f.get("company") and f.get("date") and f.get('round') and any(r.get('url') for r in f.get('refs') or []):
            refs = f.get("refs") or [{}]
            deals.setdefault((race._core(f["company"]), (f.get("round") or "").replace(" ", "")), {
                "title": f"{f['company']} {f.get('round') or ''} {f.get('amount') or ''}".strip(), "url": refs[0].get("url", ""),
                "source": refs[0].get("source", ""), "date": race._day(f["date"]), "grade": "媒体"})
    for r in records:
        if r.get("kind") == "funding" and r.get("company") and r.get("date") and r.get('round') and not r.get('stale'):
            deals[(race._core(r["company"]), (r.get("round") or "").replace(" ", ""))] = {
                "title": f"{r['company']} {r.get('round') or ''} {r.get('amount') or ''}".strip(), "url": "",
                "source": {"xiniu": "烯牛", "pharmcube": "医药魔方", "wind": "Wind"}.get(r.get("provider"), "数据商"),
                "date": race._day(r["date"]), "grade": "数据商"}
    for e in reports:
        found = race._funding_from_title(e["raw"])
        if not found:
            continue
        company = re.split(r"完成|获|宣布|再获|拿下", e["raw"])[0][:20]
        key = (race._core(company), found["round"])
        deals.setdefault(key, {"title": e["title"], "url": e["url"], "source": e["source"], "date": e["date"], "grade": e["grade"]})
    rows = list(deals.values())
    recent = [d for d in rows if race._within(d["date"], today, 90)]
    before = [d for d in rows if not race._within(d["date"], today, 90) and race._within(d["date"], today, 180)]
    if len(recent) + len(before) < 3:
        cell = _unknown("capital", "资本", today)
        cell["series"] = _series([d["date"] for d in rows], today)
        if rows:
            cell["value"], cell["evidence"] = f"近 180 天 {len(recent) + len(before)} 起", _evidence(rows)
            cell["sub"] = "事件太少，暂不判断方向"
        return cell
    if len(recent) >= max(2, len(before) * 1.5):
        direction, tone, note = "升温", "good", "赛道融资活跃，下一轮估值环境有利；竞对也在补充资金。"
    elif len(recent) <= len(before) * 0.6:
        direction, tone, note = "降温", "bad", "赛道融资放缓，下一轮可能需要更长时间。"
    else:
        direction, tone, note = "平稳", "neutral", "赛道融资节奏与此前持平。"
    return _cell("capital", "资本", direction, tone, f"近 90 天 {len(recent)} 起",
                 _series([d["date"] for d in rows], today), _evidence(recent or rows), note,
                 len({d["source"] for d in rows if d["source"]}), sub=f"此前 90 天 {len(before)} 起")


def price(reports: list[dict], today: date, company: str) -> dict:
    down = [r for r in reports if _PRICE_DOWN.search(r["raw"])]
    up = [r for r in reports if _PRICE_UP.search(r["raw"]) and not _PRICE_DOWN.search(r["raw"])]
    if not down and not up:
        return _unknown("price", "价格", today)
    winning = down if len(down) > len(up) else up
    matcher = _PRICE_DOWN if len(down) > len(up) else _PRICE_UP
    pct = next((m.group(0).replace(' ', '') for r in sorted(winning, key=lambda r: r['date'], reverse=True)
                for hit in matcher.finditer(r['raw']) for m in [_PCT.search(r['raw'][hit.start():hit.end()+12])] if m), '')
    if len(down) > len(up):
        return _cell("price", "价格", "下行", "bad", f"降价 {pct}" if pct else f"{len(down)} 条降价报道",
                     _series([r["date"] for r in down], today), _evidence(down),
                     f"逆风：价格下行会压缩{company}的毛利。", len({r["source"] for r in down}), sub=f"{len(down)} 条降价报道")
    if len(up) > len(down):
        return _cell("price", "价格", "上行", "good", f"涨价 {pct}" if pct else f"{len(up)} 条涨价报道",
                     _series([r["date"] for r in up], today), _evidence(up), f"顺风：价格上行改善{company}的毛利空间。",
                     len({r["source"] for r in up}))
    return _cell("price", "价格", "方向不一", "neutral", f"{len(down)} 降 / {len(up)} 涨", _series([r["date"] for r in down + up], today),
                 _evidence(down + up), "价格信号方向不一。", len({r["source"] for r in down + up}))


def policy(reports: list[dict], today: date) -> dict:
    reports = [r for r in reports if not re.search(r'ETF|股价|资金流向|指数.{0,6}(?:涨|跌)', r['raw'])]
    notable = [r for r in reports if r["importance"] in ("major", "notable")] or reports
    up = [r for r in notable if _POLICY_UP.search(r["raw"]) and not _POLICY_DOWN.search(r["raw"])]
    down = [r for r in notable if _POLICY_DOWN.search(r["raw"])]
    if not notable:
        return _unknown("policy", "政策", today)
    latest = max(notable, key=lambda r: r["date"])
    if len(up) > len(down):
        direction, tone, note = "顺风", "good", "政策面支持，相关需求和准入有增量。"
    elif len(down) > len(up):
        direction, tone, note = "逆风", "bad", "监管收紧，留意合规成本和准入节奏。"
    else:
        direction, tone, note = "中性", "neutral", "近期政策以常规发布为主。"
    return _cell("policy", "政策", direction, tone, f"{len(notable)} 条重要政策", _series([r["date"] for r in notable], today),
                 _evidence(notable), note, len({r["source"] for r in notable}), sub=f"最近：{latest['date']}")


def build(*, project: dict, industry: dict | None, industry_events: list[dict], competitor_events: list[dict],
          policy_events: list[dict], commercial_records: list[dict], today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    company = project.get("short_name") or project.get("name") or "公司"
    since = (today - timedelta(days=QUARTERS * 92)).isoformat()
    ind = [r for r in map(_ev, industry_events) if since <= r["date"] <= today.isoformat()]
    comp = [r for r in map(_ev, competitor_events) if since <= r["date"] <= today.isoformat()]
    pol = [r for r in map(_ev, policy_events) if since <= r["date"] <= today.isoformat()]
    cells = [demand(industry, ind, today, company), capital(industry, commercial_records, ind + comp, today),
             price(ind + comp, today, company), policy(pol, today)]
    known = [c for c in cells if c["tone"] != "unknown"]
    good, bad = sum(c["tone"] == "good" for c in known), sum(c["tone"] == "bad" for c in known)
    summary = "、".join(f"{c['label']}{c['direction']}" for c in known)
    return {"industry": (industry or {}).get("industry") or project.get("industry") or "", "cells": cells,
            "summary": summary, "balance": "偏顺风" if good > bad else "偏逆风" if bad > good else "方向不一",
            "quarters": _quarters(today), "generated_at": datetime.now(UTC).isoformat()}
