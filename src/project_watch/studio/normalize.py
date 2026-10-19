"""Deterministic normalisation of extracted metrics (never delegated to the model).

Original value, unit and period are always kept; normalised columns are added so
charts and cross-document comparisons line up:
- amounts in RMB are converted to 万元 (``value_norm`` / ``norm_unit``); other
  currencies keep their own scale in 万 of that currency and record ``currency``;
- periods become canonical keys: ``2025``, ``2025H1``, ``2025Q3``, ``2025M06``,
  ``2025-1-6M`` (cumulative months) …;
- metric names map to a standard name where a synonym is known.
"""

from __future__ import annotations

import re
from decimal import Decimal


def normal(value):
    """Whitespace/case normalization only; no industry synonyms or fuzzy matching."""
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


# Multiplier to 万 (10k) of the currency.
_SCALE_TO_WAN = {
    "元": 1e-4, "千元": 0.1, "万元": 1.0, "十万元": 10.0, "百万元": 100.0, "千万元": 1000.0,
    "亿元": 1e4, "万亿元": 1e8,
}
_CURRENCY_WORDS = (
    ("港元", "HKD"), ("港币", "HKD"), ("hkd", "HKD"), ("hk$", "HKD"),
    ("人民币", "CNY"), ("rmb", "CNY"), ("cny", "CNY"), ("美元", "USD"), ("usd", "USD"), ("us$", "USD"),
    ("$", "USD"), ("港元", "HKD"), ("港币", "HKD"), ("hkd", "HKD"), ("欧元", "EUR"), ("eur", "EUR"),
    ("日元", "JPY"), ("jpy", "JPY"),
)
_EN_SCALE = {"thousand": 0.1, "k": 0.1, "million": 100.0, "mn": 100.0, "m": 100.0,
             "billion": 1e5, "bn": 1e5, "b": 1e5}
_CN_SCALE_ONLY = {"千": 0.1, "万": 1.0, "十万": 10.0, "百万": 100.0, "千万": 1000.0, "亿": 1e4}

