"""Deterministic financial-statement integrity screening.

The engine detects explainable anomalies; it never classifies a company as
fraudulent.  Calculations use normalized ``portfolio_kpis`` and expose every
formula, input gap, and model version to the human-facing project detail.

Ported from ``modules/companies/financial_integrity.py`` without DB imports.
Keep behavior aligned through synthetic parity tests; never load client data here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

ENGINE_VERSION = "financial-integrity-v1.1.0"


class FinancialRow(Protocol):
    kpi_name: str
    value: Any
    unit: str | None
    period: str | None
    as_of_date: Any


ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "operating_revenue", "sales"),
    "accounts_receivable": ("accounts_receivable", "receivables", "trade_receivables"),
    "net_profit": ("net_profit", "net_income", "income_from_continuing_operations"),
    "parent_net_profit": ("parent_net_profit", "net_profit_attributable_to_parent"),
    "operating_cash_flow": (
        "operating_cash_flow",
        "net_operating_cash_flow",
        "cash_flow_from_operations",
        "net_cash_from_operating_activities",
    ),
    "cash": ("cash", "cash_and_cash_equivalents", "monetary_funds"),
    "net_cash_flow": ("net_cash_flow", "net_increase_in_cash", "net_change_in_cash"),
    "current_liabilities": ("current_liabilities", "total_current_liabilities"),
    "accounts_payable": ("accounts_payable", "payables", "trade_payables"),
    "inventory": ("inventory", "inventories"),
    "cost_of_goods_sold": ("cost_of_goods_sold", "cogs", "operating_cost"),
    "current_assets": ("current_assets", "total_current_assets"),
    "property_plant_equipment": ("property_plant_equipment", "ppe", "fixed_assets"),
    "total_assets": ("total_assets",),
    "depreciation": ("depreciation", "depreciation_expense"),
    "sga_expense": ("sga_expense", "selling_general_admin_expense"),
    "long_term_debt": ("long_term_debt", "non_current_interest_bearing_debt"),
    "total_liabilities": ("total_liabilities",),
    "equity": ("equity", "total_equity"),
    "liabilities_and_equity": ("liabilities_and_equity",),
    "non_current_liabilities": ("non_current_liabilities",),
    "other_non_current_liabilities": ("other_non_current_liabilities",),
    "opening_cash": ("opening_cash",),
    "closing_cash": ("closing_cash",),
    "investing_cash_flow": ("investing_cash_flow",),
    "financing_cash_flow": ("financing_cash_flow",),
    "exchange_effect": ("exchange_effect",),
    "operating_cash_inflow": ("operating_cash_inflow",),
    "operating_cash_outflow": ("operating_cash_outflow",),
    "investing_cash_inflow": ("investing_cash_inflow",),
    "investing_cash_outflow": ("investing_cash_outflow",),
    "financing_cash_inflow": ("financing_cash_inflow",),
    "financing_cash_outflow": ("financing_cash_outflow",),
    "investment_cash_paid": ("investment_cash_paid",),
    "retained_earnings": ("retained_earnings",),
    "beginning_retained_earnings": ("beginning_retained_earnings",),
    "other_comprehensive_income": ("other_comprehensive_income",),
    "comprehensive_income_total": ("comprehensive_income_total",),
    "cash_received_from_sales": ("cash_received_from_sales",),
    "financial_expenses": ("financial_expenses",),
    "rd_expenses": ("rd_expenses", "research_and_development_expenses"),
    "deferred_tax_assets": ("deferred_tax_assets",),
    "taxes_payable": ("taxes_payable",),
    "short_term_borrowings": ("short_term_borrowings",),
    "long_term_borrowings": ("long_term_borrowings",),
}


@dataclass(frozen=True)
class MetricValue:
    value: float
    unit: str | None


def _period(row: FinancialRow) -> str | None:
    if row.period:
        return str(row.period)
    return row.as_of_date.isoformat() if row.as_of_date else None


def _canonical(name: str) -> str:
    lowered = name.strip().lower()
    for canonical, aliases in ALIASES.items():
        if lowered in aliases:
            return canonical
    return lowered


def _matrix(rows: Iterable[FinancialRow]) -> dict[str, dict[str, MetricValue]]:
    result: dict[str, dict[str, MetricValue]] = {}
    for row in rows:
        period = _period(row)
        if period is None or row.value is None:
            continue
        try:
            value = float(row.value)
        except (TypeError, ValueError):
            continue
        result.setdefault(period, {})[_canonical(row.kpi_name)] = MetricValue(value, row.unit)
    return result


def _value(metrics: dict[str, MetricValue], name: str) -> float | None:
    item = metrics.get(name)
    return item.value if item else None


def _change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior) * 100


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in {None, 0}:
        return None
    return numerator / denominator


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _check(
    *,
    key: str,
    title: str,
    status: str,
    label: str,
    observation: str,
    formula: str,
    values: dict[str, float | None],
    evidence_needed: list[str],
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "status": status,
        "label": label,
        "observation": observation,
        "formula": formula,
        "values": {name: _round(value) for name, value in values.items()},
        "evidence_needed": evidence_needed,
    }


def _material_difference(actual: float, expected: float) -> bool:
    """Use an explainable tolerance for values normalized to 万元."""
    return abs(actual - expected) > max(abs(actual) * 0.001, 1.0)


def _cross_statement_checks(
    current: dict[str, MetricValue], prior: dict[str, MetricValue]
) -> list[dict[str, Any]]:
    """Verify deterministic relationships inside one reporting period."""
    checks: list[dict[str, Any]] = []

    assets = _value(current, "total_assets")
    liabilities = _value(current, "total_liabilities")
    equity = _value(current, "equity")
    reported_total = _value(current, "liabilities_and_equity")
    expected_total = (
        liabilities + equity if liabilities is not None and equity is not None else reported_total
    )
    balance_gap = (
        assets - expected_total if assets is not None and expected_total is not None else None
    )
    balance_warning = (
        balance_gap is not None
        and assets is not None
        and expected_total is not None
        and _material_difference(assets, expected_total)
    )
    checks.append(
        _check(
            key="balance_sheet_equation",
            title="资产负债表恒等式",
            status="warning" if balance_warning else "ok" if balance_gap is not None else "gap",
            label="存在勾稽差"
            if balance_warning
            else "勾稽一致"
            if balance_gap is not None
            else "待补数据",
            observation=(
                f"资产总计与负债及权益合计相差{balance_gap:,.1f}万元。"
                if balance_gap is not None
                else "需要资产、负债和所有者权益合计。"
            ),
            formula="资产总计 -（负债合计 + 所有者权益合计）",
            values={
                "total_assets_wan": assets,
                "total_liabilities_wan": liabilities,
                "equity_wan": equity,
                "tie_difference_wan": balance_gap,
            },
            evidence_needed=["资产负债表", "合并抵销表"],
        )
    )

    cfo = _value(current, "operating_cash_flow")
    cfi = _value(current, "investing_cash_flow")
    cff = _value(current, "financing_cash_flow")
    exchange = _value(current, "exchange_effect")
    net_cash = _value(current, "net_cash_flow")
    expected_net_cash = None
    if cfo is not None and cfi is not None and cff is not None and exchange is not None:
        expected_net_cash = cfo + cfi + cff + exchange
    cash_flow_gap = (
        net_cash - expected_net_cash
        if net_cash is not None and expected_net_cash is not None
        else None
    )
    cash_flow_warning = (
        cash_flow_gap is not None
        and net_cash is not None
        and expected_net_cash is not None
        and _material_difference(net_cash, expected_net_cash)
    )
    checks.append(
        _check(
            key="cash_flow_statement_bridge",
            title="现金流量表分部勾稽",
            status="warning" if cash_flow_warning else "ok" if cash_flow_gap is not None else "gap",
            label="存在勾稽差"
            if cash_flow_warning
            else "勾稽一致"
            if cash_flow_gap is not None
            else "待补数据",
            observation=(
                f"经营、投资、筹资现金流及汇率影响之和与现金净增加额相差{cash_flow_gap:,.1f}万元。"
                if cash_flow_gap is not None
                else "需要经营、投资、筹资现金流及汇率影响。"
            ),
            formula="现金净增加额 -（经营现金流 + 投资现金流 + 筹资现金流 + 汇率影响）",
            values={
                "operating_cash_flow_wan": cfo,
                "investing_cash_flow_wan": cfi,
                "financing_cash_flow_wan": cff,
                "exchange_effect_wan": exchange,
                "tie_difference_wan": cash_flow_gap,
            },
            evidence_needed=["现金流量表", "现金流量表编制底稿"],
        )
    )

    opening_cash = _value(current, "opening_cash")
    closing_cash = _value(current, "closing_cash") or _value(current, "cash")
    roll_forward_gap = None
    if closing_cash is not None and opening_cash is not None and net_cash is not None:
        roll_forward_gap = closing_cash - opening_cash - net_cash
    roll_forward_warning = (
        roll_forward_gap is not None
        and closing_cash is not None
        and _material_difference(closing_cash, closing_cash - roll_forward_gap)
    )
    checks.append(
        _check(
            key="cash_statement_roll_forward",
            title="现金期初期末勾稽",
            status="warning"
            if roll_forward_warning
            else "ok"
            if roll_forward_gap is not None
            else "gap",
            label="存在勾稽差"
            if roll_forward_warning
            else "勾稽一致"
            if roll_forward_gap is not None
            else "待补数据",
            observation=(
                f"期初现金加现金净增加额与期末现金相差{roll_forward_gap:,.1f}万元。"
                if roll_forward_gap is not None
                else "需要同表列示的期初、期末现金及现金净增加额。"
            ),
            formula="期末现金 - 期初现金 - 现金净增加额",
            values={
                "opening_cash_wan": opening_cash,
                "closing_cash_wan": closing_cash,
                "net_cash_change_wan": net_cash,
                "tie_difference_wan": roll_forward_gap,
            },
            evidence_needed=["现金流量表", "银行余额调节表"],
        )
    )

    comprehensive = _value(current, "comprehensive_income_total")
    profit = _value(current, "net_profit")
    other_comprehensive = _value(current, "other_comprehensive_income")
    comprehensive_gap = None
    if comprehensive is not None and profit is not None and other_comprehensive is not None:
        comprehensive_gap = comprehensive - profit - other_comprehensive
    comprehensive_warning = (
        comprehensive_gap is not None
        and comprehensive is not None
        and _material_difference(comprehensive, comprehensive - comprehensive_gap)
    )
    checks.append(
        _check(
            key="comprehensive_income_equation",
            title="综合收益勾稽",
            status=(
                "warning"
                if comprehensive_warning
                else "ok"
                if comprehensive_gap is not None
                else "gap"
            ),
            label=(
                "报表内部不一致"
                if comprehensive_warning
                else "勾稽一致"
                if comprehensive_gap is not None
                else "待补数据"
            ),
            observation=(
                f"综合收益总额与净利润加其他综合收益相差{comprehensive_gap:,.1f}万元。"
                if comprehensive_gap is not None
                else "需要净利润、其他综合收益和综合收益总额。"
            ),
            formula="综合收益总额 - 净利润 - 其他综合收益",
            values={
                "net_profit_wan": profit,
                "other_comprehensive_income_wan": other_comprehensive,
                "comprehensive_income_total_wan": comprehensive,
                "tie_difference_wan": comprehensive_gap,
            },
            evidence_needed=["利润表编制底稿", "其他综合收益明细", "合并报表复核说明"],
        )
    )

    retained = _value(current, "retained_earnings")
    beginning_retained = _value(current, "beginning_retained_earnings")
    prior_retained = _value(prior, "retained_earnings")
    parent_profit = _value(current, "parent_net_profit")
    retained_roll_gap = (
        retained - beginning_retained - parent_profit
        if retained is not None and beginning_retained is not None and parent_profit is not None
        else None
    )
    opening_retained_gap = (
        beginning_retained - prior_retained
        if beginning_retained is not None and prior_retained is not None
        else None
    )
    retained_warning = (
        retained_roll_gap is not None
        and retained is not None
        and _material_difference(retained, retained - retained_roll_gap)
    ) or (
        opening_retained_gap is not None
        and beginning_retained is not None
        and _material_difference(beginning_retained, beginning_retained - opening_retained_gap)
    )
    checks.append(
        _check(
            key="retained_earnings_roll_forward",
            title="未分配利润期初期末衔接",
            status="warning"
            if retained_warning
            else "ok"
            if retained_roll_gap is not None
            else "gap",
            label="期初数需解释"
            if retained_warning
            else "勾稽一致"
            if retained_roll_gap is not None
            else "待补数据",
            observation=(
                f"利润表年初未分配利润与上年资产负债表年末数相差{opening_retained_gap:,.1f}万元；按归母净利润推算的期末数相差{retained_roll_gap:,.1f}万元。"
                if retained_roll_gap is not None and opening_retained_gap is not None
                else "需要上年末、本年初和本年末未分配利润及归母净利润。"
            ),
            formula="本年末未分配利润 - 本年初未分配利润 - 归母净利润；本年初 - 上年末",
            values={
                "retained_earnings_wan": retained,
                "beginning_retained_earnings_wan": beginning_retained,
                "prior_retained_earnings_wan": prior_retained,
                "parent_net_profit_wan": parent_profit,
                "roll_forward_difference_wan": retained_roll_gap,
                "opening_difference_wan": opening_retained_gap,
            },
            evidence_needed=["所有者权益变动表", "前期差错更正说明", "合并范围变动说明"],
        )
    )
    return checks


def _deterministic_checks(
    current: dict[str, MetricValue], prior: dict[str, MetricValue]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    revenue, prior_revenue = _value(current, "revenue"), _value(prior, "revenue")
    receivable, prior_receivable = (
        _value(current, "accounts_receivable"),
        _value(prior, "accounts_receivable"),
    )
    revenue_growth, receivable_growth = (
        _change(revenue, prior_revenue),
        _change(receivable, prior_receivable),
    )
    divergence = (
        receivable_growth - revenue_growth
        if receivable_growth is not None and revenue_growth is not None
        else None
    )
    ar_ratio = _ratio(receivable, revenue)
    checks.append(
        _check(
            key="revenue_receivable_divergence",
            title="收入增长与应收增长",
            status="warning"
            if divergence is not None and divergence > 20
            else "ok"
            if divergence is not None
            else "gap",
            label="出现背离"
            if divergence is not None and divergence > 20
            else "未见明显背离"
            if divergence is not None
            else "待补数据",
            observation=(
                f"收入同比{revenue_growth:+.1f}%，应收同比{receivable_growth:+.1f}%，增速差{divergence:+.1f}%。"
                if divergence is not None
                and revenue_growth is not None
                and receivable_growth is not None
                else "需要连续两期收入与应收账款数据。"
            ),
            formula="应收增速 - 收入增速；应收账款 / 营业收入",
            values={
                "revenue_growth_pct": revenue_growth,
                "receivable_growth_pct": receivable_growth,
                "divergence_pct": divergence,
                "receivable_to_revenue_pct": ar_ratio * 100 if ar_ratio is not None else None,
            },
            evidence_needed=[
                "应收账龄表",
                "期后回款",
                "前十大客户收入与回款",
                "销售合同及验收记录",
            ],
        )
    )

    profit, operating_cash = _value(current, "net_profit"), _value(current, "operating_cash_flow")
    profit_cash_gap = (
        operating_cash - profit if operating_cash is not None and profit is not None else None
    )
    opposite = profit is not None and operating_cash is not None and profit > 0 > operating_cash
    deeper_cash_loss = (
        profit is not None
        and operating_cash is not None
        and revenue not in {None, 0}
        and operating_cash < profit
        and abs(operating_cash - profit) > abs(revenue or 0) * 0.1
    )
    checks.append(
        _check(
            key="profit_cash_reconciliation",
            title="净利润与经营现金流",
            status="warning"
            if opposite or deeper_cash_loss
            else "ok"
            if profit_cash_gap is not None
            else "gap",
            label="现金表现弱于损益"
            if opposite or deeper_cash_loss
            else "未见方向性背离"
            if profit_cash_gap is not None
            else "待补数据",
            observation=(
                f"本期净利润{profit:,.1f}万元，经营现金流{operating_cash:,.1f}万元，两者差额{profit_cash_gap:,.1f}万元。"
                if profit_cash_gap is not None and profit is not None and operating_cash is not None
                else "需要同期间净利润和经营活动现金流。"
            ),
            formula="经营活动现金流 - 净利润",
            values={
                "net_profit_wan": profit,
                "operating_cash_flow_wan": operating_cash,
                "cash_conversion_gap_wan": profit_cash_gap,
            },
            evidence_needed=["现金流量表", "营运资金变动明细", "非现金损益明细"],
        )
    )

    cash = _value(current, "cash")
    prior_cash = _value(current, "opening_cash") or _value(prior, "cash")
    net_cash = _value(current, "net_cash_flow")
    expected_cash = (
        prior_cash + net_cash if prior_cash is not None and net_cash is not None else None
    )
    cash_tie_difference = (
        cash - expected_cash if cash is not None and expected_cash is not None else None
    )
    material_cash_gap = cash_tie_difference is not None and abs(cash_tie_difference) > max(
        abs(cash or 0) * 0.05, 10
    )
    checks.append(
        _check(
            key="cash_roll_forward",
            title="现金余额与现金净变动",
            status="warning"
            if material_cash_gap
            else "ok"
            if cash_tie_difference is not None
            else "gap",
            label="存在勾稽差"
            if material_cash_gap
            else "基本一致"
            if cash_tie_difference is not None
            else "待补数据",
            observation=(
                f"按现金净变动推算的期末余额与报表余额相差{cash_tie_difference:,.1f}万元。"
                if cash_tie_difference is not None
                else "需要期初现金、现金净增加额和期末现金。"
            ),
            formula="期末现金 -（期初现金 + 本期现金净增加额）",
            values={
                "closing_cash_wan": cash,
                "opening_cash_wan": prior_cash,
                "net_cash_change_wan": net_cash,
                "tie_difference_wan": cash_tie_difference,
            },
            evidence_needed=["现金流量表", "银行对账单", "受限资金明细"],
        )
    )

    liabilities, prior_liabilities = (
        _value(current, "current_liabilities"),
        _value(prior, "current_liabilities"),
    )
    payables = _value(current, "accounts_payable")
    liability_growth = _change(liabilities, prior_liabilities)
    checks.append(
        _check(
            key="current_liability_payable",
            title="流动负债与应付款项",
            status="warning"
            if liability_growth is not None and liability_growth > 50
            else "gap"
            if liabilities is not None and payables is None
            else "ok",
            label="增长较快"
            if liability_growth is not None and liability_growth > 50
            else "明细待补"
            if liabilities is not None and payables is None
            else "未见明显异常",
            observation=(
                f"流动负债同比{liability_growth:+.1f}%，本期应付账款{payables:,.1f}万元。"
                if liability_growth is not None and payables is not None
                else "需要流动负债及应付账款的连续期间明细。"
            ),
            formula="流动负债同比变化；应付账款 / 流动负债",
            values={
                "current_liabilities_wan": liabilities,
                "liability_growth_pct": liability_growth,
                "accounts_payable_wan": payables,
                "payable_share_pct": (_ratio(payables, liabilities) or 0) * 100
                if payables is not None and liabilities not in {None, 0}
                else None,
            },
            evidence_needed=["其他应付款明细", "供应商账龄", "关联方往来", "采购合同"],
        )
    )
    return checks


def _quality_checks(
    current: dict[str, MetricValue], prior: dict[str, MetricValue]
) -> list[dict[str, Any]]:
    """Screen earnings quality, funding dependence, and unusual account signs."""
    checks: list[dict[str, Any]] = []
    revenue = _value(current, "revenue")
    prior_revenue = _value(prior, "revenue")
    receivable = _value(current, "accounts_receivable")
    prior_receivable = _value(prior, "accounts_receivable")
    sales_cash = _value(current, "cash_received_from_sales")
    collection_rate = _ratio(sales_cash, revenue)
    dso = _ratio(receivable, revenue)
    dso_days = dso * 365 if dso is not None else None
    revenue_growth = _change(revenue, prior_revenue)
    receivable_growth = _change(receivable, prior_receivable)
    collection_warning = (
        collection_rate is not None
        and collection_rate < 0.7
        and receivable_growth is not None
        and revenue_growth is not None
        and receivable_growth > revenue_growth + 20
    )
    checks.append(
        _check(
            key="revenue_cash_collection_quality",
            title="收入现金含量与应收周转",
            status="warning"
            if collection_warning
            else "ok"
            if collection_rate is not None
            else "gap",
            label="回款质量待核验"
            if collection_warning
            else "已完成筛查"
            if collection_rate is not None
            else "待补数据",
            observation=(
                f"销售收现/收入为{collection_rate * 100:.1f}%，期末应收/收入为{dso * 100:.1f}%（粗略DSO {dso_days:.0f}天）。"
                if collection_rate is not None and dso is not None and dso_days is not None
                else "需要营业收入、销售收现及应收账款。"
            ),
            formula="销售商品收现 / 营业收入；期末应收 / 营业收入 × 365",
            values={
                "cash_collection_rate_pct": (
                    collection_rate * 100 if collection_rate is not None else None
                ),
                "receivable_to_revenue_pct": dso * 100 if dso is not None else None,
                "dso_days": dso_days,
                "revenue_growth_pct": revenue_growth,
                "receivable_growth_pct": receivable_growth,
            },
            evidence_needed=["应收账龄表", "期后回款流水", "前十大客户合同", "收入确认及验收凭证"],
        )
    )

    cfo = _value(current, "operating_cash_flow")
    cfi = _value(current, "investing_cash_flow")
    cff = _value(current, "financing_cash_flow")
    profit = _value(current, "net_profit")
    financing_dependency = _ratio(cff, abs(cfo or 0) + abs(cfi or 0))
    cash_burn = -(cfo or 0) if cfo is not None and cfo < 0 else 0.0
    cash = _value(current, "cash")
    runway_months = _ratio(cash, cash_burn / 12) if cash_burn else None
    funding_warning = (
        cfo is not None
        and cfi is not None
        and cff is not None
        and cfo < 0
        and cfi < 0
        and cff > abs(cfo + cfi)
    )
    checks.append(
        _check(
            key="cash_burn_financing_dependence",
            title="经营烧钱与外部融资依赖",
            status="warning" if funding_warning else "ok" if None not in {cfo, cfi, cff} else "gap",
            label="依赖外部融资"
            if funding_warning
            else "已完成筛查"
            if None not in {cfo, cfi, cff}
            else "待补数据",
            observation=(
                f"经营现金流{cfo:,.1f}万元、投资现金流{cfi:,.1f}万元，筹资现金流{cff:,.1f}万元；按本期经营净流出粗略测算现金覆盖约{runway_months:.1f}个月。"
                if None not in {cfo, cfi, cff} and runway_months is not None
                else "需要三类现金流及期末现金。"
            ),
            formula="筹资现金流 /（|经营现金流| + |投资现金流|）；现金 / 月均经营净流出",
            values={
                "operating_cash_flow_wan": cfo,
                "investing_cash_flow_wan": cfi,
                "financing_cash_flow_wan": cff,
                "net_profit_wan": profit,
                "financing_dependency_ratio": financing_dependency,
                "runway_months": runway_months,
            },
            evidence_needed=["月度现金流预测", "融资协议", "借款合同", "未来十二个月资金计划"],
        )
    )

    financial_expenses = _value(current, "financial_expenses")
    finance_cost_ratio = _ratio(financial_expenses, revenue)
    short_debt = _value(current, "short_term_borrowings")
    long_debt = _value(current, "long_term_borrowings")
    interest_bearing_debt = (
        short_debt + long_debt if short_debt is not None and long_debt is not None else None
    )
    finance_cost_warning = finance_cost_ratio is not None and finance_cost_ratio > 0.1
    checks.append(
        _check(
            key="finance_cost_reconciliation",
            title="财务费用与有息负债",
            status="warning"
            if finance_cost_warning
            else "ok"
            if finance_cost_ratio is not None
            else "gap",
            label="费用异常偏高"
            if finance_cost_warning
            else "已完成筛查"
            if finance_cost_ratio is not None
            else "待补数据",
            observation=(
                f"财务费用{financial_expenses:,.1f}万元，占收入{finance_cost_ratio * 100:.1f}%；期末借款合计{interest_bearing_debt:,.1f}万元。"
                if finance_cost_ratio is not None
                and financial_expenses is not None
                and interest_bearing_debt is not None
                else "需要财务费用、营业收入及有息负债。"
            ),
            formula="财务费用 / 营业收入；与期末及平均有息负债交叉复核",
            values={
                "financial_expenses_wan": financial_expenses,
                "financial_expense_to_revenue_pct": (
                    finance_cost_ratio * 100 if finance_cost_ratio is not None else None
                ),
                "interest_bearing_debt_wan": interest_bearing_debt,
            },
            evidence_needed=["财务费用明细", "利息资本化明细", "汇兑损益明细", "借款及融资合同"],
        )
    )

    taxes_payable = _value(current, "taxes_payable")
    investment_cash_paid = _value(current, "investment_cash_paid")
    unusual_items = []
    if taxes_payable is not None and taxes_payable < 0:
        unusual_items.append(f"应交税费为负{abs(taxes_payable):,.1f}万元")
    if investment_cash_paid is not None and investment_cash_paid < 0:
        unusual_items.append(f"投资支付的现金为负{abs(investment_cash_paid):,.1f}万元")
    checks.append(
        _check(
            key="negative_balance_presentation",
            title="负数科目与列报方向",
            status="warning" if unusual_items else "ok" if taxes_payable is not None else "gap",
            label="列报方向待核实"
            if unusual_items
            else "未见负数异常"
            if taxes_payable is not None
            else "待补数据",
            observation="；".join(unusual_items)
            if unusual_items
            else "未发现已接入科目的异常负数列报。",
            formula="对通常不应以负数列示的负债及现金流出项目进行方向筛查",
            values={
                "taxes_payable_wan": taxes_payable,
                "investment_cash_paid_wan": investment_cash_paid,
            },
            evidence_needed=["应交税费明细及重分类说明", "投资活动现金流底稿", "投资收回凭证"],
        )
    )

    dta = _value(current, "deferred_tax_assets")
    dta_share = _ratio(dta, _value(current, "total_assets"))
    dta_warning = dta_share is not None and dta_share > 0.1 and profit is not None and profit < 0
    checks.append(
        _check(
            key="deferred_tax_asset_recoverability",
            title="递延所得税资产可实现性",
            status="warning" if dta_warning else "ok" if dta_share is not None else "gap",
            label="可实现性待核验"
            if dta_warning
            else "已完成筛查"
            if dta_share is not None
            else "待补数据",
            observation=(
                f"递延所得税资产{dta:,.1f}万元，占总资产{dta_share * 100:.1f}%，且本期仍为亏损。"
                if dta_share is not None and dta is not None and profit is not None
                else "需要递延所得税资产、总资产及盈利情况。"
            ),
            formula="递延所得税资产 / 总资产，并结合未来可抵扣应纳税所得额判断",
            values={
                "deferred_tax_assets_wan": dta,
                "deferred_tax_asset_share_pct": dta_share * 100 if dta_share is not None else None,
                "net_profit_wan": profit,
            },
            evidence_needed=["递延所得税资产明细", "可抵扣亏损到期表", "未来盈利预测及订单依据"],
        )
    )

    other_non_current = _value(current, "other_non_current_liabilities")
    other_non_current_share = _ratio(other_non_current, _value(current, "total_assets"))
    other_non_current_warning = (
        other_non_current_share is not None and other_non_current_share > 0.05
    )
    checks.append(
        _check(
            key="other_non_current_liability_substance",
            title="其他非流动负债经济实质",
            status=(
                "warning"
                if other_non_current_warning
                else "ok"
                if other_non_current_share is not None
                else "gap"
            ),
            label=(
                "大额新增科目"
                if other_non_current_warning
                else "已完成筛查"
                if other_non_current_share is not None
                else "待补数据"
            ),
            observation=(
                f"其他非流动负债{other_non_current:,.1f}万元，占总资产{other_non_current_share * 100:.1f}%。"
                if other_non_current_share is not None and other_non_current is not None
                else "需要其他非流动负债及总资产。"
            ),
            formula="其他非流动负债 / 总资产，并与上年末及合同义务核对",
            values={
                "other_non_current_liabilities_wan": other_non_current,
                "other_non_current_liability_share_pct": (
                    other_non_current_share * 100 if other_non_current_share is not None else None
                ),
            },
            evidence_needed=["其他非流动负债明细", "形成原因及合同", "是否附带回购或赎回义务"],
        )
    )
    return checks


def _beneish(current: dict[str, MetricValue], prior: dict[str, MetricValue]) -> dict[str, Any]:
    required = {
        "revenue",
        "accounts_receivable",
        "cost_of_goods_sold",
        "current_assets",
        "property_plant_equipment",
        "total_assets",
        "depreciation",
        "sga_expense",
        "current_liabilities",
        "long_term_debt",
        "net_profit",
        "operating_cash_flow",
    }
    missing = sorted(
        name for name in required if _value(current, name) is None or _value(prior, name) is None
    )
    base = {
        "name": "Beneish M-Score",
        "version": "1999 eight-variable",
        "status": "not_calculable" if missing else "calculated",
        "missing_metrics": missing,
        "threshold": -1.78,
        "interpretation": "仅作盈余操纵筛查，不构成财务造假结论。",
    }
    if missing:
        return {**base, "score": None, "components": {}}

    sales_t, sales_p = _value(current, "revenue") or 0, _value(prior, "revenue") or 0
    ar_t, ar_p = (
        _value(current, "accounts_receivable") or 0,
        _value(prior, "accounts_receivable") or 0,
    )
    cogs_t, cogs_p = (
        _value(current, "cost_of_goods_sold") or 0,
        _value(prior, "cost_of_goods_sold") or 0,
    )
    ca_t, ca_p = _value(current, "current_assets") or 0, _value(prior, "current_assets") or 0
    ppe_t, ppe_p = (
        _value(current, "property_plant_equipment") or 0,
        _value(prior, "property_plant_equipment") or 0,
    )
    ta_t, ta_p = _value(current, "total_assets") or 0, _value(prior, "total_assets") or 0
    dep_t, dep_p = _value(current, "depreciation") or 0, _value(prior, "depreciation") or 0
    sga_t, sga_p = _value(current, "sga_expense") or 0, _value(prior, "sga_expense") or 0
    debt_t = (_value(current, "current_liabilities") or 0) + (
        _value(current, "long_term_debt") or 0
    )
    debt_p = (_value(prior, "current_liabilities") or 0) + (_value(prior, "long_term_debt") or 0)
    ni_t, cfo_t = _value(current, "net_profit") or 0, _value(current, "operating_cash_flow") or 0
    components = {
        "DSRI": _ratio(_ratio(ar_t, sales_t), _ratio(ar_p, sales_p)),
        "GMI": _ratio(_ratio(sales_p - cogs_p, sales_p), _ratio(sales_t - cogs_t, sales_t)),
        "AQI": _ratio(
            1 - ((_ratio(ca_t + ppe_t, ta_t)) or 0), 1 - ((_ratio(ca_p + ppe_p, ta_p)) or 0)
        ),
        "SGI": _ratio(sales_t, sales_p),
        "DEPI": _ratio(_ratio(dep_p, dep_p + ppe_p), _ratio(dep_t, dep_t + ppe_t)),
        "SGAI": _ratio(_ratio(sga_t, sales_t), _ratio(sga_p, sales_p)),
        "LVGI": _ratio(_ratio(debt_t, ta_t), _ratio(debt_p, ta_p)),
        "TATA": _ratio(ni_t - cfo_t, ta_t),
    }
    if any(value is None for value in components.values()):
        return {
            **base,
            "status": "not_calculable",
            "score": None,
            "components": {key: _round(value) for key, value in components.items()},
            "missing_metrics": ["存在零分母或无效期间"],
        }
    calculated = {key: float(value) for key, value in components.items() if value is not None}
    score = (
        -4.84
        + 0.920 * calculated["DSRI"]
        + 0.528 * calculated["GMI"]
        + 0.404 * calculated["AQI"]
        + 0.892 * calculated["SGI"]
        + 0.115 * calculated["DEPI"]
        - 0.172 * calculated["SGAI"]
        + 4.679 * calculated["TATA"]
        - 0.327 * calculated["LVGI"]
    )
    return {
        **base,
        "score": _round(score),
        "components": {key: _round(value) for key, value in components.items()},
        "screening_result": "higher_attention" if score > -1.78 else "below_reference_threshold",
    }


def build_financial_integrity_analysis(rows: Iterable[FinancialRow]) -> dict[str, Any]:
    """Return an explainable, versioned screening result from normalized KPIs."""
    matrix = _matrix(rows)
    periods = sorted(matrix)
    if not periods:
        return {
            "engine_version": ENGINE_VERSION,
            "status": "insufficient_data",
            "periods": [],
            "as_of_period": None,
            "metric_count": 0,
            "checks": [],
            "models": {
                "beneish": {
                    "name": "Beneish M-Score",
                    "status": "not_calculable",
                    "score": None,
                    "missing_metrics": sorted(ALIASES),
                }
            },
        }
    current = matrix[periods[-1]]
    prior = matrix[periods[-2]] if len(periods) > 1 else {}
    checks = [
        *_cross_statement_checks(current, prior),
        *_deterministic_checks(current, prior),
        *_quality_checks(current, prior),
    ]
    warning_count = sum(item["status"] == "warning" for item in checks)
    gap_count = sum(item["status"] == "gap" for item in checks)
    verified_count = sum(item["status"] == "ok" for item in checks)
    return {
        "engine_version": ENGINE_VERSION,
        "status": "screened" if len(periods) > 1 else "partial",
        "periods": periods,
        "as_of_period": periods[-1],
        "metric_count": len({name for metrics in matrix.values() for name in metrics}),
        "checks": checks,
        "summary": {
            "warning_count": warning_count,
            "data_gap_count": gap_count,
            "verified_count": verified_count,
            "check_count": len(checks),
        },
        "models": {
            "beneish": _beneish(current, prior),
            "dechow_fscore": {
                "name": "Dechow F-Score",
                "status": "not_calculable",
                "score": None,
                "missing_metrics": ["RSST应计项", "软资产", "证券发行信息"],
                "interpretation": "待取得完整资产负债科目后启用；不使用不完整变量估算分数。",
            },
        },
    }


__all__ = ["ENGINE_VERSION", "build_financial_integrity_analysis"]
