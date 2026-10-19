from datetime import date

import pytest

from project_watch.studio.commitments import build, due_interval


@pytest.mark.parametrize(('text', 'start', 'end'), [
    ('2026年二季度', '2026-04-01', '2026-06-30'), ('明年年底', '2027-12-31', '2027-12-31'),
    ('2026年2月', '2026-02-01', '2026-02-28'), ('2026年8月1日', '2026-08-01', '2026-08-01'),
    ('2026-08-01', '2026-08-01', '2026-08-01'),
])
def test_due_intervals(text, start, end):
    assert due_interval(text, date(2026, 1, 1)) == (date.fromisoformat(start), date.fromisoformat(end))


@pytest.mark.parametrize('text', ['很快', '年底后', '2026年13月', '2026-02-30', '5年内'])
def test_unsupported_or_invalid_deadlines_are_not_guessed(text):
    assert due_interval(text, date(2026, 1, 1)) is None


def promise(**changes):
    return {'subject_id': 'alpha', 'subject_name': '合成甲', 'target_kind': '交付', 'target_text': '原型甲',
            'due_end': '2026-06-30', 'said_at': '2026-01-01', 'support': 'source_assertion', 'fact_id': 'c1', **changes}


def outcome(**changes):
    return {'subject_id': 'alpha', 'basis': 'actual', 'support': 'source_assertion', 'occurred_at': '2026-06-01',
            'attributes': {'name': '原型甲', 'state': 'delivered', 'state_text': '交付'}, **changes}


@pytest.mark.parametrize(('day', 'label'), [('2026-06-01', '已兑现'), ('2026-08-01', '晚 2 个月兑现')])
def test_located_outcome_and_late_delivery(day, label):
    row = build([promise()], [outcome(occurred_at=day)], today=date(2026, 10, 9))['items'][0]
    assert row['status_label'] == label


def test_no_news_is_not_nonperformance_and_different_product_or_subject_never_matches():
    wrong = [outcome(subject_id='beta'), outcome(attributes={'name': '原型乙', 'state': 'delivered', 'state_text': '交付'}),
             outcome(basis='forecast'), outcome(support='pending')]
    assert build([promise()], wrong, today=date(2026, 10, 9))['items'][0]['status'] == 'overdue'
    assert build([promise()], wrong, today=date(2026, 6, 1))['items'][0]['status'] == 'not_due'
    assert build([promise()], wrong, today=date(2026, 7, 1))['items'][0]['status'] == 'grace'


def test_repeated_promise_keeps_first_and_latest_and_cancellation_reverses_fulfillment():
    repeated = promise(fact_id='c2', said_at='2026-02-01', due_end='2026-07-31')
    cancelled = outcome(occurred_at='2026-09-01', attributes={'name': '原型甲', 'state': 'cancelled', 'state_text': '取消交付'})
    row = build([promise(), repeated], [outcome(), cancelled], today=date(2026, 10, 9))['items'][0]
    assert row['status'] == 'cancelled' and row['statements'] == 2 and row['due_changed']
    assert (row['first_said_at'], row['last_said_at']) == ('2026-01-01', '2026-02-01')


def test_extract_peer_events_and_commitments_with_one_bounded_input_and_no_extra_search(tmp_path):
    from test_studio_competition_research import research_pipeline
    from test_studio_evidence_versions import sample

    from project_watch.studio.commitments import read
    from project_watch.studio.event_state import timeline
    from project_watch.studio.monitoring_contracts import ObjectInput

    p, pid, _, _ = research_pipeline(tmp_path)
    p.store.put_plan(pid, {'competitors': [{'name': '合成乙'}]})
    peer = p.objects.save(pid, ObjectInput(kind='company', name='合成乙', visibility='public'), actor='user')
    text = '合成乙原型甲于2026年8月1日交付。合成乙2026年1月1日宣布计划在2026年二季度交付原型甲。'
    p.evidence.save(sample(pid, text).model_copy(update={'source_key': 'public-profile:peer', 'visibility': 'public'}))
    inputs = p.structured_facts.inputs(pid, p.model_binding())
    result = p.structured_facts.apply(pid, inputs, {'events': [{'source': 'B1', 'quote': text.split('。')[0],
        'company': '合成乙', 'name': '原型甲', 'channel': 'commercial', 'role': 'actor', 'state': 'delivered',
        'state_text': '交付', 'basis': 'actual', 'occurred_at': '2026-08-01'}], 'commitments': [
        {'source': 'B1', 'quote': text.split('。')[1], 'company': '合成乙', 'statement': text.split('。')[1],
         'target_kind': '交付', 'target_text': '原型甲', 'due_text': '2026年二季度', 'said_at': '2026-01-01', 'speaker': '合成乙'}]})
    assert len(result['fact_ids']) == 2
    assert timeline(p, pid)['items'][0]['subject_id'] == peer['id']
    assert timeline(p, pid)['items'][0]['support'] == 'source_assertion'
    assert read(p, pid)['items'][0]['status'] == 'late'


def test_company_comention_cannot_attribute_another_actors_delivery(tmp_path):
    from test_studio_competition_research import research_pipeline
    from test_studio_evidence_versions import sample

    from project_watch.studio.monitoring_contracts import ObjectInput

    p, pid, _, _ = research_pipeline(tmp_path)
    p.store.put_plan(pid, {'competitors': [{'name': '合成乙'}]})
    p.objects.save(pid, ObjectInput(kind='company', name='合成乙', visibility='public'), actor='user')
    text = 'Synthetic Optics原型甲2026-08-01交付给合成乙。'
    p.evidence.save(sample(pid, text).model_copy(update={'source_key': 'public-profile:co-mention', 'visibility': 'public'}))
    inputs = p.structured_facts.inputs(pid, p.model_binding())
    data = {'events': [{'source': 'B1', 'quote': text, 'company': '合成乙', 'name': '原型甲', 'channel': 'commercial',
                       'role': 'actor', 'state': 'delivered', 'state_text': '交付', 'basis': 'actual', 'occurred_at': '2026-08-01'}]}
    assert p.structured_facts.apply(pid, inputs, data)['fact_ids'] == []


def test_product_prefix_and_reversal_do_not_make_an_old_delivery_fulfill_another_promise():
    product_variant = outcome(attributes={'name':'原型甲增强版','state':'delivered','state_text':'交付'})
    assert build([promise()],[product_variant],today=date(2026,10,9))['items'][0]['status'] == 'overdue'
    reversal = outcome(occurred_at='2026-07-01',attributes={'name':'原型甲','state':'cancelled','state_text':'取消'})
    later = outcome(occurred_at='2026-08-01',sources=[{'url':'https://press.example/later'}])
    row = build([promise()],[outcome(),reversal,later],today=date(2026,10,9))['items'][0]
    assert row['status'] == 'late' and row['late_months'] == 2
    assert row['outcome_sources'] == later['sources']
    conflicting = outcome(occurred_at='2026-08-01',attributes={'name':'原型甲','state':'cancelled','state_text':'取消'})
    assert build([promise()],[later,conflicting],today=date(2026,10,9))['items'][0]['status'] == 'uncertain'
