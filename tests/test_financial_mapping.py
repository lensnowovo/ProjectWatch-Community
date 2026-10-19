"""Structured finance suggestions stay source-located and unconfirmed."""

from decimal import Decimal

from project_watch.analysis.financial_mapping import MappingIssue, map_financial_fragments
from project_watch.domain import FragmentInput
from project_watch.extractors import _located_sheet_cells


def row(ordinal: int, sheet: str, number: int, text: str) -> FragmentInput:
    return FragmentInput(ordinal, f"sheet {sheet} row {number}", text)


def test_consolidated_actual_and_forecast_are_separate_candidates() -> None:
    fragments = [
        row(1, "合并利润表", 1, "A1=单位：万元"),
        row(2, "合并利润表", 2, "A2=项目 | B2=2025年实际 | C2=2026年预测"),
        row(
            3,
            "合并利润表",
            3,
            "A3=营业收入 | B3[header:2025年实际]=120.5 | C3[header:2026年预测]=160",
        ),
    ]
    result = map_financial_fragments("synthetic.xlsx", fragments)
    assert result.issues == ()
    assert [(item.metric, item.number, item.period, item.basis) for item in result.candidates] == [
        ("营业收入", Decimal("120.5"), "2025", "actual"),
        ("营业收入", Decimal("160"), "2026", "forecast"),
    ]
    assert all(item.unit == "万元" and item.scope == "合并" for item in result.candidates)
    assert all(item.source_locator == fragments[2].locator for item in result.candidates)


def test_missing_unit_or_period_never_creates_a_number() -> None:
    result = map_financial_fragments(
        "synthetic.xlsx",
        [
            row(1, "合并资产负债表", 1, "A1=应收账款 | B1[header:2025年实际]=100"),
            row(2, "合并资产负债表", 2, "A2=单位：万元"),
            row(3, "合并资产负债表", 3, "A3=存货 | B3=200"),
        ],
    )
    assert result.candidates == ()
    assert {issue.reason for issue in result.issues} == {"unit_missing", "period_missing"}


def test_sheet_context_does_not_leak_and_unknown_basis_stays_unknown() -> None:
    result = map_financial_fragments(
        "synthetic.xls",
        [
            row(1, "合并利润表", 1, "A1=单位：元"),
            row(2, "合并利润表", 2, "A2=营业成本 | B2[header:2025年]=60"),
            row(3, "单体利润表", 1, "A1=营业成本 | B1[header:2025年实际]=80"),
        ],
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].basis == "unknown"
    assert result.candidates[0].unit == "元"
    assert result.candidates[0].number == Decimal("60")
    assert any(issue.reason == "scope_or_unit_missing" for issue in result.issues)


def test_no_spreadsheet_or_unrecognized_metric_is_mapped() -> None:
    fragments = [row(1, "合并利润表", 1, "A1=单位：万元 | B1=2025年实际")]
    assert map_financial_fragments("notes.txt", fragments).candidates == ()
    assert map_financial_fragments("synthetic.xlsx", fragments).candidates == ()


def test_text_formatted_amounts_and_parenthesized_negative_are_exact_candidates() -> None:
    result = map_financial_fragments(
        "synthetic.xlsx",
        [
            row(1, "合并利润表", 1, "A1=单位：万元"),
            row(
                2,
                "合并利润表",
                3,
                "A3=净利润 | B3[header:2025年实际]=(1,234.50) | C3[header:2026年预测]=2,000.00",
            ),
        ],
    )

    assert result.issues == ()
    assert [(item.number, item.period, item.basis) for item in result.candidates] == [
        (Decimal("-1234.50"), "2025", "actual"),
        (Decimal("2000.00"), "2026", "forecast"),
    ]


def test_explicit_foreign_currency_is_not_mapped_as_rmb() -> None:
    result = map_financial_fragments(
        "synthetic.xlsx",
        [
            row(1, "合并利润表", 1, "A1=币种：美元 | B1=单位：万元"),
            row(2, "合并利润表", 2, "A2=营业收入 | B2[header:2025年实际]=120"),
        ],
    )

    assert result.candidates == ()
    assert result.issues == (MappingIssue("sheet 合并利润表 row 2", "currency_unsupported"),)