NAME_SYNONYMS = {
    "营业收入": ("营业收入", "营业总收入", "营收", "收入", "主营业务收入", "销售收入", "revenue", "total revenue", "sales", "收益", "收入總額"),
    "营业成本": ("营业成本", "主营业务成本", "销售成本", "cost of revenue", "cogs", "销售及服务成本", "銷售成本"),
    "毛利": ("毛利", "毛利润", "gross profit"),
    "毛利率": ("毛利率", "gross margin"),
    "净利润": ("净利润", "净利", "归母净利润", "净亏损", "净虧損", "期内亏损", "期內虧損",
            "年内亏损", "年內虧損", "年度亏损", "本期亏损", "归母净亏损", "歸母淨虧損",
            "net income", "net profit", "net loss", "loss for the year", "loss for the period"),
    "扣非净利润": ("扣非净利润", "扣除非经常性损益后的净利润"),
    "经营活动现金流净额": ("经营活动现金流净额", "经营活动产生的现金流量净额", "经营性现金流", "经营现金流",
                     "经营活动所用现金净额", "经营活动所得现金净额", "经营活动（所用）所得现金净额",
                     "经营活动所得（所用）现金净额", "经营活动现金净额", "經營活動所用現金淨額", "經營活動所得現金淨額",
                     "operating cash flow", "net cash used in operating activities",
                     "net cash from operating activities"),
    "货币资金": ("货币资金", "期末货币资金", "账面现金", "现金及现金等价物", "现金余额", "期末现金及现金等价物",
             "银行结余及现金", "銀行結餘及現金", "现金及银行结余", "cash", "cash and cash equivalents",
             "bank balances and cash"),
    "研发投入": ("研发投入", "研发费用", "研发支出", "r&d", "r&d expense", "研发开支", "研究及开发开支", "研發開支"),
    "总资产": ("总资产", "资产总计", "资产总值", "资产总额", "資產總值", "total assets"),
    "净资产": ("净资产", "所有者权益合计", "股东权益", "net assets", "equity", "权益总额", "权益合计", "总权益", "權益總額"),
    "销售费用": ("销售费用", "销售及市场费用", "selling expense", "sales and marketing", "销售开支", "销售及分销开支", "分销及销售开支", "销售及市场推广开支", "銷售開支"),
    "管理费用": ("管理费用", "管理及行政费用", "g&a", "administrative expense", "一般及行政开支", "行政开支", "管理开支", "一般及行政開支"),
    "财务费用": ("财务费用", "finance cost", "financial expense", "财务成本", "财务成本净额", "融资成本", "財務成本"),
    "营业利润": ("营业利润", "经营利润", "营业亏损", "经营亏损", "經營虧損", "營業虧損",
             "operating profit", "operating income", "operating loss"),
    "每股收益": ("每股收益", "每股亏损", "每股虧損", "earnings per share", "loss per share", "eps"),
    "基本每股收益": ("基本每股收益", "基本每股亏损", "基本每股虧損", "basic earnings per share", "basic loss per share"),
    "稀释每股收益": ("稀释每股收益", "稀释每股亏损", "稀釋每股虧損", "diluted earnings per share", "diluted loss per share"),
    "收入同比增长": ("收入同比增长", "收入同比增长率", "营业收入同比增长", "营业收入同比增长率", "营收同比增长率"),
    "净利率": ("净利率", "净利润率", "net margin"),
    "应收账款": ("应收账款", "应收款", "accounts receivable", "贸易应收款项", "应收账款及票据"),
    "存货": ("存货", "库存", "inventory"),
    "总负债": ("总负债", "负债合计", "负债总计", "负债总额", "負債總額", "total liabilities"),
    "投资活动现金流净额": ("投资活动现金流净额", "投资活动产生的现金流量净额", "投资现金流", "investing cash flow", "投资活动所用现金净额", "投资活动所得现金净额", "投资活动（所用）所得现金净额", "投資活動所用現金淨額", "net cash used in investing activities"),
    "筹资活动现金流净额": ("筹资活动现金流净额", "筹资活动产生的现金流量净额", "筹资现金流", "financing cash flow", "融资活动所得现金净额", "融资活动所用现金净额", "筹资活动所得现金净额", "筹资活动所用现金净额", "融资活动（所用）所得现金净额", "融資活動所得現金淨額", "net cash from financing activities"),
    "员工人数": ("员工人数", "员工数", "员工总数", "人数", "headcount", "employees"),
    "其他收益": ("其他收益", "其他收入", "其他收入合计", "其他收益合计"),
    "其他利得及损失": ("其他利得及损失", "其他收益及亏损", "其他收益及亏损净额", "其他收益及亏损，净额", "其他利得及亏损"),
    "投资收益": ("投资收益", "投资收入"),
    "资产减值损失": ("资产减值损失", "减值亏损", "资产减值亏损", "资产减值损失（损失以“－”号填列）"),
    "利润总额": ("利润总额", "税前利润", "除税前利润", "除税前溢利", "税前溢利", "税前亏损", "除税前亏损", "除稅前虧損", "profit before tax", "loss before tax"),
    "所得税费用": ("所得税费用", "所得税开支", "所得税", "所得稅開支", "income tax expense"),
    "经调整净利润（非IFRS）": ("经调整净利润（非IFRS）", "经调整亏损(非IFRS)", "经调整亏损（非IFRS）", "年内经调整亏损（非IFRS）", "期内经调整亏损（非IFRS）", "经调整净亏损（非IFRS）", "经调整净利润(非IFRS)"),
    "流动资产合计": ("流动资产合计", "流动资产总值", "流动资产总额", "流動資產總值"),
    "固定资产": ("固定资产", "物业、厂房及设备", "物业厂房及设备", "物业、厂房及设备账面值", "物業、廠房及設備"),
    "使用权资产": ("使用权资产", "使用权资产账面值", "使用權資產"),
    "无形资产": ("无形资产", "无形资产账面值", "無形資產"),
    "非流动资产合计": ("非流动资产合计", "非流动资产总值", "非流动资产总额"),
    "合同负债": ("合同负债", "合约负债", "合約負債"),
    "租赁负债": ("租赁负债", "租赁负债期末余额", "租賃負債"),
    "流动负债合计": ("流动负债合计", "流动负债总额", "流动负债总值", "流動負債總額"),
    "非流动负债合计": ("非流动负债合计", "非流动负债总额", "非流动负债总值"),
    "现金及现金等价物净增加额": ("现金及现金等价物净增加额", "现金及现金等价物净减少额", "现金及现金等价物（减少）增加净额", "现金及现金等价物增加（减少）净额", "现金流出净额", "现金流入净额", "现金及现金等价物净变动"),
    "税金及附加": ("税金及附加", "营业税金及附加"),
    "营业总成本": ("营业总成本", "营业总成本合计"),
    "信用减值损失": ("信用减值损失", "信用减值损失（损失以“－”号填列）"),
    "公允价值变动收益": ("公允价值变动收益", "公允价值变动损益", "公允价值变动收益（损失以“－”号填列）"),
    "资产处置收益": ("资产处置收益", "资产处置损益"),
    "少数股东损益": ("少数股东损益", "少数股东应占亏损", "非控股权益应占亏损"),
    "实收资本": ("实收资本", "股本", "实收资本（或股本）"),
    "应交税费": ("应交税费", "应交税金"),
    "应付职工薪酬": ("应付职工薪酬", "应付薪酬"),
    "购建长期资产支付的现金": ("购建长期资产支付的现金", "购建固定资产、无形资产和其他长期资产支付的现金", "资本开支", "资本性支出", "购买物业、厂房及设备", "购置物业、厂房及设备付款", "capital expenditure"),
    "偿还债务支付的现金": ("偿还债务支付的现金", "偿还借款", "偿还银行借款"),
    "取得借款收到的现金": ("取得借款收到的现金", "新增银行贷款", "新增借款", "借款所得款项"),
    "归属于母公司所有者权益合计": ("归属于母公司所有者权益合计", "归属于母公司股东权益合计", "归属于母公司所有者权益"),
}
_SYNONYM_INDEX = {alias.lower(): std for std, aliases in NAME_SYNONYMS.items() for alias in aliases}
PERCENT_NAMES = {"毛利率", "净利率", "收入同比增长"}
_NET_FLOW_NAMES = {"经营活动现金流净额", "投资活动现金流净额", "筹资活动现金流净额", "现金及现金等价物净增加额"}
_PER_SHARE_NAMES = {"每股收益", "基本每股收益", "稀释每股收益"}
# Exact metric labels only: loss mentions elsewhere in the passage must not flip
# revenue, reduction amounts, percentages or another company's profit.
_LOSS_LABELS = {
    alias for name in {"净利润", "营业利润", "利润总额", "经调整净利润（非IFRS）", *_PER_SHARE_NAMES}
    for alias in NAME_SYNONYMS[name]
    if "亏损" in alias or "虧損" in alias or "loss" in alias
}


