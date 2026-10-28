"""Synthetic acceptance and failure cases for public projection admission."""
import json

import pytest
from test_studio_dsh_bootstrap import call, record
from test_studio_dsh_bootstrap import research as research_fixture
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import dsh_record_review as review
from project_watch.studio.http import HttpError

research = research_fixture


def prepare(research, count=1):
    call(research, 'pw_search', query='Synthetic Optics', category='company')
    record(research)
    row = research.state['records'][0]
    research.state['records'] = [{**row, 'key': str(i), 'label': 'Synthetic assertion ' + str(i)} for i in range(count)]


def test_rejected_heading_never_projects_or_reenters_context(research):
    prepare(research, 2)
    raw = json.dumps(research.state['records'], sort_keys=True)
    def check(payload):
        result = synthetic_review(payload)
        result['checks'][0].update(status='contradicted', reason='Synthetic mixed-subject attribution')
        return result
    research.record_reviewer = check
    result = call(research, 'pw_finish', gaps={})
    assert result['status'] == 'partial' and result['record_admission']['accepted'] == 1
    assert result['retained_records'] == 2 and result['records'] == 1
    assert json.dumps(research.state['records'], sort_keys=True) == raw
    [fact] = research.store.get_public_profile(research.project_id)['facts']
    assert fact['label'] == 'Synthetic assertion 1'
    assert result['record_admission']['rejected'][0]['code'] == 'evidence_contradicted'


@pytest.mark.parametrize('kind', ['company', 'peer', 'relation'])
def test_rejected_records_cannot_create_news_peers_or_scope(research, kind):
    prepare(research)
    row = research.state['records'][0]
    row.update(kind=kind, reason='Synthetic relation', axis='asset', query='Synthetic query',
               relation_type='supplier', relation_target='Synthetic target')
    def check(payload):
        result = synthetic_review(payload)
        for c in result['checks']:
            c.update(status='insufficient', reason='No source supports this relationship')
        return result
    research.record_reviewer = check
    result = call(research, 'pw_finish', gaps={})
    assert result['records'] == 0 and result['status'] == 'partial'
    assert not research.store.list_events(project_id=research.project_id)
    assert not research.pipeline.monitoring_plan.items(research.project_id)
    assert not research.store.get_public_profile(research.project_id)['facts']


def test_partial_review_receipts_resume_without_repeating_completed_requests(research):
    prepare(research, 25)
    calls = []
    def interrupted(payload):
        calls.append(payload['phase'])
        if len(calls) == 2:
            raise RuntimeError('synthetic interruption before dispatch')
        return synthetic_review(payload)
    research.record_reviewer = interrupted
    with pytest.raises(RuntimeError):
        review.admit(research, research.state['records'], [])
    research.record_reviewer = lambda p: (calls.append(p['phase']), synthetic_review(p))[1]
    accepted, _, receipt = review.admit(research, research.state['records'], [])
    assert len(accepted) == 25 and not receipt['rejected']
    assert calls == ['fields', 'fields', 'fields', 'fields', 'consistency']


def test_cross_record_conflict_quarantines_both_records(research):
    prepare(research, 13)
    def check(payload):
        if payload['phase'] == 'consistency':
            return {'consistent': False, 'reason': 'Synthetic incompatible assertions',
                    'conflicts': [{'fields': ['record:0', 'record:1'], 'reason': 'Conflicting attribution'}]}
        return synthetic_review(payload)
    research.record_reviewer = check
    accepted, _, receipt = review.admit(research, research.state['records'], [])
    assert len(accepted) == 11 and len(receipt['rejected']) == 2


@pytest.mark.parametrize('failure', ['missing', 'invalid', 'pause', 'generation', 'unknown'])
def test_review_failure_cannot_publish(research, failure):
    prepare(research, 13)
    calls = []
    def check(payload):
        calls.append(1)
        if failure == 'invalid':
            return {'checks': []}
        if failure == 'pause':
            research.store.set_status(research.project_id, 'paused')
        if failure == 'generation':
            research.reader.read = lambda u: {'status': 'article_body', 'text': 'Updated source context'}
            research.pw_read({'source_id': 'S1'})
        if failure == 'unknown':
            research.hold('request_outcome_unknown')
            raise HttpError('request_outcome_unknown')
        return synthetic_review(payload)
    research.record_reviewer = None if failure == 'missing' else check
    with pytest.raises((HttpError, ValueError)):
        review.admit(research, research.state['records'], [])
    assert len(calls) <= 1
    assert not research.store.get_public_profile(research.project_id)
    assert not research.state['finished']


def test_old_unverified_fact_is_not_carried_into_new_current_profile(research):
    prepare(research)
    old = {k: research.state['records'][0][k] for k in ('kind', 'label', 'text', 'date', 'refs')}
    old['label'] = 'Unverified old attribute'
    research.store.put_compiled_profile(research.project_id, {'facts':[old]},
        [old['refs'][0]['evidence_version_id']], public=True,
        expected_generation=research.pipeline.evidence.dependencies.generation(research.project_id))
    def check(payload):
        result = synthetic_review(payload)
        for c in result['checks']:
            if c['field'].startswith('previous:'):
                c.update(status='insufficient', reason='Source does not establish old attribute')
        return result
    research.record_reviewer = check
    call(research, 'pw_finish', gaps={})
    profile = research.store.get_public_profile(research.project_id)
    assert old not in profile['facts'] and old in profile['historical_facts']
    assert profile['outcome']['semantic_verification'] == review.VERSION


def test_bound_sources_and_full_record_content_are_sent(research):
    prepare(research)
    row = research.state['records'][0]
    row.update(reason='Tentative relationship', relation_type='supplier', relation_target='Synthetic partner')
    p = review.payload_for(research, {'record:0':row})
    sent = json.loads(p['fields']['record:0'])
    assert sent['label'] == row['label'] and sent['text'] == row['text']
    assert sent['relation_type'] == 'supplier' and sent['reason'] == row['reason']
    assert p['bound_evidence']['record:0'] and p['evidence']


def test_non_factual_answer_cannot_admit_a_company_fact(research):
    prepare(research)
    def check(payload):
        result = synthetic_review(payload)
        result['checks'][0].update(status='not_factual', evidence=[])
        return result
    research.record_reviewer = check
    accepted, _, receipt = review.admit(research, research.state['records'], [])
    assert not accepted and receipt['rejected'][0]['code'] == 'not_a_public_fact'


def test_supported_record_cannot_borrow_another_records_source(research, monkeypatch):
    prepare(research)
    original = review.payload_for
    def payload_for(*args):
        payload = original(*args)
        payload['evidence']['unrelated'] = {'context': 'Another subject has this attribute.'}
        return payload
    monkeypatch.setattr(review, 'payload_for', payload_for)
    def check(payload):
        result = synthetic_review(payload)
        result['checks'][0]['evidence'] = [{'ref': 'unrelated', 'quote': 'Another subject'}]
        return result
    research.record_reviewer = check
    accepted, _, receipt = review.admit(research, research.state['records'], [])
    assert not accepted and receipt['rejected'][0]['code'] == 'evidence_not_bound'
