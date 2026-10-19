"""Deterministic financial view: statement layout, derived ratios, key numbers and runway.

Nothing here asks the model. Every derived number names the inputs it was computed from,
so the page can show "毛利率 = 毛利 ÷ 营业收入" with both source cells one click away.
"""

from __future__ import annotations

import re

from project_watch.studio.normalize import scope_key, sort_key

# Statement layout following the Chinese Accounting Standards (企业会计准则) presentation.
# Items are std names from normalize.NAME_SYNONYMS (or the CAS wording itself); HK/IFRS lines without a
# CAS equivalent keep their own wording ("借款（流动）") instead of being forced into a similar CAS line.
# Flags: T = subtotal/total line, K = key line shown in the condensed view, "" = detail line.
_INCOME = (
    ("", (("营业总收入", "营业总收入", "T"), ("营业收入", "营业收入", "TK"), ("营业总成本", "营业总成本", ""),
          ("营业成本", "营业成本", "K"), ("毛利", "毛利", "TK"), ("税金及附加", "税金及附加", ""))),
    ("期间费用", (("销售费用", "销售费用", "K"), ("管理费用", "管理费用", "K"), ("研发投入", "研发费用", "K"),
              ("财务费用", "财务费用", "K"), ("利息费用", "其中：利息费用", ""), ("利息收入", "利息收入", ""))),
    ("其他损益", (("其他收益", "其他收益／其他收入", ""), ("投资收益", "投资收益", ""),
              ("公允价值变动收益", "公允价值变动收益", ""), ("信用减值损失", "信用减值损失", ""),
              ("资产减值损失", "资产减值损失", ""), ("无形资产减值亏损", "其中：无形资产减值", ""),
              ("资产处置收益", "资产处置收益", ""), ("其他利得及损失", "其他利得及损失", ""))),
    ("", (("营业利润", "营业利润", "TK"), ("营业外收入", "营业外收入", ""), ("营业外支出", "营业外支出", ""),
          ("利润总额", "利润总额", "TK"), ("所得税费用", "所得税费用", ""), ("净利润", "净利润", "TK"),
          ("少数股东损益", "少数股东损益", ""), ("扣非净利润", "扣除非经常性损益后的净利润", "K"),
          ("经调整净利润（非IFRS）", "经调整净利润（非IFRS）", "K"))),
    ("每股收益", (("基本每股收益", "基本每股收益", ""), ("稀释每股收益", "稀释每股收益", ""), ("每股收益", "每股收益", ""))),
)
_BALANCE = (
    ("流动资产", (("货币资金", "货币资金", "K"), ("交易性金融资产", "交易性金融资产", ""), ("应收票据", "应收票据", ""),
              ("应收账款", "应收账款", "K"), ("应收款项融资", "应收款项融资", ""), ("预付款项", "预付款项", ""),
              ("其他应收款", "其他应收款", ""), ("存货", "存货", "K"), ("合同资产", "合同资产", ""),
              ("一年内到期的非流动资产", "一年内到期的非流动资产", ""), ("其他流动资产", "其他流动资产", ""),
              ("流动资产合计", "流动资产合计", "TK"))),
    ("非流动资产", (("长期股权投资", "长期股权投资", ""), ("其他非流动金融资产", "其他非流动金融资产", ""),
               ("投资性房地产", "投资性房地产", ""), ("固定资产", "固定资产", "K"), ("在建工程", "在建工程", ""),
               ("使用权资产", "使用权资产", ""), ("无形资产", "无形资产", "K"), ("开发支出", "开发支出", ""),
               ("商誉", "商誉", "K"), ("长期待摊费用", "长期待摊费用", ""), ("递延所得税资产", "递延所得税资产", ""),
               ("其他非流动资产", "其他非流动资产", ""), ("非流动资产合计", "非流动资产合计", "TK"))),
    ("", (("总资产", "资产总计", "TK"),)),
    ("流动负债", (("短期借款", "短期借款", "K"), ("借款（流动）", "借款（流动部分）", "K"), ("应付票据", "应付票据", ""),
              ("应付账款", "应付账款", "K"), ("应付账款及其他应付款项", "应付账款及其他应付款项", "K"),
              ("预收款项", "预收款项", ""), ("合同负债", "合同负债", ""), ("应付职工薪酬", "应付职工薪酬", ""),
              ("应交税费", "应交税费", ""), ("其他应付款", "其他应付款", ""),
              ("一年内到期的非流动负债", "一年内到期的非流动负债", ""), ("其他流动负债", "其他流动负债", ""),
              ("流动负债合计", "流动负债合计", "TK"))),
    ("非流动负债", (("长期借款", "长期借款", "K"), ("借款（非流动）", "借款（非流动部分）", "K"), ("应付债券", "应付债券", ""),
               ("租赁负债", "租赁负债", ""), ("长期应付款", "长期应付款", ""), ("递延收益", "递延收益", ""),
               ("递延所得税负债", "递延所得税负债", ""), ("其他非流动负债", "其他非流动负债", ""),
               ("非流动负债合计", "非流动负债合计", "TK"))),
    ("", (("总负债", "负债合计", "TK"),)),
    ("所有者权益", (("实收资本", "实收资本（或股本）", ""), ("资本公积", "资本公积", ""), ("其他综合收益", "其他综合收益", ""),
               ("盈余公积", "盈余公积", ""), ("未分配利润", "未分配利润", ""),
               ("归属于母公司所有者权益合计", "归属于母公司所有者权益合计", "K"), ("少数股东权益", "少数股东权益", ""),
               ("净资产", "所有者权益合计", "TK"))),
)
_CASHFLOW = (
    ("经营活动", (("销售商品、提供劳务收到的现金", "销售商品、提供劳务收到的现金", ""),
              ("经营活动现金流入小计", "经营活动现金流入小计", ""),
              ("购买商品、接受劳务支付的现金", "购买商品、接受劳务支付的现金", ""),
              ("支付给职工以及为职工支付的现金", "支付给职工以及为职工支付的现金", ""),
              ("支付的各项税费", "支付的各项税费", ""), ("经营活动现金流出小计", "经营活动现金流出小计", ""),
              ("经营活动现金流净额", "经营活动产生的现金流量净额", "TK"))),
    ("投资活动", (("收回投资收到的现金", "收回投资收到的现金", ""),
              ("购建长期资产支付的现金", "购建固定资产、无形资产和其他长期资产支付的现金", "K"),
              ("投资支付的现金", "投资支付的现金", ""), ("投资活动现金流净额", "投资活动产生的现金流量净额", "TK"))),
    ("筹资活动", (("吸收投资收到的现金", "吸收投资收到的现金", ""), ("取得借款收到的现金", "取得借款收到的现金", ""),
              ("偿还债务支付的现金", "偿还债务支付的现金", ""),
              ("分配股利、利润或偿付利息支付的现金", "分配股利、利润或偿付利息支付的现金", ""),
              ("筹资活动现金流净额", "筹资活动产生的现金流量净额", "TK"))),
    ("", (("现金及现金等价物净增加额", "现金及现金等价物净增加额", "TK"),)),
)
LAYOUT = (("income", "利润表", _INCOME), ("balance", "资产负债表", _BALANCE), ("cashflow", "现金流量表", _CASHFLOW))
STATEMENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
    (key, title, tuple(name for _g, items in groups for name, _label, _flags in items)) for key, title, groups in LAYOUT)
