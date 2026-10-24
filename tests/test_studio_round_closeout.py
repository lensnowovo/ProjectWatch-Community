"""Synthetic offline round-closeout regressions; no external services."""
import json

import pytest
import test_studio_dsh_monitor as fixtures
from test_studio_dsh_monitor import collect, judge

from project_watch.studio.dsh_bootstrap import ToolValidationError
from project_watch.studio.dsh_runtime import compact_context_messages


@pytest.fixture
def loop(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path)


def test_evidence_only_is_not_object_progress(loop):
    watch, _, ref = collect(loop)
    result = loop.tool('pw_assess', {'watch_id': watch['id'], 'refs': [ref],
        'status': 'progress', 'change_basis': 'evidence_only', 'note': '新材料补齐，原判断不变'})
    assert result['effective_status'] == 'unchanged'
    with loop.store._connect() as db:
        row = db.execute('SELECT status FROM studio_question_assessments ORDER BY rowid DESC LIMIT 1').fetchone()
    assert row[0] == 'unchanged'
    assert loop.state['assessments'][-1]['submitted_status'] == 'progress'


def test_directional_assessment_requires_change_basis(loop):
    watch, _, ref = collect(loop)
    with pytest.raises(ToolValidationError) as error:
        loop.tool('pw_assess', {'watch_id': watch['id'], 'refs': [ref], 'status': 'progress', 'note': '获批'})
    assert error.value.issues[0]['code'] == 'change_basis_required'
    assert loop.pending_assessments(loop.frozen())
    result = loop.tool('pw_assess', {'watch_id': watch['id'], 'refs': [ref],
        'status': 'progress', 'change_basis': 'object_change', 'note': '激光模组取得许可'})
    assert result['effective_status'] == 'progress'


@pytest.mark.parametrize(('claim','source','supported'), [
    ('2025-09-30实施', '2025年9月30日实施', True),
    ('2025年09月30日实施', '2025/9/30实施', True),
    ('2025-09-30实施', '2025年9月29日及2026年8月30日实施', False),
    ('2025-09-30实施', '2025年。9月。30日。', False),
    ('2025-09-30实施', '2025年9月实施', False),
    ('2025-02-30实施', '2025年2月28日实施', False),
    ('2025-02-30实施', '2025-02-30实施', False),
    ('2025年实施', '2025年9月30日实施', True),
    ('09例', '9例', False),
    ('100万元', '100元', False),
    ('收入100万元', '未实现收入100万元', False),
])
def test_whole_calendar_date_equivalence_only(loop, claim, source, supported):
    missing = loop.pipeline.questions._unsupported_numbers(loop.project_id, claim, [], [source])
    assert (not missing) is supported


def test_tool_text_blocks_and_nested_excerpts_are_lossless():
    body = '合成对象计划开展临床研究，不代表已经获批。' * 80
    data = {'fingerprint': 'x', 'numbered': {'N1': {'url': 'https://example.com/x',
        'context': body, 'read_scope': 'search_summary', 'association_excerpts': [
            {'url': 'https://example.com/x', 'excerpt': body[50:750], 'basis': 'search_summary'},
            {'url': 'https://example.com/other', 'excerpt': body[50:750], 'basis': 'search_summary'}]}}}
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'c', 'function': {'name': 'pw_context'}}]},
        {'role': 'tool', 'tool_call_id': 'c', 'content': [{'type':'text','text':json.dumps(data)}]}]
    original = json.dumps(messages)
    packed = compact_context_messages(messages)
    assert json.dumps(messages) == original
    assert isinstance(packed[-1]['content'], list)
    result = json.loads(packed[-1]['content'][0]['text'])
    first, other = result['numbered']['N1']['association_excerpts']
    ptr = first['excerpt_same_as']
    text = result
    for part in ptr['path']:
        text = text[part]
    assert text[ptr['start']:ptr['end']] == data['numbered']['N1']['association_excerpts'][0]['excerpt']
    assert other == data['numbered']['N1']['association_excerpts'][1]
    assert first['basis'] == 'search_summary'
    assert compact_context_messages([*messages, {'role':'user','content':'继续'}])[:-1] == packed
    assert len(json.dumps(packed)) < len(original)


def test_finished_round_has_fact_based_summary(loop):
    watch, _, ref = collect(loop)
    loop.tool('pw_assess', {'watch_id': watch['id'], 'refs': [ref], 'status': 'unchanged',
        'change_basis': 'evidence_only', 'note': '补充材料'})
    judge(loop, ref)
    loop.tool('pw_revise', {'changes_json': json.dumps([{'watch_id':watch['id'], 'action':'finish',
        'text':'激光模组取得许可', 'why':'问题已回答', 'refs':[ref]}]), 'reason':'本轮结束'})
    finished = loop.tool('pw_finish', {'reason':'全部齐全'})
    summary = finished['round_summary']
    assert summary['job_id'] == loop.execution.job['id']
    assert summary['evidence_only'] == 1 and summary['object_changes'] == 0
    assert summary['judgment_change'] == 'first'
    assert summary['stopped'] == 1 and summary['active'] == 0
    assert summary['stop_details'] == [{'reason': '本次问题已回答', 'count': 1}]
    assert summary['outputs']['revision_applied'] == 1
    assert loop.plan.view(loop.project_id)['last_round'] == summary


def test_invalid_assessment_not_silently_normalized(loop):
    watch, _, ref = collect(loop)
    with pytest.raises(ValueError, match='unknown assessment status'):
        loop.tool('pw_assess', {'watch_id':watch['id'], 'refs':[ref], 'status':'invented',
            'change_basis':'evidence_only', 'note':'补充证据'})


def test_legacy_round_not_invented(loop):
    loop.store.set_state('dsh-monitor:' + loop.project_id, json.dumps({'status':'complete'}))
    assert loop.plan.view(loop.project_id)['last_round'] is None
