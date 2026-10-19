"""Deterministic checks over extracted metrics: cross-document changes and integrity issues.

Everything here is rule-based and reproducible; the model never decides whether two
numbers disagree. Every finding carries the sources (file + locator) on both sides.
"""

from __future__ import annotations

import re
from collections import defaultdict

from project_watch.studio.normalize import NAME_SYNONYMS, period_key, scope_key, sort_key

TOLERANCE = 0.01  # 1% relative difference counts as "the same number"


def _ref(m: dict) -> dict:
    return {"source_id": m["source_id"], "source_name": m.get("source_name") or "", "locator": m.get("locator", ""),
            "value": m.get("value"), "unit": m.get("unit", ""), "period": m.get("period", "")}


def _key(m: dict) -> tuple:
    return (m.get("std_name") or m["name"], m.get("period_key") or m["period"], scope_key(m.get("scope")),
            m.get("norm_unit") or "", m.get("currency") or "", m.get("category") or "financial")


def _comparable(a: dict, b: dict) -> bool:
    return (a.get("value_norm") is not None and b.get("value_norm") is not None
            and (a.get("currency") or "") == (b.get("currency") or "")
            and (a.get("norm_unit") or "") == (b.get("norm_unit") or ""))


def _recency(m: dict) -> str:
    return m.get("source_created_at") or m.get("created_at") or ""


def _differs(a: float, b: float) -> bool:
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base > TOLERANCE


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def _pct(new: float, old: float) -> str:
    if old == 0:
        return ""
    change = (new - old) / abs(old) * 100
    return f"（{'+' if change >= 0 else ''}{change:.0f}%）"


def metric_issues(metrics: list[dict]) -> list[dict]:
    """Conflicting actuals across documents and impossible relationships."""
    issues: list[dict] = []
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for m in metrics:
        if m.get("basis") == "actual":
            groups[_key(m)].append(m)
    for (name, period, scope, _unit, _currency, _category), rows in groups.items():
        by_source: dict[str, dict] = {}
        for r in rows:
            by_source.setdefault(r["source_id"], r)
        distinct = list(by_source.values())
        if len(distinct) < 2:
            continue
        base = distinct[0]
        others = [r for r in distinct[1:] if _comparable(base, r) and _differs(base["value_norm"], r["value_norm"])]
        if others:
            issues.append({
                "kind": "conflict", "severity": "high",
                "text": f"{name} {period}{('（' + scope + '）') if scope else ''}在不同资料中的实际数不一致："
                        + "；".join(f"{r.get('source_name') or '资料'} {_fmt(r['value_norm'], r['norm_unit'])}"
                                    for r in [base, *others]),
                "refs": [_ref(r) for r in [base, *others]],
            })

    def actual(name: str) -> dict[tuple, dict]:
        out = {}
        for m in sorted(metrics, key=_recency, reverse=True):
            if (m.get("std_name") or m["name"]) == name and m.get("basis") == "actual" and m.get("value_norm") is not None:
                out.setdefault((m.get("period_key") or m["period"], scope_key(m.get("scope")),
                                m.get("currency") or "", m.get("category") or "financial"), m)
        return out

    revenue, profit, gross, margin, cash = (actual(n) for n in ("营业收入", "净利润", "毛利", "毛利率", "货币资金"))
    for key, rev in revenue.items():
        if rev["value_norm"] < 0:
            issues.append({"kind": "sanity", "severity": "medium",
                           "text": f"营业收入 {key[0]} 为负数（{_fmt(rev['value_norm'], rev['norm_unit'])}），请核对原文。",
                           "refs": [_ref(rev)]})
        for label, other in (("净利润", profit.get(key)), ("毛利", gross.get(key))):
            if other and _comparable(rev, other) and rev["value_norm"] > 0 and other["value_norm"] > rev["value_norm"] * (1 + TOLERANCE):
                issues.append({"kind": "sanity", "severity": "high",
                               "text": f"{key[0]} {label}（{_fmt(other['value_norm'], other['norm_unit'])}）高于营业收入"
                                       f"（{_fmt(rev['value_norm'], rev['norm_unit'])}），可能是单位、口径或期间不一致。",
                               "refs": [_ref(other), _ref(rev)]})
    for key, m in margin.items():
        if m.get("norm_unit") == "%" and abs(m["value_norm"]) > 100:
            issues.append({"kind": "sanity", "severity": "medium",
                           "text": f"毛利率 {key[0]} 为 {_fmt(m['value_norm'], '%')}，超出合理范围。", "refs": [_ref(m)]})
    for key, m in cash.items():
        if m["value_norm"] < 0:
            issues.append({"kind": "sanity", "severity": "medium",
                           "text": f"货币资金 {key[0]} 为负数，请核对原文。", "refs": [_ref(m)]})
    return issues


