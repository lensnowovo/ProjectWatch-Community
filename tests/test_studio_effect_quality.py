"""Synthetic regressions for effect-first live findings; no paid calls."""
import json

from project_watch.studio import events
from project_watch.studio import insight_generation as gen


def test_collection_clock_is_only_for_grouping_not_event_occurrence():
    row = {'category': 'company', 'title': '合成公告', 'summary': '旧材料',
           'collected_at': '2026-10-20T10:00:00+08:00', 'published_at': ''}
    groups, _ = events.plan_groups([row], [])
    assert events.item_time(row) == row['collected_at']
    assert events.summarise(groups[0])['event_at'] is None
    row['published_at'] = '2025-01-02'
    groups, _ = events.plan_groups([row], [])
    assert events.summarise(groups[0])['event_at'] == '2025-01-02'


def test_legacy_collection_fallback_is_not_given_to_judgment_as_today():
    stamp = '2026-10-20T10:01:02+08:00'
    row = {'title': '旧材料', 'collected_at': stamp, 'published_at': '', 'url': 'https://example.com/a'}
    entry = gen._public_entry({'title': '旧材料', 'event_at': stamp, 'sources': [row]}, '公司新闻', row)
    assert entry['date'] == entry['event_at'] == ''
    assert entry['collected_at'] == '2026-10-20'
    # A known day is not discarded simply because collected on the same day.
    entry = gen._public_entry({'event_at': '2026-10-20'}, '公司新闻', row)
    assert entry['event_at'] == '2026-10-20'










def test_unbounded_global_verdict_returns_actionable_feedback():
    assert gen.judgment_issues({'position': {'summary': '获批、销量与植入例数全球领先', 'confidence': 'high'}})
    assert not gen.judgment_issues({'position': {'summary': '截至2025年注册获批进度较早，整体地位仍需比较'}})
    assert not gen.judgment_issues({'position': {'summary': '尚不能判断是否全球领先'}})


def test_intent_orders_need_explicit_scenario_not_projected_company_revenue():
    text = '在手和意向订单超80套，按80套及挂网价预计销售收入4000万元'
    assert gen.judgment_issues({'operations': {'for': [{'text': text}]}})
    assert not gen.judgment_issues({'operations': {'for': [{'text': '在手和意向订单超80套，实际确认收入待披露'}]}})








def test_scope_fallback_retains_input_and_does_not_assert_a_replacement():
    raw = {'headline': '整体领先', 'position': {'verdict': 'leading', 'confidence': 'high', 'summary': '获批和销量全球领先'}}
    original = json.dumps(raw)
    result = gen.normalize(raw, {})
    assert json.dumps(raw) == original
    assert result['position']['verdict'] == 'unclear'
    assert result['position']['confidence'] == 'low'
    assert result['headline'] == ''
    assert result['consistency_checks'] and result['gaps']
    assert not gen.judgment_issues({'summary': '截至2025年在已披露可比样本中，注册获批进度全球领先'})


def test_explicit_intent_scenario_is_not_blocked():
    assert not gen.judgment_issues({'summary': '假设意向订单全部转化，按挂网价估算收入，挂网价不等于出厂价，不代表已实现收入'})






def test_an_unbounded_comparison_in_one_point_does_not_erase_the_summary():
    numbered = {'N1': {'kind': 'public', 'text': '同行获全球挑战赛第一。公司完成种子轮。', 'context': '同行获全球挑战赛第一。公司完成种子轮。',
                       'context_kind': '原文摘录', 'url': 'https://example.invalid/a'}}
    raw = {'position': {'verdict': 'behind', 'confidence': 'medium', 'summary': '公司刚完成种子轮，规模落后于同行',
                        'for': [{'text': '公司完成种子轮', 'refs': ['N1']},
                                {'text': '同行获全球挑战赛第一', 'refs': ['N1']}], 'against': []}}
    out = gen.normalize(raw, numbered)['position']
    assert out['summary'] == '公司刚完成种子轮，规模落后于同行'
    assert out['verdict'] == 'behind' and out['confidence'] == 'low'
    assert [p['text'] for p in out['for']] == ['公司完成种子轮']
