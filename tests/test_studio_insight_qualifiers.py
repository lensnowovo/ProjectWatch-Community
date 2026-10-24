"""Synthetic disclosure/order regressions; no live data or provider calls."""
from copy import deepcopy

import pytest

from project_watch.studio import insight_generation as gen


def source(body, **extra):
    return {'kind': 'public', 'text': body, 'context': body,
            'context_kind': '原文摘录', 'url': 'https://example.invalid/report', **extra}


def answer(summary, point, refs=None):
    return {'headline': summary, 'operations': {'verdict': 'improving', 'summary': summary,
            'for': [{'text': point, 'refs': refs or ['N1']}]}}


def test_unknown_recognition_is_not_a_negative_accounting_fact():
    numbered = {'N1': source('2026年上半年植入产品未确认收入。9月已销售12套。后续收入未披露。',
                            text='植入产品收入尚未确认')}
    raw = answer('植入产品收入尚未确认，商业化需要观察', '9月已销售12套')
    before = deepcopy(raw)
    out = gen.normalize(raw, numbered)
    assert '收入尚未确认' not in out['headline']
    assert '确认情况待核实' in out['headline']
    assert '确认情况待核实' in out['operations']['summary']
    assert out['operations']['verdict'] == 'unclear'
    assert raw == before
    assert any(c['reason'] == 'recognition_not_supported' for c in out['qualifier_checks'])


@pytest.mark.parametrize('claim', ['2026年上半年植入产品未确认收入', '2026年9月公司产品收入尚未确认'])
def test_explicit_same_period_negative_is_preserved(claim):
    out = gen.normalize(answer(claim, claim), {'N1': source(claim + '。')})
    assert out['headline'] == claim
    assert out['operations']['summary'] == claim
    assert out['operations']['for'][0]['text'] == claim


@pytest.mark.parametrize('claim', ['后续收入未披露', '缺少收入确认数据', '尚未确认收入是否达到预期'])
def test_disclosure_and_question_wording_is_not_reclassified(claim):
    out = gen.normalize(answer(claim, '产品已上市'), {'N1': source('产品已上市。')})
    assert out['headline'] == claim


def test_unsafe_negative_point_is_not_used_as_a_verdict_premise():
    out = gen.normalize(answer('公司承压', '产品收入尚未确认'), {'N1': source('后续收入未披露。')})
    assert not out['operations']['for']
    assert out['operations']['verdict'] == 'unclear'
    assert not out['operations']['summary']
    assert out['gaps']


def test_summary_and_headline_restore_intent_without_converting_to_signed_orders():
    numbered = {'N1': source('截至2026年9月，在手和意向订单合计超过70套。已售12套。')}
    out = gen.normalize(answer('产品上市、订单超70套、销售增长', '在手及意向订单超70套'), numbered)
    for text in (out['headline'], out['operations']['summary']):
        assert '在手及意向订单超70套' in text
        assert '已签' not in text
        assert '销售增长' in text


def test_order_same_number_different_unit_or_relation_is_not_transplanted():
    for claim in ('订单70套', '订单超过70万元', '订单超80套', '甲产品订单超70套'):
        out = gen.normalize(answer(claim, '甲产品有销售进展'), {
            'N1': source('乙产品在手及意向订单超70套。')})
        assert '甲产品在手及意向' not in out['headline']
        assert out['headline'] != '在手及意向' + claim


def test_uncited_competitor_orders_cannot_qualify_company_summary():
    out = gen.normalize(answer('订单超70套', '公司已签订单超70套'), {
        'N1': source('公司已签订单超70套。'), 'C1': source('竞品意向订单超70套。')})
    assert out['operations']['summary'] == '订单超70套'
    assert out['headline'] == '订单超70套'


def test_conflicting_scopes_are_not_combined_by_number():
    out = gen.normalize(answer('订单超70套', '公司有两条产品线', ['N1', 'N2']), {
        'N1': source('甲产品在手及意向订单超70套。'), 'N2': source('乙产品已签订单超70套。')})
    assert '在手及意向订单超70套' not in out['operations']['summary']
    assert '待核实' in out['operations']['summary']


def test_source_body_wins_over_lossy_source_summary():
    numbered = {'N1': source('在手及意向订单合计超过70套。', text='订单超70套')}
    out = gen.normalize(answer('订单超70套', '在手及意向订单超70套'), numbered)
    assert out['headline'] == '在手及意向订单超70套'


def test_already_qualified_order_is_preserved():
    text = '在手及意向订单合计超过70套'
    out = gen.normalize(answer(text, text), {'N1': source(text)})
    assert out['operations']['summary'] == text


def test_input_order_context_is_not_lost_behind_long_report():
    body = '合成公司资料。' * 400 + '在手及意向订单合计超过70套。' + '附录。' * 400
    assert '在手及意向订单合计超过70套' in gen._context(body)


def test_rejected_premise_invalidates_summary_even_with_other_valid_evidence():
    raw = answer('由于没有确认收入，公司承压', '产品收入尚未确认')
    raw['operations']['for'].append({'text': '产品已上市', 'refs': ['N1']})
    out = gen.normalize(raw, {'N1': source('产品已上市。后续收入未披露。')})
    assert out['operations']['for'][0]['text'] == '产品已上市'
    assert out['operations']['summary'] == out['headline'] == ''
    assert out['operations']['verdict'] == 'unclear'


def test_search_summary_cannot_confirm_negative_accounting_fact():
    claim = '2026年上半年产品未确认收入'
    out = gen.normalize(answer(claim, '产品已上市'), {
        'N1': source(claim, context_kind='搜索摘要')})
    assert '确认情况待核实' in out['headline']


def test_cross_section_sources_do_not_repair_unrelated_summary():
    raw = answer('订单超70套', '甲产品进展', ['N1'])
    raw['position'] = {'for': [{'text': '乙产品意向订单超70套', 'refs': ['C1']}]}
    out = gen.normalize(raw, {'N1': source('甲产品已签订单超70套。'),
                              'C1': source('乙产品意向订单超70套。')})
    assert out['operations']['summary'] == '订单超70套'


def test_no_new_calls_or_unstable_fingerprint_after_guarded_save(tmp_path):
    from test_studio_insight import Model, _setup

    from project_watch.studio.insight import load

    project, store, pipeline = _setup(tmp_path)
    gen.run(pipeline, Model(answer('产品收入尚未确认', '产品已上市', ['D1'])), project.id)
    assert '确认情况待核实' in load(store, project.id)['analysis']['headline']
    assert gen.due(pipeline, project.id) is None
    assert not store.jobs(project.id)