def document_changes(metrics: list[dict], new_source_ids: set[str]) -> list[dict]:
    """What the newly received documents change relative to earlier ones (numbers only)."""
    new = [m for m in metrics if m["source_id"] in new_source_ids]
    old = [m for m in metrics if m["source_id"] not in new_source_ids]
    if not new or not old:
        return []
    old_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for m in old:
        old_by_key[_key(m)].append(m)
    old_names = {(_key(m)[0], *_key(m)[2:]) for m in old}
    changes: list[dict] = []
    seen: set[tuple] = set()
    for m in sorted(new, key=lambda r: (_key(r)[0], sort_key(_key(r)[1]))):
        key = _key(m)
        name, period = key[:2]
        previous = old_by_key.get(key, [])
        basis = m.get("basis")
        if not previous:
            if (name, *key[2:]) in old_names and (key, "new") not in seen:
                seen.add((key, "new"))
                changes.append({"kind": "new_period", "text": f"{name} 新增 {period} 数据："
                                f"{_fmt(m['value_norm'], m['norm_unit'])}{'（' + _basis_label(basis) + '）' if basis != 'actual' else ''}",
                                "refs": [_ref(m)]})
            continue
        for p in sorted(previous, key=_recency, reverse=True):
            pair = (key, basis, p.get("basis"))
            if pair in seen:
                continue
            seen.add(pair)
            if not _comparable(m, p) or not _differs(m["value_norm"], p["value_norm"]):
                continue
            if basis == "actual" and p.get("basis") in ("forecast", "budget"):
                changes.append({"kind": "actual_vs_plan",
                                "text": f"{name} {period} 实际 {_fmt(m['value_norm'], m['norm_unit'])}，此前{_basis_label(p.get('basis'))}"
                                        f" {_fmt(p['value_norm'], p['norm_unit'])}{_pct(m['value_norm'], p['value_norm'])}",
                                "refs": [_ref(p), _ref(m)], "direction": "up" if m["value_norm"] >= p["value_norm"] else "down"})
            elif basis in ("forecast", "budget") and p.get("basis") in ("forecast", "budget"):
                changes.append({"kind": "plan_revised",
                                "text": f"{name} {period} {_basis_label(basis)}由 {_fmt(p['value_norm'], p['norm_unit'])} 调整为"
                                        f" {_fmt(m['value_norm'], m['norm_unit'])}{_pct(m['value_norm'], p['value_norm'])}",
                                "refs": [_ref(p), _ref(m)], "direction": "up" if m["value_norm"] >= p["value_norm"] else "down"})
            elif basis == "actual" and p.get("basis") == "actual":
                changes.append({"kind": "restated",
                                "text": f"{name} {period} 实际数被改写：由 {_fmt(p['value_norm'], p['norm_unit'])} 变为"
                                        f" {_fmt(m['value_norm'], m['norm_unit'])}{_pct(m['value_norm'], p['value_norm'])}",
                                "refs": [_ref(p), _ref(m)], "direction": "up" if m["value_norm"] >= p["value_norm"] else "down"})
    order = {"actual_vs_plan": 0, "restated": 1, "plan_revised": 2, "new_period": 3}
    return sorted(changes, key=lambda c: order.get(c["kind"], 9))


def _basis_label(basis: str | None) -> str:
    return {"forecast": "预测", "budget": "预算", "actual": "实际"}.get(basis or "", "口径未明")


# ---------------------------------------------------------------- current view of the numbers

def superseded(metrics: list[dict]) -> list[dict]:
    """Forecasts/budgets for which an actual now exists: [{"plan": m, "actual": m}]."""
    actuals: dict[tuple, dict] = {}
    for m in sorted(metrics, key=_recency, reverse=True):  # the latest document's actual wins
        if m.get("basis") == "actual" and m.get("value_norm") is not None:
            actuals.setdefault(_key(m), m)
    out = []
    for m in metrics:
        actual = actuals.get(_key(m))
        if m.get("basis") in ("forecast", "budget") and actual and _comparable(m, actual):
            out.append({"plan": m, "actual": actual})
    return out


