"""Shared financial field names, independent of any screening or scoring engine."""

CHINESE_FINANCIAL_NAMES = {
    "营业收入": "revenue",
    "营业成本": "cost_of_goods_sold",
    "应收账款": "accounts_receivable",
    "净利润": "net_profit",
    "经营活动现金流量净额": "operating_cash_flow",
    "经营活动产生的现金流量净额": "operating_cash_flow",
    "货币资金": "cash",
    "存货": "inventory",
    "流动资产合计": "current_assets",
    "资产总计": "total_assets",
    "流动负债合计": "current_liabilities",
    "负债合计": "total_liabilities",
    "所有者权益合计": "equity",
    "固定资产": "property_plant_equipment",
    "短期借款": "short_term_borrowings",
    "长期借款": "long_term_borrowings",
}
FINANCIAL_METRIC_NAMES = frozenset(CHINESE_FINANCIAL_NAMES)
