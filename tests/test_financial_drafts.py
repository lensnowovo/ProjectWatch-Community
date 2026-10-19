"""Synthetic review drafts must not invent missing accounting context."""
from project_watch.analysis.financial_drafts import financial_review_drafts
from project_watch.analysis.financial_mapping import map_financial_fragments
from project_watch.domain import FragmentInput


def row(n, text, sheet="资产负债表"):
    return FragmentInput(n, f"sheet {sheet} row {n}", text)


def test_generic_balance_sheet_has_reviewable_values_not_automatic_facts():
    rows = [row(1, "A1=资产负债表 2025-12-31 单位：元"),
            row(2, "A2=资产 | B2=期末余额 | C2=年初余额 | D2=负债 | E2=期末余额"),
            row(4, "A4=货币资金 | B4=123.45 | C4=100 | D4=短期借款 | E4=20")]
    drafts, truncated = financial_review_drafts("synthetic.xls", rows)
    assert truncated == 0
    assert [(d.metric, d.number, d.period, d.source_cell) for d in drafts] == [
        ("货币资金", "123.45", "2025", "B4"), ("货币资金", "100", "2024", "C4"), ("短期借款", "20", "2025", "E4")]
    assert all(d.missing_fields == ("scope", "basis") for d in drafts)
    assert all(d.unit == "元" and d.scope is None and d.basis == "unknown" for d in drafts)
    assert "sheet 资产负债表 row 2" in drafts[0].context_locators
    assert map_financial_fragments("synthetic.xls", rows).candidates == ()


def test_monthly_and_relative_flows_are_not_silently_annualized():
    drafts, _ = financial_review_drafts("synthetic.xlsx", [
        row(1, "A1=2025年利润表 单位：元", "利润表"),
        row(2, "A2=项目 | B2=本月数 | C2=本期金额 | D2=上期金额", "利润表"),
        row(3, "A3=一、营业收入 | B3=10 | C3=120 | D3=90", "利润表")])
    assert len(drafts) == 3
    assert all(d.metric == "营业收入" and d.period is None for d in drafts)
    assert all("period" in d.missing_fields for d in drafts)


def test_drafts_preserve_missing_units_forecast_and_single_entity():
    drafts, _ = financial_review_drafts("synthetic.xlsx", [
        row(1, "A1=单体利润表", "利润表"),
        row(2, "A2=净利润 | B2[header:2026年预测]=(15.50)", "利润表")])
    assert len(drafts) == 1
    assert (drafts[0].number, drafts[0].basis, drafts[0].scope) == ("-15.50", "forecast", "单体")
    assert drafts[0].missing_fields == ("unit",)


def test_unit_and_date_do_not_leak_to_another_sheet():
    drafts, _ = financial_review_drafts("synthetic.xlsx", [
        row(1, "A1=2025-12-31 单位：元"),
        row(2, "A2=存货 | B2[header:2025年实际]=100"),
        FragmentInput(3, "sheet 利润表 row 2", "A2=净利润 | B2=20")])
    other = next(d for d in drafts if d.metric == "净利润")
    assert other.unit is None and other.period is None


def test_foreign_currency_percent_and_trial_debit_columns_not_suggested():
    assert financial_review_drafts("synthetic.xlsx", [row(1, "A1=币种：美元 单位：万元"), row(2, "A2=存货 | B2=100")])[0] == []
    assert financial_review_drafts("synthetic.xlsx", [row(1, "A1=存货 | B1[format:percent]=0.5")])[0] == []
    assert financial_review_drafts("synthetic.xlsx", [row(1, "A1=项目 | B1=2025年借方发生额", "科目余额表"), row(2, "A2=存货 | B2=100", "科目余额表")])[0] == []


def test_cashflow_line_number_is_not_money_and_ambiguous_units_stay_empty():
    drafts, _ = financial_review_drafts("synthetic.xlsx", [
        row(1, "A1=单位：元", "现金流量表"), row(2, "A2=项目 | B2=行次 | C2=金额", "现金流量表"),
        row(3, "A3=经营活动现金流量净额 | B3=11 | C3=200", "现金流量表"), row(4, "A4=单位：万元", "现金流量表")])
    assert len(drafts) == 1 and drafts[0].source_cell == "C3"
    assert drafts[0].unit is None and drafts[0].period is None


def test_non_spreadsheet_is_not_parsed():
    assert financial_review_drafts("report.pdf", [row(1, "A1=存货 | B1=20")]) == ([], 0)


def test_legacy_numeric_header_is_not_presented_as_a_column_name():
    drafts, _ = financial_review_drafts("synthetic.xls", [
        row(1, "A1=项目 | B1=金额"),
        row(2, "A2=存货 | B2[header:123456.78]=200")])
    assert drafts[0].column_header == "金额"
    assert drafts[0].period is None


def test_limit_is_bounded_and_distributed_across_sheets():
    rows = [row(n, f"A{n}=存货 | B{n}={n}") for n in range(1, 101)]
    rows.append(FragmentInput(101, "sheet 利润表 row 1", "A1=净利润 | B1=20"))
    drafts, truncated = financial_review_drafts("synthetic.xlsx", rows)
    assert len(drafts) == 80 and truncated == 21
    assert any(d.metric == "净利润" for d in drafts)


def test_numeric_text_is_not_reused_as_an_extractor_header():
    from project_watch.extractors import _located_sheet_cells
    headers = {}
    _located_sheet_cells(["项目", "金额"], 1, headers)
    _located_sheet_cells(["存货", "12025.60"], 2, headers)
    assert headers.get(2) != "12025.60"
