"""Presentation state and the fate of one unverified caveat. Synthetic records only."""
from test_section_analysis import apply, host, part  # noqa: F401  (host is a pytest fixture)

from project_watch.studio import insight_generation as gen


def source(body, **extra):
    return {'kind': 'public', 'text': body, 'context': body, 'context_kind': '原文摘录',
            'url': 'https://example.invalid/report', **extra}


POSITION = {
    'headline': '获批领先，但收入确认待核实',
    'position': {
        'verdict': 'ahead', 'confidence': 'low',
        'summary': '截至2026年，NEO-ONE为全球首款获批的侵入式脑机接口器械，领先仍处注册临床的同行；但侵入式尚未确认收入。',
        'for': [{'text': '2026年3月NEO-ONE获批上市', 'refs': ['N1']}],
        'against': [{'text': '侵入式产品尚未确认收入', 'refs': ['N1']}],
        'change_if': ['出现同口径销售收入'],
    },
}
SOURCES = {'N1': source('2026年3月NEO-ONE获批上市。2026年上半年营收4634.4万元。')}


def test_caveat_is_marked_but_the_model_verdict_stands():
    """The recorded failure: model said ahead, page said 无法判断."""
    p = gen.normalize(POSITION, SOURCES)['position']
    assert p['verdict'] == 'ahead' and p['verdict_source'] == 'model'
    assert p['state'] == 'judged_annotated'
    assert p['reason_codes'] == ['recognition_not_supported']
    assert p['annotations'] == [{'span': '收入确认情况待核实', 'code': 'recognition_not_supported',
                                 'original': '尚未确认收入'}]
    assert p['summary'].endswith('但侵入式收入确认情况待核实。')
    # The standalone point is still dropped: it asserts a negative accounting fact no source states.
    assert p['evidence_counts'] == {'support': 1, 'oppose': 0, 'sources': 1}


def test_leading_negative_claim_still_invalidates_the_conclusion():
    """Same wording at the head of a summary is the premise, not a caveat."""
    raw = {'operations': {'verdict': 'improving', 'confidence': 'high',
                          'summary': '尚未确认收入，商业化需要观察',
                          'for': [{'text': '2026年9月已售12套', 'refs': ['N1']}]}}
    p = gen.normalize(raw, {'N1': source('2026年9月已售12套。')})['operations']
    assert p['verdict'] == 'unclear' and p['verdict_source'] == 'policy'
    assert p['state'] == 'undetermined'
    assert 'recognition_not_supported' in p['reason_codes']


def test_dropped_point_only_invalidates_a_summary_that_leads_with_it():
    """A caveat after 但 may be mentioned in the summary; a leading premise may not."""
    raw = {'operations': {'verdict': 'improving', 'confidence': 'high',
                          'summary': '由于没有确认收入，公司经营承压',
                          'for': [{'text': '产品收入尚未确认', 'refs': ['N1']},
                                  {'text': '产品已上市', 'refs': ['N1']}]}}
    p = gen.normalize(raw, {'N1': source('产品已上市。后续收入未披露。')})['operations']
    assert p['verdict'] == 'unclear' and p['state'] == 'undetermined'
    assert p['summary'] == ''


def test_contradictory_overall_direction_still_falls():
    raw = {'operations': {'verdict': 'improving', 'confidence': 'high',
                          'summary': '订单增长，整体承压',
                          'for': [{'text': '2026年9月已售12套', 'refs': ['N1']}]}}
    p = gen.normalize(raw, {'N1': source('2026年9月已售12套。')})['operations']
    assert p['verdict'] == 'unclear' and p['state'] == 'undetermined'
    assert 'summary_verdict_conflict' in p['reason_codes']


def test_section_without_supported_items_is_unavailable():
    raw = {'industry': {'verdict': 'growing', 'confidence': 'high', 'summary': '赛道扩张。',
                        'drivers': [{'text': '政策支持', 'refs': ['X9']}]}}
    p = gen.normalize(raw, {'N1': source('无关材料。')})['industry']
    assert p['verdict'] == 'unclear' and p['verdict_source'] == 'unavailable'
    assert p['state'] == 'undetermined'
    assert p['evidence_counts'] == {'support': 0, 'oppose': 0, 'sources': 0}


def test_published_section_is_judged(host):  # noqa: F811  (pytest fixture)
    result, view = apply(host, dict(operations=part()))
    assert result['published'] == ['operations']
    assert view['analysis']['operations']['state'] == 'judged'
    assert view['analysis']['operations']['verdict_source'] == 'model'


def test_unpublished_section_is_absent_not_undetermined(host):  # noqa: F811  (pytest fixture)
    """The empty template must not look like a model that chose "undetermined"."""
    _, view = apply(host, dict(operations=part()))
    chain = view['analysis']['chain']
    assert chain['state'] == 'absent'
    assert chain['verdict_source'] == 'unavailable'
    assert chain['reason_codes'] == ['never_published']
    assert chain['summary'] == ''