def _name_key(name: str) -> str:
    # CAS statements prefix lines with "其中：", "减：", "加：" — the line is the same item without it.
    text = re.sub(r"^\s*(?:其中|减|加|減)\s*[:：]\s*", "", name or "")
    return re.sub(r"[\s（）()]", "", text.lower())


_LOSS_KEYS = frozenset(_name_key(label) for label in _LOSS_LABELS)


def standard_name(name: str) -> str:
    key = _name_key(name)
    for alias, std in _SYNONYM_INDEX.items():
        if key == re.sub(r"[\s（）()]", "", alias):
            return std
    return re.sub(r"^\s*(?:其中|减|加|減)\s*[:：]\s*", "", name or "").strip()


def detect_currency(*texts: str) -> str:
    # Explicit unit currency takes precedence over other currencies in the quote.
    for text in texts:
        for word, code in _CURRENCY_WORDS:
            if word in (text or "").lower():
                return code
    joined = " ".join(t for t in texts if t).lower()
    return "CNY" if re.search(r"[元]", joined) else ""


def _scaled(value, factor):
    return value * (Decimal(str(factor)) if isinstance(value, Decimal) else factor)


def normalize_amount(value: float | Decimal | None, unit: str, context: str = "") -> tuple[float | Decimal | None, str, str]:
    """Return (value_norm, norm_unit, currency). Unknown units pass through unchanged."""
    if value is None:
        return None, unit or "", ""
    raw = (unit or "").strip()
    compact = re.sub(r"\s+", "", raw.lower())
    currency = detect_currency(raw, context)
    if compact in ("%", "％", "percent", "pct"):
        return value, "%", ""
    if re.search(r"[/／]|\bper\b|每", compact):
        return value, raw, currency  # A denominator is not a whole-company amount.
    stripped = compact
    for word, _code in _CURRENCY_WORDS:
        stripped = stripped.replace(word, "")
    stripped = stripped.replace("人民币", "")
    if stripped in _SCALE_TO_WAN:
        code = currency or "CNY"
        return _scaled(value, _SCALE_TO_WAN[stripped]), "万元" if code == "CNY" else f"万{_currency_label(code)}", code
    if currency:
        scale = _CN_SCALE_ONLY.get(stripped.removesuffix("元")) if stripped.endswith("元") else None
        if scale is None:
            scale = _CN_SCALE_ONLY.get(stripped) or _EN_SCALE.get(stripped)
        if scale is None and stripped in ("", "元"):
            scale = 1e-4
        if scale is not None:
            return _scaled(value, scale), "万元" if currency == "CNY" else f"万{_currency_label(currency)}", currency
    if stripped in _CN_SCALE_ONLY and currency in ("", "CNY") and _looks_monetary(context):
        return _scaled(value, _CN_SCALE_ONLY[stripped]), "万元", "CNY"
    return value, raw, currency


