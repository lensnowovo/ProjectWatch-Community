"""Synthetic regressions for effect-first live findings; no paid calls."""
import json

import pytest
import test_studio_dsh_bootstrap as bootstrap_fixtures
import test_studio_dsh_monitor as monitor_fixtures

from project_watch.studio import events
from project_watch.studio import insight_generation as gen
from project_watch.studio.dsh_bootstrap import (
    RecordValidationError,
    monitoring_record,
    validate_record,
)


@pytest.fixture
def research(tmp_path):
    return bootstrap_fixtures.research.__wrapped__(tmp_path)


@pytest.fixture
def loop(tmp_path):
    return monitor_fixtures.loop.__wrapped__(tmp_path)


def validate(quote, text, label='合成记录'):
    validate_record({'id': 'S1', 'excerpt': quote, 'published_at': ''}, quote, text, label)


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


@pytest.mark.parametrize('text', [
    '李明（张华联合指导的第一位博士生导师）辞任董事。',
    '李明是张华联合指导的第一位博士生。',
    '张华是李明的博士生导师。',
])
def test_inverted_mentor_relation_is_rejected(text):
    with pytest.raises(RecordValidationError) as error:
        validate('李明辞任董事，张华系李明联合指导的第一位博士生。', text)
    assert any(i['code'] == 'person_relation_reversed' for i in error.value.issues)


@pytest.mark.parametrize('text', ['张华系李明联合指导的第一位博士生。', '李明是张华的博士生导师。', '李明辞任董事。'])
def test_correct_person_relation_and_unrelated_role_survive(text):
    validate('李明辞任董事，张华系李明联合指导的第一位博士生。', text)


@pytest.mark.parametrize('quote,text', [
    ('The trial enrolled 21 total participants worldwide.', '试验入组21人。'),
    ('The study enrolled 21 patients.', '研究入组21名患者。'),
    ('The study enrolled 21 participants.', '研究入组21位受试者。'),
])
def test_exact_english_people_counts_translate_without_losing_unit(quote, text):
    validate(quote, text)


@pytest.mark.parametrize('quote,text', [
    ('The study has 21 sites.', '研究入组21人。'),
    ('The study enrolled 21 participants.', '研究入组22人。'),
    ('The study did not enroll 21 participants.', '研究入组21人。'),
    ('The study enrolled up to 21 participants.', '研究入组21人。'),
    ('The study enrolled 21 participants.', '研究入组21例。'),
])
def test_people_translation_does_not_weaken_numbers_units_or_negation(quote, text):
    with pytest.raises(RecordValidationError):
        validate(quote, text)


def test_unbounded_global_verdict_returns_actionable_feedback():
    assert gen.judgment_issues({'position': {'summary': '获批、销量与植入例数全球领先', 'confidence': 'high'}})
    assert not gen.judgment_issues({'position': {'summary': '截至2025年注册获批进度较早，整体地位仍需比较'}})
    assert not gen.judgment_issues({'position': {'summary': '尚不能判断是否全球领先'}})


def test_intent_orders_need_explicit_scenario_not_projected_company_revenue():
    text = '在手和意向订单超80套，按80套及挂网价预计销售收入4000万元'
    assert gen.judgment_issues({'operations': {'for': [{'text': text}]}})
    assert not gen.judgment_issues({'operations': {'for': [{'text': '在手和意向订单超80套，实际确认收入待披露'}]}})


