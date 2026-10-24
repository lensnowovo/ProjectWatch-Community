"""Synthetic headline-period and final-verdict consistency regressions."""
from copy import deepcopy

import pytest
from test_studio_insight_qualifiers import answer, source

from project_watch.studio import insight_generation as gen


@pytest.mark.parametrize('text', ['收入小', '亏损扩', '营收规模小', '利润降', '毛利率升'])
def test_compressed_financial_headline_requires_its_own_period(text):
    headline, checks = gen._period_headline('产品已获批；' + text)
    assert headline == '产品已获批；财务变化需核对报告期'
    assert checks[0]['reason'] == 'financial_period_missing'
    # Explicit periods survive; a product-approval year is not an accounting period.
    scoped = '2026年上半年' + text
    assert gen._period_headline(scoped) == (scoped, [])
    assert gen._period_headline('2026年产品获批后' + text)[1]


def test_short_trend_guard_does_not_match_unrelated_financial_words():
    for text in ('收入确认情况待核实', '扩产计划仍待核实；产品体积小'):
        assert gen._period_headline(text) == (text, [])


def test_missing_headline_period_does_not_borrow_a_publication_date():
    raw = answer('首证与商业化起步，但收入仍靠科研设备、亏损扩大，支付覆盖仍需跟踪', '产品已上市')
    before = deepcopy(raw)
    out = gen.normalize(raw, {'N1': source('2025年收入主要来自科研设备，2026年上半年亏损扩大。',
                                         published_at='2026-10-20')})
    assert '收入仍靠' not in out['headline'] and '亏损扩大' not in out['headline']
    assert '首证与商业化起步' in out['headline'] and '支付覆盖仍需跟踪' in out['headline']
    assert '报告期' in out['headline']
    assert '2026' not in out['headline'] and '2025' not in out['headline']
    assert any(c['reason'] == 'financial_period_missing' for c in out['consistency_checks'])
    assert raw == before


@pytest.mark.parametrize('text', [
    '2026年上半年收入主要来自科研设备', '报告期内（具体期间未披露）亏损扩大',
    '2025年度亏损扩大；2026年9月完成首销', '2026年上半年，收入主要来自科研设备',
])
def test_explicit_financial_period_is_preserved(text):
    out = gen.normalize(answer(text, '产品已上市'), {'N1': source('产品已上市。')})
    assert out['headline'] == text


@pytest.mark.parametrize('text', [
    '2026年9月完成首销，但收入仍靠科研设备',
    '2026年9月获批后收入仍靠科研设备',
    '2026年10月报道亏损扩大',
    '收入仍靠科研设备，2026年9月已售12套',
    '收入仍靠科研设备而2026年9月已售12套',
    '2026年9月，完成首销后收入仍靠科研设备',
])
def test_event_or_publication_period_does_not_qualify_financial_claim(text):
    out = gen.normalize(answer(text, '产品已上市'), {'N1': source('产品已上市。')})
    assert '收入仍靠' not in out['headline'] and '亏损扩大' not in out['headline']


def test_downgraded_operating_summary_drops_direction_but_keeps_evidence():
    raw = answer('产品上市，但侵入式收入尚未确认，改善与承压并存、整体略向好', '2026年9月已售12套')
    out = gen.normalize(raw, {'N1': source('2026年上半年侵入式未确认收入。2026年9月已售12套。')})
    part = out['operations']
    assert part['verdict'] == 'unclear' and part['confidence'] == 'low'
    assert '确认情况待核实' in part['summary'] and '无法判断' in part['summary']
    assert '向好' not in part['summary'] and '承压' not in part['summary']
    assert part['for'][0]['text'] == '2026年9月已售12套'
    assert any(c['reason'] == 'summary_verdict_downgraded' for c in out['consistency_checks'])


@pytest.mark.parametrize(('verdict', 'summary'), [
    ('unclear', '整体略向好'), ('pressured', '整体向好'),
    ('improving', '整体承压'), ('steady', '经营趋势改善'),
])
def test_explicit_operating_summary_label_conflict_is_not_resolved_by_guessing(verdict, summary):
    raw = answer(summary, '产品已上市')
    raw['operations']['verdict'] = verdict
    out = gen.normalize(raw, {'N1': source('产品已上市。')})
    assert out['operations']['verdict'] == 'unclear'
    assert out['operations']['confidence'] == 'low'
    assert '无法判断' in out['operations']['summary']
    assert out['operations']['summary'] != summary
    assert any(c['reason'] == 'summary_verdict_conflict' for c in out['consistency_checks'])


def test_partial_positive_signal_is_not_an_overall_verdict():
    raw = answer('产品进展向好，但现金情况待核实，无法判断整体经营趋势', '产品已上市')
    raw['operations']['verdict'] = 'unclear'
    out = gen.normalize(raw, {'N1': source('产品已上市。')})
    assert out['operations']['summary'] == raw['operations']['summary']


def test_consistent_direction_and_intent_orders_are_preserved():
    text = '在手及意向订单超70套，整体向好'
    out = gen.normalize(answer(text, '在手及意向订单超70套'), {'N1': source(text)})
    assert out['operations']['verdict'] == 'improving'
    assert out['operations']['summary'] == text


def test_new_guard_does_not_convert_missing_income_into_zero_or_a_negative_fact():
    text = '后续收入确认情况待核实；在手及意向订单超70套'
    out = gen.normalize(answer(text, text), {'N1': source(text)})
    assert out['headline'] == text
    assert not out['consistency_checks']


def test_financial_period_at_tail_is_not_lost_by_character_clipping():
    text = '本公司' + '合成产品' * 20 + '收入主要来自科研设备（2026年上半年）'
    out = gen.normalize(answer(text, '产品已上市'), {'N1': source('产品已上市。')})
    assert '收入主要来自' not in out['headline'] or '2026年上半年' in out['headline']