def _currency_label(code: str) -> str:
    return {"USD": "美元", "HKD": "港元", "EUR": "欧元", "JPY": "日元"}.get(code, code)


def _looks_monetary(context: str) -> bool:
    return bool(re.search(r"收入|利润|成本|费用|现金|资金|资产|融资|估值|金额", context or ""))


_YEAR = r"(?:19|20)\d{2}"


def period_key(period: str) -> str:
    """Canonical period key; returns the cleaned original when the form is unknown."""
    text = re.sub(r"\s+", "", (period or "")).upper().replace("年度", "年").replace("财年", "FY")
    text = text.replace("（", "(").replace("）", ")")
    match = re.fullmatch(rf"(?:FY)?({_YEAR})(?:年|FY|E|A|\(E\)|\(A\)|预测|预计|实际|全年)*", text)
    if match:
        return match.group(1)
    match = re.fullmatch(rf"FY({_YEAR})", text)
    if match:
        return match.group(1)
    match = re.fullmatch(rf"({_YEAR})年?(?:H|上半年|半年度|中报)(1)?", text)
    if match and ("上半年" in text or "半年度" in text or "中报" in text or text.endswith("H1")):
        return f"{match.group(1)}H1"
    match = re.fullmatch(rf"({_YEAR})年?(?:H2|下半年)", text)
    if match:
        return f"{match.group(1)}H2"
    match = (re.fullmatch(rf"({_YEAR})年?Q([1-4])", text)
             or re.fullmatch(rf"({_YEAR})年?第?([1-4一二三四])(?:季度|季)", text))
    if match:
        quarter = "一二三四".find(match.group(2)) + 1 if match.group(2) in "一二三四" else int(match.group(2))
        return f"{match.group(1)}Q{quarter}"
    match = re.fullmatch(rf"({_YEAR})年?([1-4])Q", text)
    if match:
        return f"{match.group(1)}Q{match.group(2)}"
    match = re.fullmatch(rf"([1-4])Q({_YEAR})", text)
    if match:
        return f"{match.group(2)}Q{match.group(1)}"
    match = re.fullmatch(rf"({_YEAR})(?:年|-|/|\.)(\d{{1,2}})(?:月|-|/|\.)(\d{{1,2}})日?", text)
    if match and 1 <= int(match.group(2)) <= 12 and 1 <= int(match.group(3)) <= 31:
        return f"{match.group(1)}M{int(match.group(2)):02d}"
    match = re.fullmatch(rf"({_YEAR})(?:年|-|/|\.)(\d{{1,2}})(?:月)?", text)
    if match and 1 <= int(match.group(2)) <= 12:
        return f"{match.group(1)}M{int(match.group(2)):02d}"
    match = re.fullmatch(rf"({_YEAR})年?(\d{{1,2}})(?:月)?[-—~至到](\d{{1,2}})月?", text)
    if match:
        start, end = int(match.group(2)), int(match.group(3))
        if start == 1 and end == 6:
            return f"{match.group(1)}H1"
        if start == 1 and end == 12:
            return match.group(1)
        if start == 1 and end == 3:
            return f"{match.group(1)}Q1"
        if start == 1 and 1 <= end <= 12:
            return f"{match.group(1)}-1-{end}M"
        if (start, end) in ((1, 3), (4, 6), (7, 9), (10, 12)):
            return f"{match.group(1)}Q{(end) // 3}"
    match = re.fullmatch(rf"({_YEAR})年?前三季度|({_YEAR})Q1-Q3|({_YEAR})年?1-9月", text)
    if match:
        year = next(g for g in match.groups() if g)
        return f"{year}-1-9M"
    return (period or "").strip()