def test_explicit_rmb_unit_remains_eligible() -> None:
    result = map_financial_fragments(
        "synthetic.xlsx",
        [
            row(1, "合并利润表", 1, "A1=单位：人民币万元"),
            row(2, "合并利润表", 2, "A2=营业收入 | B2[header:2025年实际]=120"),
        ],
    )

    assert result.issues == ()
    assert len(result.candidates) == 1
    assert result.candidates[0].unit == "万元"


def test_percent_formatted_cell_cannot_be_mapped_as_currency_amount() -> None:
    result = map_financial_fragments(
        "synthetic.xlsx",
        [
            row(1, "合并利润表", 1, "A1=单位：万元"),
            row(
                2,
                "合并利润表",
                2,
                "A2=营业收入 | B2[header:2025年实际][format:percent]=0.7",
            ),
        ],
    )
    assert result.candidates == ()
    assert result.issues == (MappingIssue("sheet 合并利润表 row 2", "format_unit_conflict"),)


def test_candidate_limit_does_not_hide_later_financial_sheets() -> None:
    fragments = [row(1, "合并利润表", 1, "A1=单位：万元")]
    fragments.extend(
        row(
            year - 1998,
            "合并利润表",
            year - 1998,
            f"A{year - 1998}=营业收入 | B{year - 1998}[header:{year}年实际]={year}",
        )
        for year in range(2000, 2030)
    )
    fragments.extend(
        [
            row(32, "合并资产负债表", 1, "A1=单位：万元"),
            row(33, "合并资产负债表", 2, "A2=资产总计 | B2[header:2025年实际]=500"),
            row(34, "合并现金流量表", 1, "A1=单位：万元"),
            row(
                35,
                "合并现金流量表",
                2,
                "A2=经营活动现金流量净额 | B2[header:2025年实际]=80",
            ),
        ]
    )
    result = map_financial_fragments("synthetic-three-statements.xlsx", fragments)

    assert len(result.candidates) == 24
    assert {item.metric for item in result.candidates} == {
        "营业收入",
        "资产总计",
        "经营活动现金流量净额",
    }
    assert result.truncated_count == 8


def test_side_by_side_balance_sheet_maps_each_metric_to_its_own_values() -> None:
    result = map_financial_fragments(
        "synthetic-balance.xlsx",
        [
            row(1, "合并资产负债表", 1, "A1=单位：万元"),
            row(
                2,
                "合并资产负债表",
                3,
                "A3=货币资金 | B3[header:2025年实际]=100 | C3[header:2024年实际]=80"
                " | D3=短期借款 | E3[header:2025年实际]=50"
                " | F3[header:2024年实际]=40",
            ),
        ],
    )

    assert [(item.metric, item.period, item.number) for item in result.candidates] == [
        ("货币资金", "2025", Decimal("100")),
        ("货币资金", "2024", Decimal("80")),
        ("短期借款", "2025", Decimal("50")),
        ("短期借款", "2024", Decimal("40")),
    ]
    assert result.issues == ()


def test_unknown_side_label_blocks_value_bleed_from_known_metric() -> None:
    result = map_financial_fragments(
        "synthetic-balance.xlsx",
        [
            row(1, "合并资产负债表", 1, "A1=单位：万元"),
            row(
                2,
                "合并资产负债表",
                3,
                "A3=货币资金 | B3[header:2025年实际]=100 | D3=应付票据 | E3[header:2025年实际]=75",
            ),
        ],
    )

    assert [(item.metric, item.number) for item in result.candidates] == [
        ("货币资金", Decimal("100"))
    ]


def test_extractor_column_headers_flow_into_both_balance_sheet_sides() -> None:
    headers: dict[int, str] = {}
    rows = [
        ["单位：万元"],
        ["资产", "2025年实际", "2024年实际", "负债", "2025年实际", "2024年实际"],
        ["货币资金", 100, 80, "短期借款", 50, 40],
    ]
    fragments = [
        row(
            index, "合并资产负债表", index, " | ".join(_located_sheet_cells(values, index, headers))
        )
        for index, values in enumerate(rows, start=1)
    ]

    result = map_financial_fragments("synthetic-balance.xlsx", fragments)

    assert [(item.metric, item.period, item.number) for item in result.candidates] == [
        ("货币资金", "2025", Decimal("100")),
        ("货币资金", "2024", Decimal("80")),
        ("短期借款", "2025", Decimal("50")),
        ("短期借款", "2024", Decimal("40")),
    ]