def test_quoted_peer_becomes_idempotent_long_term_relation(research):
    quote = 'Example Peer 开发激光模组，与本公司产品可比。'
    research.collectors.web_search = lambda *a, **kw: [{'url': 'https://example.com/peer', 'title': '合成竞对', 'snippet': quote}]
    research.tool('pw_search', {'query': 'Example Peer', 'category': 'competitor'})
    research.tool('pw_record', {'kind': 'peer', 'source_id': 'S1', 'quote': quote, 'text': quote,
        'label': 'Example Peer', 'reason': '同类产品，技术路径需分别比较'})
    for _ in range(2):
        research.state['finished'] = None
        result = research.tool('pw_finish', {'gaps': {}})
        assert result['monitoring_scope']['count'] == 1
    [watch] = research.pipeline.monitoring_plan.items(research.project_id)
    assert watch['scope_kind'] == 'relation' and watch['relation_type'] == 'competitor'
    assert watch['relation_target'] == 'Example Peer' and not watch['exit_rule']
    assert '直接' not in watch['reason'] and watch['queries']
    research.pipeline.monitoring_plan.update(watch['id'], {'status': 'retired'}, actor='human', reason='test')
    research.state['finished'] = None
    research.tool('pw_finish', {'gaps': {}})
    assert research.pipeline.monitoring_plan.items(research.project_id)[0]['status'] == 'retired'


def test_peer_subtitle_is_not_identity_and_unquoted_identity_does_not_project():
    row = {'kind': 'peer', 'label': '示例甲（非侵入式路线，替代路线竞争）',
           'reason': '不同技术路线，比较应用范围', 'refs': [{'quote': '示例甲开发合成产品。'}]}
    relation = monitoring_record(row)
    assert relation['relation_target'] == '示例甲'
    assert relation['relation_aliases'] == [] and relation['reason'] == row['reason']
    assert monitoring_record({**row, 'label': '另一家公司'}) is None
    assert monitoring_record({**row, 'label': '示例甲（中国）有限公司'}) is None
    assert monitoring_record({**row, 'reason': ''}) is None


def test_standing_peers_do_not_consume_question_section_cap(research):
    plan, pid = research.pipeline.monitoring_plan, research.project_id
    for i in range(5):
        plan.add(pid, {'text': f'合成竞对{i}', 'scope_kind': 'relation', 'relation_type': 'competitor',
            'relation_target': f'合成竞对{i}', 'reason': '同类产品', 'axis': 'peers',
            'sources': 'https://example.com/peers', 'queries': [f'合成竞对{i}'], 'tier': 'background'},
            actor='system', origin='bootstrap', visibility='public')
    for i in range(4):
        plan.add(pid, {'text': f'合成旧问题{i}', 'axis': 'peers'}, actor='system', origin='system')
    with pytest.raises(ValueError, match='4'):
        plan.add(pid, {'text': '第五个旧问题', 'axis': 'peers'}, actor='system', origin='system')
    assert len(plan.items(pid)) == 9


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


@pytest.mark.parametrize('quote,text', [
    ('Up to 21 participants enrolled.', '最多21人入组。'),
    ('At least 21 patients enrolled.', '至少21名患者入组。'),
    ('About 21 participants enrolled.', '约21人入组。'),
])
def test_people_count_bounds_preserved(quote, text):
    validate(quote, text)


def test_monitor_context_and_citations_keep_category(loop):
    loop.store.add_items(loop.project_id, 'policy', [{'key': 'synthetic-policy', 'title': '合成政策',
        'url': 'https://example.com/policy', 'source': 'Synthetic', 'published_at': '',
        'snippet': '地方公布合成政策。'}])
    item = loop.store.item_by_key(loop.project_id, 'synthetic-policy')
    loop.store.save_triage([{'id': item['id'], 'summary': '地方公布合成政策。', 'relevant': True}])
    loop.store.create_event(loop.project_id, {'title': '合成政策', 'category': 'policy', 'importance': 'routine'}, [item['id']])
    ctx = loop.tool('pw_context', {})
    code = next(c for c, e in ctx['numbered'].items() if e.get('url') == item['url'])
    assert gen._ref(code, ctx['numbered'])['label'] == '行业与政策'
    from project_watch.studio.dsh_bootstrap import ToolValidationError
    with pytest.raises(ToolValidationError) as error:
        loop.tool('pw_judge', {'analysis_json': json.dumps({'headline': '全球领先', 'position': {
            'summary': '全球领先', 'verdict': 'leading', 'for': [{'text': '合成政策', 'refs': [code]}]}})})
    assert str(error.value) == 'judgment_scope_validation'
    assert not loop.state.get('judged')
