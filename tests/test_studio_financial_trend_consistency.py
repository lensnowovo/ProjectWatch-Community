"""Synthetic annual-series guards; no real company receipts or services."""
from copy import deepcopy

import pytest
from test_studio_insight_qualifiers import answer, source

from project_watch.studio import insight_generation as gen


@pytest.mark.parametrize('text', [
    '收入连续增长：2022-2024年营收8000万、7000万、1.2亿元，2025年上半年5000万元。',
    '营收持续增长：2022至2024年营业收入分别为8000、7000、12000万元。',
    '2022—2024年度营业收入分别为0.8亿元、7000万元、1.2亿元，收入逐年增长。',
    '营收逐年下降：2022-2024年营收0.8亿元、9000万元、7000万元。',
])
def test_explicit_comparable_series_rejects_contradictory_trend(text):
    issues = gen.judgment_issues({'position': {'for': [{'text': text}]}})
    assert len(issues) == 1
    assert issues[0]['code'] == 'financial_series_trend_conflict'
    assert issues[0]['field'] == 'position.for.0.text'
    assert '逐期' in issues[0]['hint']


@pytest.mark.parametrize('text', [
    '收入连续增长：2022-2024年营收6000万、7000万、1.2亿元。',
    '营收逐年下降：2022-2024年营收1.2亿元、9000万元、7000万元。',
    '2022-2024年营收8000万、7000万、1.2亿元，2024年同比增长。',
    '并非收入连续增长：2022-2024年营收8000万、7000万、1.2亿元。',
    '无法判断收入连续增长：2022-2024年营收8000万、7000万、1.2亿元。',
    '尚不能认定收入连续增长：2022-2024年营收8000万、7000万、1.2亿元。',
    '若收入连续增长：2022-2024年营收8000万、7000万、1.2亿元。',
    '预计收入连续增长：2022-2024年营收8000万、7000万、1.2亿元。',
    '收入连续增长：2022-2024年净利润8000万、7000万、1.2亿元。',
    '收入连续增长：2022-2024年上半年营收8000万、7000万、1.2亿元。',
    '收入连续增长：2022-2024年营收8000万、7000万美元、1.2亿元。',
    '收入连续增长：2022-2024年营收8000万、7000万美元、12000万美元。',
    '收入连续增长：2022-2024年营收约8000万、7000万、1.2亿元。',
    '收入连续增长：2022-2024年营收8000万、7000万。',
    '甲公司收入连续增长，乙公司2022-2024年营收8000万、7000万、1.2亿元。',
    '收入连续增长：2022-2024年营收8000万、7000、1.2亿元。',
    '收入连续增长：2022-2024年营收8000万、7000万、1.2亿 美元。',
])
def test_incomparable_ambiguous_conditional_or_correct_series_not_guessed(text):
    assert not gen.judgment_issues({'summary': text})


def test_classic_omits_contradiction_and_downgrades_without_altering_input():
    text = '收入连续增长：2022-2024年营收8000万、7000万、1.2亿元'
    raw = answer('2024年收入增长', text)
    before = deepcopy(raw)
    out = gen.normalize(raw, {'N1': source(text)})
    assert raw == before
    assert out['operations']['for'] == []
    assert out['operations']['verdict'] == 'unclear'
    assert out['operations']['confidence'] == 'low'
    assert out['headline'] == ''
    assert any(c['reason'] == 'financial_series_trend_conflict' for c in out['consistency_checks'])
    assert any('逐期' in gap for gap in out['gaps'])


@pytest.mark.parametrize('text', [
    '甲公司2024年前三季度科研收入达1亿元，同期乙公司全年营收1.2亿元。',
    '甲公司2024年前三季度科研收入已突破亿元，同期乙公司全年营收1.2亿元。',
    '甲公司2024年上半年营业收入8000万元，同期乙公司2024年全年收入1亿元。',
    '甲公司2024年第四季度营收5000万元，同期乙公司下半年收入9000万元。',
])
def test_same_period_comparison_rejects_explicitly_different_financial_windows(text):
    issues = gen.judgment_issues({'position': {'against': [{'text': text}]}})
    assert len(issues) == 1
    assert issues[0]['code'] == 'financial_comparison_period_mismatch'
    assert issues[0]['field'] == 'position.against.0.text'
    assert '期间' in issues[0]['hint']


@pytest.mark.parametrize('text', [
    '甲公司2024年前三季度收入1亿元；乙公司2024年全年收入1.2亿元，期间不同不直接比较。',
    '甲公司2024年前三季度收入1亿元，同期乙公司2024年前三季度收入1.2亿元。',
    '2024年上半年收入1亿元，上年同期2023年上半年收入8000万元。',
    '甲公司2024年上半年收入1亿元，不能与同期乙公司全年收入1.2亿元直接比较。',
    '甲公司2024年前三季度取得许可，同期乙公司全年收入1.2亿元。',
    '甲公司2024年上半年收入1亿元，同期乙公司公布全年计划。',
    '甲公司2024年收入1亿元，同期乙公司全年收入1.2亿元。',
    '上半年收入1亿元，增长20%；同期另一公司全年收入1.2亿元，不能由此比较高低。',
])
def test_period_guard_preserves_qualified_or_incomparable_statements(text):
    assert not gen.judgment_issues({'summary': text})


def test_classic_period_conflict_keeps_explicit_gap_and_does_not_guess_period():
    text = '甲公司2024年前三季度营收1亿元，同期乙公司全年收入1.2亿元'
    raw = answer(text, text)
    out = gen.normalize(raw, {'N1': source(text)})
    assert out['operations']['verdict'] == 'unclear'
    assert out['operations']['for'] == []
    assert any('比较期间' in gap for gap in out['gaps'])
