"""Synthetic regressions derived from receipt shapes, no real materials/network."""

import pytest

from project_watch.studio.questions import QuestionLedger


def match(body, *, scope='question', **extra):
    watch = {'scope_kind': scope, 'relation_type': 'policy', 'axis': 'rules',
             'relation_target': '合成接口相关价格项目', 'relation_aliases': [],
             'text': '合成接口是否进入基本医保支付范围？'}
    return QuestionLedger.public_row_match(watch, {}, {'body_status': 'search_summary', 'body': body, **extra})


@pytest.mark.parametrize('body', [
    '某省公布合成接口等医疗服务价格项目，明确医保支付类别。',
    '合成接口收费路径逐渐清晰，部分服务纳入医保支付。',
    '合成接口已纳入乙类医保，仍有先行自付条件。',
    '合成接口的医保支付范围尚未确认。',
])
def test_policy_wording_routes_only_source_link(body):
    found = match(body)
    assert found and found['basis'] == 'search_summary'
    assert found['excerpt'] in body
    assert found['matched_text'] in found['excerpt']


@pytest.mark.parametrize('body', [
    '合成接口获得融资，另一治疗纳入医保支付。',
    '合成接口行业增长。另一治疗收费项目纳入医保。',
    '合成接口。收费路径逐渐清晰，医保支付尚待落地。',
    '其他接口已纳入乙类医保。',
])
def test_policy_wording_does_not_broaden_to_sector_cooccurrence(body):
    assert not match(body)


def test_structural_link_not_from_generated_summary():
    assert not match('', summary='合成接口已纳入乙类医保。')
    assert not match('合成接口已纳入乙类医保。', body_status='unavailable')