# Per-share lines are not amounts in 万元; they are laid out but excluded from amount calculations.
_PER_SHARE_LINES = {"基本每股收益", "稀释每股收益", "每股收益"}
SECTION_OF = {name: key for key, _title, names in STATEMENTS for name in names}
RATIO_NAMES = {"毛利率", "净利率", "收入同比增长"}
REPORTED_RATIOS = {"流动比率", "速动比率", "总负债对总资产比率", "资产负债率"}


def section_of(metric: dict) -> str:
    """income / balance / cashflow / ratio / financial_other / operating."""
    name = metric.get("std_name") or metric.get("name") or ""
    if metric.get("category") == "operating":
        return "operating"
    if name in RATIO_NAMES or name in REPORTED_RATIOS or (metric.get("norm_unit") or metric.get("unit")) == "%":
        return "ratio"
    return SECTION_OF.get(name, "financial_other")


def _recency(m: dict) -> str:
    return m.get("source_created_at") or m.get("created_at") or ""


def _agrees(a: dict, b: dict) -> bool:
    x, y = float(a["value_norm"]), float(b["value_norm"])
    return a.get("norm_unit") == b.get("norm_unit") and abs(x - y) <= max(abs(x), abs(y), 1e-9) * 0.005


def current_values(metrics: list[dict]) -> dict[tuple[str, str], dict]:
    """(std_name, period_key) → the value to show: latest actual, else latest plan. Default scope only.

    One report often cites a total on several pages and a note or parent-company figure under the
    same label once. Among equally recent candidates the value most pages agree on wins.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for m in metrics:
        name = m.get("std_name") or m["name"]
        if (m.get("value_norm") is None or scope_key(m.get("scope"))
                or m.get("currency") not in (None, "", "CNY")
                or (name in SECTION_OF and name not in _PER_SHARE_LINES and m.get("norm_unit") != "万元")):
            continue
        groups.setdefault((name, m.get("period_key") or m["period"]), []).append(m)
    chosen: dict[tuple[str, str], dict] = {}
    for key, rows in groups.items():
        actual = [m for m in rows if m.get("basis") == "actual"]
        pool = actual or rows
        newest = max(_recency(m) for m in pool)
        latest = [m for m in pool if _recency(m) == newest]
        chosen[key] = max(latest, key=lambda m: sum(_agrees(m, o) for o in latest))
    return chosen


def _shape(period: str) -> str:
    """'' for a full year, 'H1', 'Q3', 'M06', '-1-9M' … — only like is compared with like."""
    return period[4:] if period[:4].isdigit() else period


def _months(period: str) -> int | None:
    shape = _shape(period)
    if shape == "":
        return 12
    if shape in ("H1", "H2"):
        return 6
    if re.fullmatch(r"Q[1-4]", shape):
        return 3
    if shape.startswith("-1-") and shape.endswith("M"):
        try:
            return int(shape[3:-1])
        except ValueError:
            return None
    return None  # a single month-end (M06) is a point in time, not a flow period


def _period_end(period: str) -> tuple[int, int] | None:
    if not re.match(r"^\d{4}", period):
        return None
    shape = _shape(period)
    if shape in ("", "H2"):
        month = 12
    elif shape == "H1":
        month = 6
    elif re.fullmatch(r"Q[1-4]", shape):
        month = int(shape[1]) * 3
    elif re.fullmatch(r"M(?:0[1-9]|1[0-2])", shape):
        month = int(shape[1:])
    elif re.fullmatch(r"-1-(?:[1-9]|1[0-2])M", shape):
        month = int(shape[3:-1])
    else:
        return None
    return int(period[:4]), month


def _ref(m: dict) -> dict:
    return {"source_id": m["source_id"], "source_name": m.get("source_name") or "", "locator": m.get("locator", "")}


def _pct(a: float, b: float) -> float | None:
    return None if not b else round(a / b * 100, 1)


def ratios(metrics: list[dict]) -> list[dict]:
    """Derived ratios per period. Each cell: value (%), basis, formula, refs."""
    cur = current_values(metrics)
    periods = sorted({p for (_n, p) in cur}, key=sort_key)

    def get(name: str, period: str) -> dict | None:
        m = cur.get((name, period))
        return m if m and m.get("norm_unit") == "万元" else None

    def basis(*ms: dict) -> str:
        kinds = {m.get("basis") for m in ms}
        return "unknown" if "unknown" in kinds or None in kinds else "actual" if kinds == {"actual"} else "forecast"

    rows: dict[str, dict] = {}

    def put(row: str, period: str, value: float | None, formula: str, *ms: dict) -> None:
        if value is None:
            return
        rows.setdefault(row, {"name": row, "unit": "%", "cells": {}})["cells"][period] = {
            "value": value, "basis": basis(*ms), "formula": formula, "refs": [_ref(m) for m in ms]}

    for p in periods:
        rev = get("营业收入", p)
        if not rev or rev["value_norm"] <= 0:
            continue
        gross = get("毛利", p)
        cost = get("营业成本", p)
        if gross:
            put("毛利率", p, _pct(gross["value_norm"], rev["value_norm"]), "毛利 ÷ 营业收入", gross, rev)
        elif cost:
            put("毛利率", p, _pct(rev["value_norm"] - cost["value_norm"], rev["value_norm"]),
                "（营业收入 − 营业成本）÷ 营业收入", rev, cost)
        if (net := get("净利润", p)):
            put("净利率", p, _pct(net["value_norm"], rev["value_norm"]), "净利润 ÷ 营业收入", net, rev)
        if (rd := get("研发投入", p)):
            put("研发投入占收入", p, _pct(rd["value_norm"], rev["value_norm"]), "研发投入 ÷ 营业收入", rd, rev)
        expenses = [m for m in (get(n, p) for n in ("销售费用", "管理费用", "研发投入", "财务费用")) if m]
        if expenses:
            title = "期间费用率" if len(expenses) == 4 else "已披露费用占收入（不完整）"
            put(title, p, _pct(sum(m["value_norm"] for m in expenses), rev["value_norm"]),
                "（" + " + ".join(m.get("std_name") or m["name"] for m in expenses) + "）÷ 营业收入", *expenses, rev)
        # Growth against the same shape one year earlier (2025 vs 2024, 2025H1 vs 2024H1).
        if p[:4].isdigit():
            prior = get("营业收入", f"{int(p[:4]) - 1}{_shape(p)}")
            if prior and prior["value_norm"] > 0:
                put("收入同比增长", p, round((rev["value_norm"] / prior["value_norm"] - 1) * 100, 1),
                    "本期营业收入 ÷ 上年同期 − 1", rev, prior)
    # Leverage at each balance date: both inputs must be reported for the same point in time.
    for p in sorted({p for (n, p) in cur if n == "总资产"}, key=sort_key):
        assets, debt = get("总资产", p), get("总负债", p)
        if assets and debt and assets["value_norm"] > 0:
            put("资产负债率", p, _pct(debt["value_norm"], assets["value_norm"]), "总负债 ÷ 总资产", debt, assets)
    # Actual source-disclosed ratios preserve their precision instead of being
    # replaced by a calculation from rounded totals. Plans/unknown inputs cannot
    # overwrite an existing derived ratio.
    for (name, p), m in cur.items():
        held = rows.get(name, {}).get("cells", {}).get(p)
        if (name in RATIO_NAMES and m.get("norm_unit") == "%"
                and (held is None or m.get("basis") == "actual")):
            put(name, p, m["value_norm"], "资料直接给出", m)
    order = ["收入同比增长", "毛利率", "净利率", "研发投入占收入", "期间费用率", "已披露费用占收入（不完整）", "资产负债率"]
    return [rows[n] for n in order if n in rows]


def _months_between(a: tuple[int, int], b: tuple[int, int]) -> int:
    return (b[0] - a[0]) * 12 + b[1] - a[1]


def _burn_candidates(cur: dict, cash_m: dict, cash_end: tuple[int, int]) -> list[dict]:
    """Monthly cash consumption measured three ways, most direct first. Positive burn = cash going out."""
    found: list[dict] = []
    # 1. Change of the cash balance itself, between the latest point and an earlier one 3-12 months before.
    earlier = [(m, end) for (n, p), m in cur.items()
               if n == "货币资金" and m.get("basis") == "actual" and m.get("norm_unit") == "万元"
               and (end := _period_end(p)) and 3 <= _months_between(end, cash_end) <= 12]
    if earlier:
        prev, prev_end = max(earlier, key=lambda c: c[1])
        months = _months_between(prev_end, cash_end)
        change = cash_m["value_norm"] - prev["value_norm"]
        found.append({"method": "cash_change", "label": "现金余额变化",
                      "basis_text": f"{prev.get('period_key') or prev['period']} → {cash_m.get('period_key') or cash_m['period']}"
                                    f" 现金 {prev['value_norm']:,.0f} → {cash_m['value_norm']:,.0f} 万元，{months} 个月",
                      "monthly": change / months, "refs": [_ref(prev)]})
    # 2. Operating cash flow; 3. net profit as the last, loosest proxy.
    for name, method, label in (("经营活动现金流净额", "operating_cash_flow", "经营活动现金流"),
                                ("净利润", "net_profit", "净利润（以亏损近似消耗）")):
        rows = [(m, _months(p)) for (n, p), m in cur.items()
                if n == name and m.get("basis") == "actual" and m.get("norm_unit") == "万元"]
        rows = [(m, months) for m, months in rows
                if months and (end := _period_end(m.get("period_key") or m["period"])) and end <= cash_end]
        if rows:
            m, months = max(rows, key=lambda c: sort_key(c[0].get("period_key") or c[0]["period"]))
            period = m.get("period_key") or m["period"]
            found.append({"method": method, "label": label,
                          "basis_text": f"{period} {label} {m['value_norm']:,.0f} 万元 ÷ {months} 个月",
                          "monthly": m["value_norm"] / months, "refs": [_ref(m)]})
    return found


def runway(metrics: list[dict]) -> dict | None:
    """Months of cash at the latest burn rate (actuals only). None when it cannot be computed honestly.

    Cash-balance change and operating cash flow are measured burn; when both exist the larger
    burn is used (conservative). Net profit is only a fallback when neither cash measure exists.
    """
    cur = current_values(metrics)
    cash = [m for (n, _p), m in cur.items() if n == "货币资金" and m.get("basis") == "actual" and m.get("norm_unit") == "万元"]
    cash = [m for m in cash if _period_end(m.get("period_key") or m["period"])]
    if not cash:
        return None
    cash_m = max(cash, key=lambda m: (_period_end(m.get("period_key") or m["period"]), _recency(m)))
    cash_end = _period_end(cash_m.get("period_key") or cash_m["period"])
    base = {"cash": cash_m["value_norm"], "cash_period": cash_m.get("period_key") or cash_m["period"],
            "unit": "万元", "refs": [_ref(cash_m)], "alternatives": []}
    if cash_m["value_norm"] < 0:
        return {**base, "months": None, "status": "unknown", "text": "账面现金为负，须先核对原文及科目口径"}
    candidates = _burn_candidates(cur, cash_m, cash_end)
    if not candidates:
        return {**base, "months": None, "status": "unknown", "text": "缺少现金流或利润数据，无法估算现金跑道"}
    measured = [c for c in candidates if c["method"] != "net_profit"]
    burning = [c for c in measured if c["monthly"] < 0]
    for c in candidates:
        base["alternatives"].append({"method": c["method"], "label": c["label"], "text": c["basis_text"],
                                     "burn": round(-c["monthly"], 1) if c["monthly"] < 0 else 0,
                                     "months": round(cash_m["value_norm"] / -c["monthly"], 1) if c["monthly"] < 0 else None,
                                     "refs": c["refs"]})
    if burning:
        pick = max(burning, key=lambda c: -c["monthly"])
    elif measured:
        pick = next((c for c in measured if c["method"] == "operating_cash_flow"), measured[0])
        base["refs"] += pick["refs"]
        if pick["method"] == "cash_change":
            return {**base, "months": None, "status": "unknown", "method": pick["method"],
                    "text": f"{pick['basis_text']}，现金未减少；可能含融资流入，无法据此估算消耗"}
        return {**base, "months": None, "status": "positive", "burn": round(pick["monthly"], 1), "method": pick["method"],
                "text": f"{pick['basis_text']}，经营现金流非负；不代表投资、筹资等其他活动不消耗现金"}
    else:
        pick = candidates[0]
        if pick["monthly"] >= 0:
            return {**base, "months": None, "status": "unknown", "method": pick["method"],
                    "text": "净利润非亏损，但缺少经营现金流，无法估算现金消耗"}
    base["refs"] += pick["refs"]
    burn = -pick["monthly"]
    left = round(cash_m["value_norm"] / burn, 1)
    return {**base, "months": left, "status": "short" if left < 12 else "ok", "burn": round(burn, 1),
            "method": pick["method"], "text": f"账面现金 ÷ 月均消耗（{pick['basis_text']}）"}


def key_numbers(metrics: list[dict]) -> dict:
    """Latest actual revenue (+ like-for-like growth), net profit, cash and runway."""
    cur = current_values(metrics)

    def latest(name: str) -> dict | None:
        rows = [m for (n, _p), m in cur.items() if n == name and m.get("basis") == "actual" and m.get("norm_unit") == "万元"]
        rows = [m for m in rows if _period_end(m.get("period_key") or m["period"])]
        if name != "货币资金":
            rows = [m for m in rows if _months(m.get("period_key") or m["period"])]
        return max(rows, key=lambda m: (_period_end(m.get("period_key") or m["period"]), _recency(m))) if rows else None

    def cell(m: dict | None) -> dict | None:
        if not m:
            return None
        return {"value": m["value_norm"], "unit": m.get("norm_unit") or "", "period": m.get("period_key") or m["period"],
                "refs": [_ref(m)]}

    out = {"revenue": cell(latest("营业收入")), "net_profit": cell(latest("净利润")),
           "cash": cell(latest("货币资金")), "runway": runway(metrics), "revenue_growth": None}
    rev = latest("营业收入")
    if rev:
        p = rev.get("period_key") or rev["period"]
        prior = cur.get(("营业收入", f"{int(p[:4]) - 1}{_shape(p)}")) if p[:4].isdigit() else None
        if prior and prior.get("basis") == "actual" and prior["value_norm"] > 0 and prior.get("norm_unit") == rev.get("norm_unit"):
            out["revenue_growth"] = round((rev["value_norm"] / prior["value_norm"] - 1) * 100, 1)
        reported = cur.get(("收入同比增长", p))
        if reported and reported.get("basis") == "actual" and reported.get("norm_unit") == "%":
            out["revenue_growth"] = reported["value_norm"]
    return out


def layout() -> list[dict]:
    return [{"key": key, "title": title, "groups": [
        {"title": group, "items": [{"name": n, "label": label, "total": "T" in flags, "key": "K" in flags}
                                   for n, label, flags in items]}
        for group, items in groups]} for key, title, groups in LAYOUT]


def view(metrics: list[dict]) -> dict:
    return {"sections": {m["id"]: section_of(m) for m in metrics if m.get("id")},
            "ratios": ratios(metrics), "key_numbers": key_numbers(metrics), "layout": layout()}