def current_lines(metrics: list[dict], limit: int = 80) -> list[str]:
    """One line per metric/period for prompts: actuals win; a replaced forecast is shown as history only."""
    replaced = {id(s["plan"]) for s in superseded(metrics)}
    plans_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for s in superseded(metrics):
        plans_by_key[_key(s["actual"])].append(s["plan"])
    lines, seen = [], set()
    ordered = sorted(sorted(metrics, key=_recency, reverse=True), key=lambda m: (_key(m)[0], sort_key(_key(m)[1])))
    for m in ordered:
        if id(m) in replaced:
            continue
        key = (*_key(m), m.get("basis"))
        if key in seen:
            continue
        seen.add(key)
        name, period, scope = _key(m)[:3]
        value = m.get("value_norm") if m.get("value_norm") is not None else m.get("value")
        unit = m.get("norm_unit") or m.get("unit") or ""
        text = f"- {name}{('（' + scope + '）') if scope else ''} {period} {_fmt(value, unit)}（{_basis_label(m.get('basis'))}）"
        if m.get("basis") == "actual" and plans_by_key.get(_key(m)):
            plan = max(plans_by_key[_key(m)], key=_recency)
            text += f"；此前{_basis_label(plan.get('basis'))} {_fmt(plan['value_norm'], plan['norm_unit'])}，已被实际数取代"
        lines.append(text)
        if len(lines) >= limit:
            break
    return lines


_FORWARD_WORDS = re.compile(r"预计|预测|预期|计划|目标|有望|将达|将实现|力争|规划|指引|guidance|expect|forecast", re.I)
_HISTORY_WORDS = re.compile(r"此前|原计划|原预测|原目标|曾预计|低于|高于|不及|超出|超过预期|未达|达成率|完成率|对比|相比|较")
_AMOUNT = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(百万美元|万美元|亿美元|万港元|港元|美元|百万元|千万元|亿元|万元|千元|元|万台|台|万人|人|家|亿|万|%|％)")


def _numbers(sentence: str, context: str) -> list[tuple[float, str]]:
    from project_watch.studio.normalize import normalize_amount

    out = []
    for raw, unit in _AMOUNT.findall(sentence):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        norm, norm_unit, _ = normalize_amount(value, unit, context)
        out.append((norm, norm_unit))
        out.append((value, unit))  # also match the figure as originally written
    return out


def _matches(numbers: list[tuple[float, str]], m: dict) -> bool:
    targets = [(m.get("value_norm"), m.get("norm_unit") or ""), (m.get("value"), m.get("unit") or "")]
    for value, unit in numbers:
        for target, target_unit in targets:
            if target is None or value is None:
                continue
            if unit and unit == target_unit and not _differs(value, target):
                return True
    return False


def stale_sentence(sentence: str, replaced: list[dict]) -> bool:
    """A sentence restating, as still current, a forecast that an actual has replaced."""
    if not _FORWARD_WORDS.search(sentence) or _HISTORY_WORDS.search(sentence):
        return False
    for s in replaced:
        plan, actual = s["plan"], s["actual"]
        name = plan.get("std_name") or plan.get("name") or ""
        aliases = [(alias, std) for std, words in NAME_SYNONYMS.items() for alias in words
                   if alias.lower() in sentence.lower()]
        mentioned = max(aliases, key=lambda pair: len(pair[0]))[1] if aliases else ""
        if mentioned != name and name not in sentence:
            continue
        scope = scope_key(plan.get("scope"))
        if (scope and scope not in sentence) or (not scope and re.search(r"归母|母公司|单体", sentence)):
            continue
        period = plan.get("period_key") or plan.get("period") or ""
        year = period[:4]
        periods = [period_key(m.group()) for m in re.finditer(
            rf"{re.escape(year)}(?:年)?(?:第?[一二三四1-4]季度|Q[1-4]|H[12]|上半年|下半年|\d{{1,2}}月)?", sentence, re.I)] if year else []
        if period not in periods:
            continue
        numbers = _numbers(sentence, f"{plan.get('std_name', '')} {sentence}")
        if _matches(numbers, plan) and not _matches(numbers, actual):
            return True
    return False


_SENTENCE_END = re.compile(r"(?<=[。；;！!？?])")


def scrub_stale(text: str, replaced: list[dict]) -> tuple[str, int]:
    """Remove stale-forecast sentences from a paragraph; returns (text, removed count)."""
    if not replaced or not text:
        return text, 0
    kept, removed = [], 0
    for sentence in _SENTENCE_END.split(text):
        if sentence.strip() and stale_sentence(sentence, replaced):
            removed += 1
        else:
            kept.append(sentence)
    return "".join(kept).strip(), removed
