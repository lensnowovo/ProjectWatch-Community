"""Synthetic regression for loss, per-share and disclosed financial ratios."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from project_watch.studio import finance
from project_watch.studio.normalize import normalize_amount, normalize_metric


def metric(name, value, *, period="2025", unit="万元", basis="actual", **extra):
    return normalize_metric({
        "name": name, "value": value, "period": period, "unit": unit,
        "basis": basis, "source_id": "synthetic-source", "source_name": "synthetic.txt",
        "locator": "line 1", **extra,
    })


@pytest.mark.parametrize("name", [
    "净亏损", "净虧損", "期内亏损", "期內虧損", "年内亏损", "年內虧損",
    "年度亏损", "本期亏损", "loss for the year", "loss for the period", "net loss",
])
@pytest.mark.parametrize("value", [12, -12, Decimal("12.00")])
def test_loss_names_project_negative_net_profit_without_rewriting_original(name, value):
    original = {"name": name, "value": value, "period": "2025", "unit": "万元"}
    result = normalize_metric(original)
    assert result["std_name"] == "净利润"
    assert result["value_norm"] == -12
    assert all(result[key] == val for key, val in original.items())
    assert normalize_metric(result) == result
    assert finance.key_numbers([metric(name, value)])["net_profit"]["value"] == -12


@pytest.mark.parametrize("name", ["归母净亏损", "歸母淨虧損"])
def test_owner_loss_keeps_its_scope(name):
    result = metric(name, 10)
    assert (result["std_name"], result["value_norm"], result["scope"]) == ("净利润", -10, "归母")
    assert finance.key_numbers([result])["net_profit"] is None


@pytest.mark.parametrize("name", ["营业收入", "净利润", "亏损减少额", "净亏损同比减少", "客户数"])
def test_quote_loss_is_not_a_sign_instruction_for_other_metrics(name):
    result = metric(name, 12, quote="集团净亏损12万元，但本指标按正数列示。")
    assert result["value_norm"] == 12


def test_operating_loss_is_not_net_profit():
    result = metric("经营亏损", 5)
    assert result["std_name"] == "营业利润" and result["value_norm"] == -5
    assert finance.key_numbers([result])["net_profit"] is None


@pytest.mark.parametrize("name", ["每股亏损", "每股虧損", "基本每股亏损", "稀释每股亏损", "basic loss per share"])
def test_per_share_loss_never_scales_like_total_currency(name):
    result = metric(name, 0.42, unit="元")
    assert result["value"] == 0.42 and result["unit"] == "元"
    assert result["value_norm"] == -0.42
    assert result["norm_unit"] == "元/股"
    assert result["currency"] == "CNY"
    assert finance.key_numbers([result])["net_profit"] is None


@pytest.mark.parametrize("unit", ["人民币元/股", "港元/股", "USD per share", "元/吨"])
def test_explicit_unit_denominators_are_preserved(unit):
    value, norm_unit, _currency = normalize_amount(Decimal("0.42"), unit)
    assert value == Decimal("0.42") and norm_unit == unit


def test_wrongly_named_per_share_amount_cannot_mask_total_profit():
    total = metric("净利润", -100, created_at="2026-01-01")
    per_share = metric("净利润", -0.42, unit="元/股", created_at="2026-02-01")
    assert finance.key_numbers([total, per_share])["net_profit"]["value"] == -100


def revenue_pair():
    return [metric("营业收入", 100, period="2024"), metric("营业收入", 104.8)]


def test_reported_actual_growth_wins_over_rounded_amount_recalculation():
    reported = metric("营业收入同比增长率", 4.7, unit="%", source_id="reported")
    rows = {row["name"]: row["cells"] for row in finance.ratios([*revenue_pair(), reported])}
    cell = rows["收入同比增长"]["2025"]
    assert cell["value"] == 4.7 and cell["formula"] == "资料直接给出"
    assert cell["refs"][0]["source_id"] == "reported" and len(cell["refs"]) == 1
    assert finance.key_numbers([*revenue_pair(), reported])["revenue_growth"] == 4.7


@pytest.mark.parametrize("extra", [
    {"basis": "forecast"}, {"basis": "unknown"}, {"scope": "某子公司"},
    {"period": "2025H1"}, {"unit": "万元"},
])
def test_incompatible_reported_growth_cannot_override_actuals(extra):
    reported = metric("营业收入同比增长率", 99, **({"unit": "%"} | extra))
    rows = {row["name"]: row["cells"] for row in finance.ratios([*revenue_pair(), reported])}
    assert rows["收入同比增长"]["2025"]["value"] == 4.8
    assert finance.key_numbers([*revenue_pair(), reported])["revenue_growth"] == 4.8


def test_reported_margin_keeps_disclosed_precision():
    rows = finance.ratios([metric("营业收入", 100), metric("毛利", 31.2),
                           metric("毛利率", 31.17, unit="%", source_id="margin")])
    cell = next(row for row in rows if row["name"] == "毛利率")["cells"]["2025"]
    assert cell["value"] == 31.17 and cell["refs"][0]["source_id"] == "margin"


def test_disclosed_growth_does_not_require_prior_total():
    metrics = [metric("营业收入", 100), metric("收入同比增长", 5.1, unit="%")]
    assert finance.key_numbers(metrics)["revenue_growth"] == 5.1


@pytest.mark.parametrize("name", ["每股收益", "基本每股收益", "稀释每股收益", "earnings per share"])
def test_per_share_profit_keeps_positive_raw_numerator(name):
    result = metric(name, Decimal("0.125"), unit="港元")
    assert result["value_norm"] == Decimal("0.125")
    assert result["norm_unit"] == "港元/股" and result["currency"] == "HKD"


def test_loss_percentage_is_not_a_negative_money_amount():
    result = metric("净亏损", 12, unit="%")
    assert result["value_norm"] == 12 and finance.key_numbers([result])["net_profit"] is None


def test_bare_chinese_loss_scale_uses_the_known_financial_metric_not_guessing():
    loss = metric("期内亏损", Decimal("0.12"), unit="亿")
    assert loss["value_norm"] == -1200 and loss["norm_unit"] == "万元"
    assert metric("客户数", 2, unit="亿")["norm_unit"] == "亿"


def test_foreign_reported_growth_cannot_mask_domestic_actuals():
    metrics = [*revenue_pair(), metric("营业收入同比增长率", 99, unit="%", currency="USD")]
    metrics[-1]["currency"] = "USD"  # Simulate an already scoped read projection.
    assert finance.key_numbers(metrics)["revenue_growth"] == 4.8


def test_reported_forecast_margin_does_not_replace_computed_actual_margin():
    metrics = [metric("营业收入", 100), metric("毛利", 30),
               metric("毛利率", 80, unit="%", basis="forecast")]
    cell = next(row for row in finance.ratios(metrics) if row["name"] == "毛利率")["cells"]["2025"]
    assert cell["value"] == 30 and cell["basis"] == "actual"


def test_normal_store_reads_reproject_legacy_rows_without_rewriting_raw_fields(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    path = tmp_path / "synthetic.sqlite3"
    repo = ProjectRepository(path)
    project = repo.create_project("合成财务测试")
    source = repo.add_source(project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64)
    store = StudioStore(path)
    values = [metric("期内亏损", 30), metric("基本每股亏损", 0.3, unit="元"),
              *revenue_pair(), metric("收入同比增长", 4.7, unit="%")]
    for value in values:
        value.update(source_id=source.id, quote="合成财务科目原文", locator="line 1")
        # Persist old/wrong derived fields to prove legacy read repair, not just
        # storing already-correct outputs from the new helper.
        value.update(std_name=value["name"], value_norm=value["value"], norm_unit=value["unit"])
    store.replace_metrics(project.id, [source.id], values)
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT * FROM studio_metrics ORDER BY id").fetchall()
    projected = StudioStore(path).list_metrics(project.id)
    keys = finance.key_numbers(projected)
    assert keys["net_profit"]["value"] == -30 and keys["revenue_growth"] == 4.7
    eps = next(row for row in projected if row["name"] == "基本每股亏损")
    assert eps["value"] == 0.3 and eps["value_norm"] == -0.3 and eps["norm_unit"] == "元/股"
    assert eps["quote"] == "合成财务科目原文" and eps["source_id"] == source.id
    assert StudioStore(path).list_metrics(project.id) == projected
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM studio_metrics ORDER BY id").fetchall() == original
