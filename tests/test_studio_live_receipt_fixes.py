"""Synthetic regressions derived from receipt shapes, no real materials/network."""
import json

import pytest
import test_studio_dsh_monitor as fixtures
from test_studio_dsh_monitor import collect, judge

from project_watch.studio.dsh_bootstrap import RecordValidationError, validate_record
from project_watch.studio.dsh_runtime import compact_context_messages
from project_watch.studio.questions import QuestionLedger


@pytest.fixture
def loop(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path)


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


def test_unchanged_round_not_falsely_stale_after_json_checkpoint(loop):
    _, _, ref = collect(loop)
    judge(loop, ref)
    loop.tool('pw_revise', {'changes_json': '[]', 'reason': '继续跟踪'})
    assert loop.tool('pw_finish', {'reason': '本轮结束'})['new_evidence_after_judgment'] is False


def test_real_new_version_still_marks_judgment_stale(loop):
    _, _, ref = collect(loop)
    judge(loop, ref)
    loop.tool('pw_revise', {'changes_json': '[]', 'reason': '继续跟踪'})
    with loop.store._connect() as db:
        db.execute("UPDATE studio_events SET updated_at='2099-01-01T00:00:00+00:00'")
    assert loop.tool('pw_finish', {'reason': '本轮结束'})['new_evidence_after_judgment'] is True


@pytest.mark.parametrize(('claim', 'quote', 'ok'), [
    ('2025年3月发布说明。', '2025-03-14，某地发布说明。', True),
    ('2025-03-14发布说明。', '2025年3月14日，某地发布说明。', True),
    ('3月14日发布说明。', '2025-03-14，某地发布说明。', True),
    ('3月14日发布说明。', '3月13日发布，2月14日修订。', False),
    ('2月30日发布说明。', '2月30日，某地发布说明。', False),
    ('2025年3月发布说明。', '2025年2月发布一份说明，2026年3月发布另一份。', False),
    ('2025-03-14发布说明。', '2025年3月13日发布，2026年2月14日修订。', False),
    ('2025-02-30发布说明。', '2025-02-30，某地发布说明。', False),
    ('2025年3月发布说明。', '某地发布说明，具体时间尚未确认。', False),
])
def test_record_calendar_is_whole_and_not_provider_date(claim, quote, ok):
    source = {'id': 'S1', 'published_at': '2025-03-14', 'excerpt': quote}
    if ok:
        validate_record(source, quote, claim, '说明')
    else:
        with pytest.raises(RecordValidationError):
            validate_record(source, quote, claim, '说明')


def test_missing_record_fields_are_returned_together(loop):
    loop.tool('pw_search', {'query': '合成公司', 'category': 'company'})
    with pytest.raises(RecordValidationError) as error:
        loop.tool('pw_record', {'kind': 'policy', 'source_id': 'S1'})
    assert {i['field'] for i in error.value.issues} >= {'quote', 'text', 'label'}
    assert not loop.state['records']


def test_validation_quote_shares_exact_source_without_losing_feedback():
    from project_watch.studio.dsh_bootstrap import PROMPT as bootstrap_prompt
    from project_watch.studio.dsh_monitor import PROMPT as monitor_prompt

    assert all('suggested_quote_same_as' in prompt for prompt in (bootstrap_prompt, monitor_prompt))
    quote = '合成材料说明，不构成公司新进展。' * 35
    messages = []
    for cid, name, result in [
        ('search', 'pw_search', {'sources': [{'id': 'S1', 'url': 'https://example.org/a', 'excerpt': quote}]}),
        ('record', 'pw_record', {'error': 'record_validation', 'source_id': 'S1', 'retry_unchanged': False,
                               'issues': [{'field': 'quote', 'suggested_quote': quote}]}),
    ]:
        messages += [{'role': 'assistant', 'tool_calls': [{'id': cid, 'function': {'name': name}}]},
                     {'role': 'tool', 'tool_call_id': cid, 'content': json.dumps(result, ensure_ascii=False)}]
    before = json.dumps(messages)
    packed = compact_context_messages(messages)
    feedback = json.loads(packed[-1]['content'])
    pointer = feedback['issues'][0]['suggested_quote_same_as']
    assert pointer['tool_call_id'] == 'search' and pointer['path'] == ['sources', 0, 'excerpt']
    assert quote[pointer['start']:pointer['end']] == quote
    assert feedback['retry_unchanged'] is False and feedback['issues'][0]['field'] == 'quote'
    assert compact_context_messages(messages[:2]) == packed[:2]
    assert json.dumps(messages) == before
    # Unbound IDs cannot inherit an unrelated source's text, even if identical.
    unknown = json.loads(messages[-1]['content'])
    unknown['source_id'] = 'S9'
    messages[-1]['content'] = json.dumps(unknown)
    assert 'suggested_quote' in json.loads(compact_context_messages(messages)[-1]['content'])['issues'][0]