def sort_key(key: str) -> tuple:
    match = re.match(r"(\d{4})(.*)", key or "")
    if not match:
        return (9999, key)
    rest = match.group(2)
    order = {"": 99, "H1": 6.5, "H2": 12.5}
    if rest in order:
        sub = order[rest]
    elif re.fullmatch(r"Q[1-4]", rest):
        sub = int(rest[1]) * 3 + 0.2
    elif re.fullmatch(r"M(?:0[1-9]|1[0-2])", rest):
        sub = int(rest[1:])
    elif re.fullmatch(r"-1-\d{1,2}M", rest):
        sub = int(re.sub(r"\D", "", rest[3:]) or 0) + 0.4
    else:
        sub = 98
    return (int(match.group(1)), sub, rest)


_DEFAULT_SCOPES = {"合并", "合并口径", "合并报表", "集团", "集团合并", "公司", "公司整体", "整体", "全公司",
                   "consolidated", "group", "total"}


def scope_key(scope: str | None) -> str:
    """Consolidated / whole-company scope is the default and compares equal to no scope."""
    text = re.sub(r"\s+", "", (scope or "")).strip("（）()")
    return "" if text.lower() in _DEFAULT_SCOPES else text


def normalize_metric(metric: dict) -> dict:
    """Add std_name, period_key, value_norm, norm_unit, currency to an extracted metric."""
    name = standard_name(metric.get("name", ""))
    context = " ".join(str(metric.get(k) or "") for k in ("name", "quote", "unit")) + " " + name
    value_norm, norm_unit, currency = normalize_amount(metric.get("value"), metric.get("unit", ""), context)
    if name in _PER_SHARE_NAMES:
        # A model often emits simply '元' even when the metric says '每股'.
        # Preserve the original fields; do not convert the numerator to 万元.
        unit = (metric.get("unit") or "").strip()
        value_norm = metric.get("value")
        norm_unit = unit if re.search(r"[/／]|\bper\b|每", unit.lower()) else f"{unit}/股" if unit else ""
    if (_name_key(metric.get("name", "")) in _LOSS_KEYS
            and value_norm is not None and norm_unit != "%"):
        value_norm = -abs(value_norm)
    raw_name = metric.get("name") or ""
    if (name in _NET_FLOW_NAMES and value_norm is not None and value_norm > 0
            and re.search(r"所用|流出|减少|used in", raw_name, re.I)
            and not re.search(r"所得|流入|增加|from (?:operating|investing|financing)", raw_name, re.I)):
        value_norm = -value_norm
    if name in PERCENT_NAMES and value_norm is not None and norm_unit != "%" and abs(value_norm) <= 1:
        value_norm, norm_unit = value_norm * 100, "%"
    scope = metric.get("scope") or ""
    if any(label in (metric.get("name") or "") for label in ("归母", "歸母")) and not scope_key(scope):
        scope = "归母"
    return {**metric, "std_name": name, "period_key": period_key(metric.get("period", "")),
            "value_norm": value_norm, "norm_unit": norm_unit, "currency": currency, "scope": scope}
