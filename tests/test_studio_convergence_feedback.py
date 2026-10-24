"""Synthetic convergence regressions: no model, search or private inputs."""
import json

import pytest
import test_studio_dsh_monitor as fixtures
from test_studio_dsh_monitor import collect

from project_watch.studio.dsh_bootstrap import ToolValidationError
from project_watch.studio.questions import QuestionLedger


@pytest.fixture
def loop(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path)


def test_bad_references_report_exact_ids_and_all_numeric_issues(loop):
    watch, _, ref = collect(loop)
    before = loop.pending_assessments(loop.frozen())
    with pytest.raises(ToolValidationError) as caught:
        loop.tool('pw_assess', {'watch_id': watch['id'], 'refs': [ref, 'W999', 'N999'],
            'status': 'unchanged', 'change_basis': 'evidence_only', 'note': '材料声称销售99套'})
    issues = caught.value.issues
    route = next(i for i in issues if i['field'] == 'refs')
    assert route['invalid_refs'] == ['W999', 'N999']
    assert route['accepted_refs'] == [ref]
    assert any(i['code'] == 'unsupported_numbers' and '99套' in i['unsupported'] for i in issues)
    assert loop.pending_assessments(loop.frozen()) == before
    assert not loop.state.get('assessments')


def test_prefix_typo_suggestion_is_not_automatic_rebinding(loop):
    watch, ctx, ref = collect(loop)
    # Only an advisory alternative within this same context/topic is allowed.
    wrong = ('I' if ref[0] != 'I' else 'N') + ref[1:]
    with pytest.raises(ToolValidationError) as caught:
        loop.cited_events(ctx, {'refs': [wrong]}, watch)
    assert caught.value.issues[0]['suggested_refs'][wrong] == [ref]
    assert not loop.state.get('assessments')


@pytest.mark.parametrize(('text', 'expected'), [
    ('合成器械进入创新医疗器械绿色通道', True),
    ('合成器械进入创新医疗器械特别审查程序', True),
    ('合成器械未进入创新医疗器械绿色通道', True),
    ('合成器械开通患者就医绿色通道', False),
    ('合成器械完成融资', False),
    ('其他设备进入创新医疗器械绿色通道', False),
])
def test_registration_focus_includes_specific_regulatory_channel_only(text, expected):
    watch = {'scope_kind':'question', 'relation_target':'合成器械', 'relation_aliases':[],
             'text':'合成器械注册进度', 'label':'注册进度', 'queries':['合成器械 注册']}
    assert bool(QuestionLedger.public_row_match(watch, {}, {'title':text})) is expected


def test_number_feedback_preserves_source_units_and_no_inferred_currency(loop):
    watch, ctx, ref = collect(loop)
    event_id = ctx['numbered'][ref]['id']
    with loop.store._connect() as db, db:
        db.execute('UPDATE studio_items SET snippet=?,body=? WHERE event_id=?',
                   ('合成器械植入8套，支付上限15万。', '合成器械植入8套，支付上限15万。', event_id))
    ctx = loop.tool('pw_context', {})
    ref = next(r for r,e in ctx['numbered'].items() if e.get('id') == event_id)
    with pytest.raises(ToolValidationError) as caught:
        loop.tool('pw_assess', {'watch_id':watch['id'], 'refs':[ref], 'status':'unchanged',
            'change_basis':'evidence_only', 'note':'植入8例，支付上限15万元'})
    issue = next(i for i in caught.value.issues if i['code'] == 'unsupported_numbers')
    assert issue['unsupported'] == ['15万元', '8例']
    assert any('8套' in e['quote'] and '15万' in e['quote'] for e in issue['source_examples'])
    assert all(e['ref'] == ref for e in issue['source_examples'])
    assert not loop.pipeline.questions._unsupported_numbers(loop.project_id, '植入8套，支付上限15万', [event_id])


def test_revision_reports_route_and_numbers_together_before_any_write(loop):
    watch, ctx, ref = collect(loop)
    before = loop.plan._get(loop.project_id, watch['id'])
    rows = [{'watch_id': watch['id'], 'action': 'note', 'refs': [ref, 'W999'],
             'why': '补充材料', 'text': '销售99套'}]
    with pytest.raises(ToolValidationError) as caught:
        loop.validate_revisions(rows, ctx)
    assert {i['field'] for i in caught.value.issues} == {'changes[0].refs', 'changes[0].text'}
    assert caught.value.feedback()['applied'] == 0
    assert loop.plan._get(loop.project_id, watch['id']) == before


def test_round_reports_outputs_and_real_gaps_despite_optimistic_agent_reason(loop):
    _watch, _, ref = collect(loop)
    fixtures.judge(loop, ref)
    loop.tool('pw_revise', {'changes_json': '[]', 'reason': '保留范围'})
    loop.state['errors'].append({'tool': 'pw_read', 'error': 'synthetic_source_unavailable'})
    result = loop.tool('pw_finish', {'reason': '全部完成，无缺口'})
    summary = result['round_summary']
    assert summary['outputs']['saved_entries'] == len(loop.state['records'])
    assert summary['outputs']['revision_applied'] == 0
    assert summary['stop_reason'] != '全部完成，无缺口'
    assert any('来源' in gap for gap in summary['gaps'])
    assert summary['active'] == 1 and summary['coverage'] == 'partial'
    assert not summary['stop_details']  # An active branch must not be called stopped.


def test_unfulfilled_finish_and_missing_exit_are_visible_gaps(loop):
    watch, _, ref = collect(loop)
    loop.plan.update(watch['id'], {'exit_rule': ''}, actor='system', reason='legacy')
    loop.tool('pw_context', {})
    fixtures.judge(loop, ref)
    loop.tool('pw_revise', {'changes_json': json.dumps([{'watch_id':watch['id'], 'action':'finish',
        'text':'激光模组取得许可', 'why':'问题已回答', 'refs':[ref]}]), 'reason':'本轮结束'})
    summary = loop.tool('pw_finish', {'reason':'已结束'})['round_summary']
    assert summary['outputs']['revision_dropped'] == 1
    for marker in ('修订未执行', '结束修订尚未落实', '缺少结束条件'):
        assert any(marker in g for g in summary['gaps'])


def test_specific_registration_channel_wakes_stopped_branch_once(loop):
    watch, _, ref = collect(loop)
    fixtures.judge(loop, ref)
    loop.tool('pw_revise', {'changes_json': json.dumps([{'watch_id':watch['id'], 'action':'finish',
        'text':'激光模组取得许可', 'why':'问题已回答', 'refs':[ref]}]), 'reason':'本轮结束'})
    first = loop.tool('pw_finish', {'reason':'本轮已回答'})['explorations'][watch['id']]
    assert first['state'] == 'stopped'
    fixtures.add_public_event(loop, 'finance-only', '合成公司激光模组完成融资')
    fixtures.add_public_event(loop, 'patient-channel', '合成公司激光模组开通就医绿色通道')
    loop.refresh()
    assert loop.plan.explorations.view(loop.project_id)[watch['id']]['state'] == 'stopped'
    fixtures.add_public_event(loop, 'regulatory-channel', '合成公司激光模组进入创新医疗器械绿色通道')
    loop.refresh()
    second = loop.plan.explorations.view(loop.project_id)[watch['id']]
    assert second['reason'] == 'new_evidence' and second['generation'] == first['generation'] + 1
    loop.refresh()
    assert loop.plan.explorations.view(loop.project_id)[watch['id']]['generation'] == second['generation']
