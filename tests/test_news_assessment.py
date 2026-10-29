"""Synthetic full projection checks; never call a provider or real instance."""
import socket
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_studio_dsh_bootstrap import call, record
from test_studio_dsh_bootstrap import research as research_fixture
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio.news_assessment import candidate, event_assessment

research = research_fixture


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('network prohibited')
    monkeypatch.setattr(socket, 'create_connection', deny)


@pytest.mark.parametrize('level', ['major', 'notable', 'routine', None])
def test_reviewed_rating_projects_and_replays_without_calls(research, level):
    call(research, 'pw_search', query='Synthetic', category='company')
    record(research, kind='company', importance=level, importance_reason='所引材料描述该公司产品进展' if level else '')
    calls = []
    research.record_reviewer = lambda p: (calls.append(p), synthetic_review(p))[1]
    call(research, 'pw_finish', gaps={})
    [event] = research.store.list_events(project_id=research.project_id)
    assert event['importance'] == (level or 'routine')
    assert event['importance_status'] == ('assessed' if level else 'pending')
    assert bool(event['importance_reason']) == bool(level)
    assert event['event_at'] is None  # source publication date is not occurrence
    before = len(calls)
    call(research, 'pw_finish', gaps={})
    assert len(calls) == before
    assert len(research.store.list_events(project_id=research.project_id)) == 1


def test_bad_rating_preserves_admitted_news(research):
    call(research, 'pw_search', query='Synthetic', category='company')
    record(research, kind='company', importance='major', importance_reason='缺乏依据的重大影响')
    def reviewer(payload):
        response = synthetic_review(payload)
        for check in response.get('checks', []):
            if check['field'].startswith('rating:'):
                check.update(status='insufficient', reason='影响强度缺乏依据')
        return response
    research.record_reviewer = reviewer
    call(research, 'pw_finish', gaps={})
    [event] = research.store.list_events(project_id=research.project_id)
    assert event['importance_status'] == 'pending'
    assert event['summary']


def test_rating_fact_disagreement_does_not_reject_supported_facts(research):
    from project_watch.studio.dsh_record_review import admit

    call(research, 'pw_search', query='Synthetic', category='company')
    record(research, kind='company', importance='major', importance_reason='合成影响判断')
    row = research.state['records'][0]
    records = [{**row, 'key': str(index)} for index in range(7)]
    def reviewer(payload):
        if payload['phase'] == 'consistency':
            return {'consistent': False, 'reason': '合成评级超出事实', 'conflicts': [
                {'fields': ['record:0', 'rating:record:0'], 'reason': '影响强度不一致'}]}
        return synthetic_review(payload)
    research.record_reviewer = reviewer
    accepted, _, receipt = admit(research, records, [])
    assert len(accepted) == 7
    assert accepted[0]['importance_reviewed'] is False
    assert all(row['importance_reviewed'] for row in accepted[1:])
    assert receipt['rejected'] == [{'key': 'rating:record:0', 'code': 'evidence_cross_section_conflict'}]


def test_manual_routine_is_not_turned_into_pending(research):
    call(research, 'pw_search', query='Synthetic', category='company')
    record(research, kind='company')
    call(research, 'pw_finish', gaps={})
    [event] = research.store.list_events(project_id=research.project_id)
    research.pipeline.monitor.add_feedback(research.project_id, event['id'], 'importance', event['title'], 'routine', 'routine')
    [event] = research.store.list_events(project_id=research.project_id)
    assert event['importance_status'] == 'assessed'


def test_generic_legacy_detection_and_explicit_rating_validation():
    assert candidate({}) == {}
    with pytest.raises(ValueError):
        candidate({'importance': 'major', 'importance_reason': ''})
    dsh = [{'extra': {'dsh_claims': [{'text': 'Synthetic'}]}}]
    assert event_assessment({'importance': 'routine'}, dsh)['importance_status'] == 'pending'
    for sources in [[], [{'extra': {}}], [*dsh, {'extra': {}}]]:
        assert event_assessment({'importance': 'routine'}, sources)['importance_status'] == 'assessed'
    assert event_assessment({'importance': 'major'}, dsh)['importance_status'] == 'assessed'


def test_reviewed_date_survives_projection_and_api_range_excludes_unknown_future(research):
    call(research, 'pw_search', query='Synthetic', category='company')
    record(research, kind='company')
    today = datetime.now(UTC).date().isoformat()
    research.state['records'][0]['date'] = today  # synthetic reviewer supports this fixture
    call(research, 'pw_finish', gaps={})
    [event] = research.store.list_events(project_id=research.project_id, since_days=30, dated_only=True)
    assert event['event_at'] == today
    for date in [None, '2020-01-01', (datetime.now(UTC) + timedelta(days=2)).date().isoformat()]:
        with closing(research.store._connect()) as db, db:
            db.execute('UPDATE studio_events SET event_at=? WHERE id=?', (date, event['id']))
        assert research.store.list_events(project_id=research.project_id, since_days=30, dated_only=True) == []
        assert len(research.store.list_events(project_id=research.project_id)) == 1
        if date is None:
            assert len(research.store.list_events(project_id=research.project_id, since_days=30)) == 1
