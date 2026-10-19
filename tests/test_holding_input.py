from datetime import date

import pytest

from project_watch.holding_snapshots import validated_holding_input

BASE = {
    "fund_name": "甲基金",
    "as_of": date(2026, 6, 30),
    "source_locator": "sheet Holdings!A1:D3",
    "fragment_locator": "sheet Holdings!A1:D3",
    "fragment_text": "甲基金 2026-06-30 成本 1,100 万元，持股 8.5%，价值 1,430 万元",
    "invested_cost_wan": "1100",
    "ownership_pct": "8.5",
    "current_value_wan": "1430",
}


def test_holding_input_requires_literal_numbers_and_exact_fragment_locator() -> None:
    assert validated_holding_input(**BASE)[1:] == (1100, 8.5, 1430)
    with pytest.raises(ValueError, match="does not occur"):
        validated_holding_input(**{**BASE, "current_value_wan": "1500"})
    with pytest.raises(ValueError, match="locator"):
        validated_holding_input(**{**BASE, "source_locator": "page 9"})
    with pytest.raises(ValueError, match="fund name"):
        validated_holding_input(**{**BASE, "fund_name": "乙基金"})
    with pytest.raises(ValueError, match="万元"):
        validated_holding_input(
            **{**BASE, "fragment_text": "甲基金 成本 1,100 元，持股 8.5%，价值 1,430 元"}
        )


def test_holding_input_rejects_missing_or_invalid_measurements() -> None:
    empty = {**BASE, "invested_cost_wan": None, "ownership_pct": None, "current_value_wan": None}
    with pytest.raises(ValueError, match="at least one"):
        validated_holding_input(**empty)
    with pytest.raises(ValueError, match="exceed 100"):
        validated_holding_input(
            **{**BASE, "ownership_pct": "101", "fragment_text": BASE["fragment_text"] + " 101"}
        )
    with pytest.raises(ValueError, match="future"):
        validated_holding_input(**{**BASE, "as_of": date(2099, 1, 1)})