def test_generic_sheet_name_uses_explicit_consolidated_statement_title() -> None:
    result = map_financial_fragments(
        "synthetic-balance.xlsx",
        [
            row(1, "资产负债表", 1, "A1=合并资产负债表"),
            row(2, "资产负债表", 2, "A2=单位：万元"),
            row(3, "资产负债表", 3, "A3=货币资金 | B3[header:2025年实际]=100"),
        ],
    )

    assert [(item.metric, item.scope, item.number) for item in result.candidates] == [
        ("货币资金", "合并", Decimal("100"))
    ]


def test_conflicting_statement_titles_in_one_sheet_keep_scope_unmapped() -> None:
    result = map_financial_fragments(
        "synthetic-balance.xlsx",
        [
            row(1, "资产负债表", 1, "A1=合并资产负债表"),
            row(2, "资产负债表", 2, "A2=单位：万元"),
            row(3, "资产负债表", 3, "A3=货币资金 | B3[header:2025年实际]=100"),
            row(4, "资产负债表", 4, "A4=母公司资产负债表"),
        ],
    )

    assert result.candidates == ()
    assert any(issue.reason == "scope_or_unit_missing" for issue in result.issues)


def test_trial_balance_only_suggests_explicit_closing_balance() -> None:
    headers: dict[int, str] = {}
    rows = [
        ["单位：元"],
        ["科目", "2025年期初余额", "2025年期末余额", "2025年借方发生额", "2025年贷方发生额"],
        ["应收账款", 80, 120, 90, 50],
    ]
    fragments = [
        row(
            index, "合并科目余额表", index, " | ".join(_located_sheet_cells(values, index, headers))
        )
        for index, values in enumerate(rows, start=1)
    ]

    result = map_financial_fragments("synthetic-trial.xlsx", fragments)

    assert [(item.metric, item.number, item.period, item.basis) for item in result.candidates] == [
        ("应收账款", Decimal("120"), "2025", "unknown")
    ]
    assert result.candidates[0].source_locator == "sheet 合并科目余额表 row 3"
    assert any(issue.reason == "trial_balance_not_closing" for issue in result.issues)


def test_trial_balance_without_explicit_closing_header_keeps_numbers_unmapped() -> None:
    result = map_financial_fragments(
        "synthetic-trial.xlsx",
        [
            row(1, "合并科目余额表", 1, "A1=单位：元"),
            row(
                2,
                "合并科目余额表",
                2,
                "A2=货币资金 | B2[header:2025年借方余额]=100 | C2[header:2025年贷方余额]=20",
            ),
        ],
    )

    assert result.candidates == ()
    assert {item.reason for item in result.issues} == {"trial_balance_not_closing"}


def test_generic_trial_sheet_uses_explicit_consolidated_title() -> None:
    result = map_financial_fragments(
        "synthetic-trial.xlsx",
        [
            row(1, "科目余额表", 1, "A1=合并科目余额表"),
            row(2, "科目余额表", 2, "A2=单位：万元"),
            row(3, "科目余额表", 3, "A3=存货 | B3[header:2025年期末余额]=40"),
        ],
    )

    assert [(item.metric, item.scope, item.number) for item in result.candidates] == [
        ("存货", "合并", Decimal("40"))
    ]


def test_trial_balance_debit_credit_subcolumns_are_not_closing_balances() -> None:
    result = map_financial_fragments(
        "synthetic-trial.xlsx",
        [
            row(1, "合并科目余额表", 1, "A1=单位：元"),
            row(2, "合并科目余额表", 2, "A2=科目 | B2=2025年期末余额"),
            row(3, "合并科目余额表", 3, "B3=借方 | C3=贷方"),
            row(
                4,
                "合并科目余额表",
                4,
                "A4=货币资金 | B4[header:2025年期末余额]=100 | C4=20",
            ),
        ],
    )

    assert result.candidates == ()
    assert any(issue.reason == "trial_balance_not_closing" for issue in result.issues)


def test_generic_sheet_trial_title_applies_closing_balance_guard() -> None:
    result = map_financial_fragments(
        "synthetic-trial.xlsx",
        [
            row(1, "Sheet1", 1, "A1=合并科目余额表"),
            row(2, "Sheet1", 2, "A2=单位：元"),
            row(
                3,
                "Sheet1",
                3,
                "A3=存货 | B3[header:2025年期初余额]=40 | C3[header:2025年期末余额]=55",
            ),
        ],
    )

    assert [(item.metric, item.number) for item in result.candidates] == [("存货", Decimal("55"))]
