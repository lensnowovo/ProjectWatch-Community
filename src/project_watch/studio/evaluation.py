"""Model-quality evaluation on synthetic, hand-labelled cases.

Runs the same prompts the pipeline uses (extraction, triage, same-event) and scores the
answers deterministically. The scoring is unit-tested offline; a real-key run is done by
``scripts/studio_eval.py``. Cases live in ``tests/fixtures/studio_eval/cases.json``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from project_watch.studio import pipeline as pl
from project_watch.studio.llm import ModelError
from project_watch.studio.normalize import (
    normalize_amount,
    normalize_metric,
    period_key,
    standard_name,
)

DEFAULT_CASES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "studio_eval" / "cases.json"
TOLERANCE = 0.01


def load_cases(path: Path | str | None = None) -> dict:
    # The evaluation dataset belongs to the source repository, not the wheel.
    candidate = Path(path) if path else DEFAULT_CASES
    if not path and not candidate.exists():
        candidate = Path.cwd() / "tests/fixtures/studio_eval/cases.json"
    return json.loads(candidate.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- extraction

def _gold_metric(g: dict) -> dict:
    norm, unit, _ = normalize_amount(float(g["value"]), g.get("unit", ""), g["name"] + " 收入")
    if standard_name(g["name"]) == "毛利率" and unit != "%" and abs(norm) <= 1:
        norm, unit = norm * 100, "%"
    return {**g, "std_name": standard_name(g["name"]), "value_norm": norm, "norm_unit": unit,
            "period_keys": [period_key(p) for p in g["periods"]]}


def _name_matches(gold: dict, metric: dict) -> bool:
    if metric["std_name"] == gold["std_name"]:
        return True
    names = [gold["name"], *gold.get("aliases", [])]
    return any(n and (n in metric["name"] or metric["name"] in n) for n in names if len(n) >= 2)


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= TOLERANCE * max(abs(a), abs(b), 1e-9)


def _scale_error(a: float | None, b: float | None) -> bool:
    """Off by a power of ten: the classic unit mistake (万 vs 亿, 元 vs 万元)."""
    if not a or not b or (a > 0) != (b > 0):
        return False
    ratio = abs(a / b)
    return any(_close(ratio, 10.0 ** k) for k in (-8, -4, -3, -2, -1, 1, 2, 3, 4, 8))


def score_extraction(case: dict, extracted: list[dict]) -> dict:
    gold = [_gold_metric(g) for g in case["gold"]]
    metrics = [normalize_metric({"name": str(m.get("name") or ""), "period": str(m.get("period") or ""),
                                 "value": pl._float(m.get("value")), "unit": str(m.get("unit") or ""),
                                 "basis": m.get("basis") or "unknown", "quote": str(m.get("quote") or "")})
               for m in extracted if isinstance(m, dict) and pl._float(m.get("value")) is not None]
    used: set[int] = set()
    found, errors = 0, []
    for g in gold:
        candidates = [i for i, m in enumerate(metrics) if i not in used and _name_matches(g, m)
                      and m["period_key"] in g["period_keys"]]
        exact = next((i for i in candidates if _close(metrics[i]["value_norm"], g["value_norm"])
                      and metrics[i]["norm_unit"] == g["norm_unit"]), None)
        if exact is not None:
            used.add(exact)
            found += 1
            if metrics[exact]["basis"] != g["basis"]:
                errors.append({"kind": "basis", "gold": f"{g['name']} {g['periods'][0]} {g['basis']}",
                               "got": metrics[exact]["basis"]})
            continue
        near = next((i for i in candidates if _scale_error(metrics[i]["value_norm"], g["value_norm"])), None)
        if near is not None:
            used.add(near)
            errors.append({"kind": "unit", "gold": f"{g['name']} {g['periods'][0]} {g['value']}{g['unit']}",
                           "got": f"{metrics[near]['value']}{metrics[near]['unit']}"})
        else:
            errors.append({"kind": "missing", "gold": f"{g['name']} {g['periods'][0]} {g['value']}{g['unit']}"})
    basis_ok = found - sum(1 for e in errors if e["kind"] == "basis")
    return {"id": case["id"], "gold": len(gold), "found": found, "basis_ok": basis_ok,
            "extracted": len(metrics), "unmatched_extracted": len(metrics) - len(used), "errors": errors}


def extraction_prompt(case: dict) -> str:
    return "\n".join(f"[F{n}] ({case['document']} · line {n}) {line}" for n, line in enumerate(case["lines"], 1))


# ---------------------------------------------------------------- triage

def triage_prompt(company: dict, items: list[dict]) -> str:
    context = (f"被监控公司：{company['legal_name']}（简称 {company['short_name']}；别名 {'、'.join(company['aliases'])}）\n"
               f"主营：{company['description']}\n行业：{company['industry']}\n竞品：{'、'.join(company['competitors'])}")
    listing = "\n".join(f"id={i['id']} | 来源类别={i['category']} |  | {i['source']}\n标题：{i['title']}\n摘要：{i['snippet']}"
                        for i in items)
    return f"{context}\n\n待判断信息：\n{listing}"


def apply_rules(cases: dict, results: list[dict]) -> list[dict]:
    """Apply the same post-model rules the pipeline applies, so the score reflects shipped behaviour."""
    items = {i["id"]: i for i in cases["triage"]}
    names = pl.company_names(cases["company"])
    return [pl.guard_filing(items[r["id"]], r, names) if isinstance(r, dict) and r.get("id") in items else r
            for r in results]


def score_triage(items: list[dict], results: list[dict]) -> dict:
    by_id = {r["id"]: r for r in results if isinstance(r, dict) and isinstance(r.get("id"), str)
             and type(r.get("relevant")) is bool}
    rows = []
    for item in items:
        g, r = item["gold"], by_id.get(item["id"])
        if r is None:
            rows.append({"id": item["id"], "answered": False, "relevance": False, "category": None, "importance": None})
            continue
        relevant = r["relevant"]
        row = {"id": item["id"], "answered": True, "relevance": relevant == g["relevant"],
               "category": None, "importance": None, "got": {k: r.get(k) for k in ("relevant", "category", "importance")}}
        if g["relevant"] and relevant:
            row["category"] = (r.get("category") or item["category"]) == g["category"]
            row["importance"] = r.get("importance") in g["importance"]
        rows.append(row)
    rel = [r for r in rows if r["answered"]]
    relevant_count = sum(i["gold"]["relevant"] for i in items)
    missed_major = [i["id"] for i in items if i["gold"].get("importance") == ["major"]
                    and not ((by_id.get(i["id"]) or {}).get("relevant", False)
                             and (by_id.get(i["id"]) or {}).get("importance") == "major")]
    noise = [i["id"] for i in items if not i["gold"]["relevant"] and (by_id.get(i["id"]) or {}).get("relevant", False)]
    return {"items": len(items), "answered": len(rel),
            "relevance_accuracy": _ratio(sum(r["relevance"] for r in rows), len(items)),
            "category_accuracy": _ratio(sum(r["category"] is True for r in rows), relevant_count),
            "importance_accuracy": _ratio(sum(r["importance"] is True for r in rows), relevant_count),
            "missed_major": missed_major, "noise_kept": noise,
            "wrong": [r for r in rows if not r["relevance"] or r["category"] is False or r["importance"] is False]}


# ---------------------------------------------------------------- same event

def same_event_prompt(pairs: list[dict]) -> str:
    return "\n\n".join(f"[{n}] A：{p['a']}\n    B：{p['b']}" for n, p in enumerate(pairs, 1))


def score_same_event(pairs: list[dict], results: list[dict]) -> dict:
    answers = {r["pair"]: r["same"] for r in results if isinstance(r, dict)
               and type(r.get("pair")) is int and type(r.get("same")) is bool}
    rows = [{"id": p["id"], "gold": p["same"], "got": answers.get(n)} for n, p in enumerate(pairs, 1)]
    return {"pairs": len(pairs), "accuracy": _ratio(sum(r["got"] == r["gold"] for r in rows), len(rows)),
            "false_merges": [r["id"] for r in rows if r["got"] and not r["gold"]],
            "missed_merges": [r["id"] for r in rows if r["gold"] and not r["got"]]}


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


# ---------------------------------------------------------------- runner

def _rows(data, key: str) -> list:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ValueError(f"expected a {key} array")
    return data[key]


def run(llm, cases: dict | None = None, *, repeat: int = 1) -> dict:
    """Run every suite ``repeat`` times against ``llm`` (anything with ``.json(system, user, max_tokens=)``)."""
    cases = cases or load_cases()
    report: dict = {"extraction": [], "triage": [], "same_event": [], "errors": [], "seconds": 0.0}
    started = time.monotonic()
    for round_no in range(repeat):
        for case in cases["extraction"]:
            try:
                data = llm.json(pl._EXTRACT_SYSTEM, extraction_prompt(case), max_tokens=4000)
                report["extraction"].append({**score_extraction(case, _rows(data, "metrics")), "round": round_no})
            except (ModelError, ValueError, AttributeError, TypeError) as exc:
                report["errors"].append(f"extraction {case['id']}: {type(exc).__name__}")
                report["extraction"].append({**score_extraction(case, []), "round": round_no})
        try:
            data = llm.json(pl._TRIAGE_SYSTEM, triage_prompt(cases["company"], cases["triage"]), max_tokens=4000)
            report["triage"].append({**score_triage(cases["triage"], apply_rules(cases, _rows(data, "results"))),
                                     "round": round_no})
        except (ModelError, ValueError, AttributeError, TypeError) as exc:
            report["errors"].append(f"triage: {type(exc).__name__}")
            report["triage"].append({**score_triage(cases["triage"], []), "round": round_no})
        try:
            data = llm.json(pl._SAME_EVENT_SYSTEM, same_event_prompt(cases["same_event"]), max_tokens=1500)
            report["same_event"].append({**score_same_event(cases["same_event"], _rows(data, "results")),
                                         "round": round_no})
        except (ModelError, ValueError, AttributeError, TypeError) as exc:
            report["errors"].append(f"same_event: {type(exc).__name__}")
            report["same_event"].append({**score_same_event(cases["same_event"], []), "round": round_no})
    report["seconds"] = round(time.monotonic() - started, 1)
    report["summary"] = summarise(report)
    return report


def summarise(report: dict) -> dict:
    ex = report["extraction"]
    gold = sum(r["gold"] for r in ex)
    errors = [e for r in ex for e in r["errors"]]

    def mean(rows, key):
        values = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(values) / len(values), 3) if values else None

    return {
        "extraction_recall": _ratio(sum(r["found"] for r in ex), gold),
        "extraction_basis_accuracy": _ratio(sum(r["basis_ok"] for r in ex), sum(r["found"] for r in ex)),
        "extraction_unit_errors": sum(1 for e in errors if e["kind"] == "unit"),
        "extraction_unmatched": sum(r["unmatched_extracted"] for r in ex),
        "triage_relevance_accuracy": mean(report["triage"], "relevance_accuracy"),
        "triage_category_accuracy": mean(report["triage"], "category_accuracy"),
        "triage_importance_accuracy": mean(report["triage"], "importance_accuracy"),
        "triage_missed_major": sorted({i for r in report["triage"] for i in r["missed_major"]}),
        "same_event_accuracy": mean(report["same_event"], "accuracy"),
        "same_event_false_merges": sorted({i for r in report["same_event"] for i in r["false_merges"]}),
        "failed_calls": len(report["errors"]),
    }


def markdown(report: dict, *, model: str = "") -> str:
    s = report["summary"]

    def pct(v):
        return "—" if v is None else f"{v * 100:.0f}%"

    lines = [f"# Studio 模型评测{f'（{model}）' if model else ''}", "",
             "| 项目 | 结果 |", "| --- | --- |",
             f"| 指标抽取召回 | {pct(s['extraction_recall'])} |",
             f"| 实际/预测/预算判断正确 | {pct(s['extraction_basis_accuracy'])} |",
             f"| 单位数量级错误 | {s['extraction_unit_errors']} |",
             f"| 多抽出的指标（不一定是错） | {s['extraction_unmatched']} |",
             f"| 筛选：相关性 | {pct(s['triage_relevance_accuracy'])} |",
             f"| 筛选：类别 | {pct(s['triage_category_accuracy'])} |",
             f"| 筛选：重要性 | {pct(s['triage_importance_accuracy'])} |",
             f"| 漏掉的重大信息 | {'、'.join(s['triage_missed_major']) or '无'} |",
             f"| 同一事件判断 | {pct(s['same_event_accuracy'])} |",
             f"| 错误合并 | {'、'.join(s['same_event_false_merges']) or '无'} |",
             f"| 失败调用 | {s['failed_calls']} |", "", f"耗时 {report['seconds']} 秒。", ""]
    details = [e for r in report["extraction"] for e in r["errors"]]
    if details:
        lines += ["## 抽取问题", *[f"- {e['kind']}：{e['gold']}" + (f" → {e['got']}" if e.get("got") else "") for e in details], ""]
    wrong = [w for r in report["triage"] for w in r["wrong"]]
    if wrong:
        lines += ["## 筛选问题", *[f"- {w['id']}：{json.dumps(w.get('got'), ensure_ascii=False)}" for w in wrong], ""]
    return "\n".join(lines)
