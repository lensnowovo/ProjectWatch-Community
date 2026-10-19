from datetime import UTC, datetime

import pytest
from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample


def setup(tmp_path):
    from project_watch.studio.monitoring_contracts import ObjectInput
    service, pid = make_service(tmp_path / 'events.sqlite3')
    subject = service.objects.save(pid, ObjectInput(kind='company', name='Alpha', visibility='public'), actor='user')
    return service, pid, subject


def add(service, pid, subject, state, day, *, role='recipient', basis='actual', discovered='2026-09-01T00:00:00+00:00', key=None):
    from unittest.mock import patch

    from project_watch.studio.monitoring_contracts import FactInput, LocatedRef
    text = f'Alpha Series A {state} {day or "date unknown"}.'
    evidence = service.evidence.save(sample(pid, text).model_copy(update={'source_key': key or state, 'visibility': 'public'}))
    ref = LocatedRef(evidence_version_id=evidence['id'], part_index=0, start=0, end=len(text), quote=text)
    with patch('project_watch.studio.objects.now', return_value=discovered):
        return service.objects.save_fact(pid, FactInput(subject_id=subject['id'], predicate='event', text=text,
            visibility='public', basis=basis, subject_attribution='explicit', occurred_at=day, refs=(ref,),
            attributes={'kind': 'event', 'name': 'Series A', 'channel': 'funding', 'role': role,
                'state': state, 'state_text': state}))


def test_delayed_old_news_does_not_roll_back_receipt(tmp_path):
    from project_watch.studio.event_state import timeline
    service, pid, subject = setup(tmp_path)
    add(service, pid, subject, 'received', '2026-08-01')
    add(service, pid, subject, 'intent', '2026-06-01', discovered='2026-09-20T00:00:00+00:00')
    result = timeline(service, pid, cutoff=datetime(2026, 10, 1, tzinfo=UTC))
    assert result['items'][0]['state'] == 'received'
    assert len(result['items'][0]['history']) == 2


@pytest.mark.parametrize('state', ['cancelled', 'delayed', 'withdrawn', 'paused', 'resumed'])
def test_reverse_changes_are_not_forced_into_forward_sequence(tmp_path, state):
    from project_watch.studio.event_state import timeline
    service, pid, subject = setup(tmp_path)
    add(service, pid, subject, 'agreement', '2026-07-01')
    add(service, pid, subject, state, '2026-08-01')
    assert timeline(service, pid, cutoff=datetime(2026, 10, 1, tzinfo=UTC))['items'][0]['state'] == state


def test_forecast_investor_and_unknown_date_are_not_company_receipt(tmp_path):
    from project_watch.studio.event_state import timeline
    service, pid, subject = setup(tmp_path)
    add(service, pid, subject, 'agreement', '2026-07-01')
    add(service, pid, subject, 'received', '2026-08-01', role='actor')
    add(service, pid, subject, 'received', '2026-08-02', basis='forecast', key='forecast')
    add(service, pid, subject, 'received', None, key='undated')
    row = timeline(service, pid, cutoff=datetime(2026, 10, 1, tzinfo=UTC))['items'][0]
    assert row['state'] == 'agreement' and sum(h['support'] == 'pending' for h in row['history']) == 3


def test_conflict_does_not_use_uuid_or_discovery_order_as_tiebreaker(tmp_path):
    from project_watch.studio.event_state import timeline
    service, pid, subject = setup(tmp_path)
    add(service, pid, subject, 'received', '2026-08-01')
    add(service, pid, subject, 'cancelled', '2026-08-01')
    row = timeline(service, pid, cutoff=datetime(2026, 10, 1, tzinfo=UTC))['items'][0]
    assert row['support'] == 'conflict' and row['state'] is None


def test_cutoff_excludes_future_discovery_and_occurrence(tmp_path):
    from project_watch.studio.event_state import timeline
    service, pid, subject = setup(tmp_path)
    add(service, pid, subject, 'agreement', '2026-07-01')
    add(service, pid, subject, 'received', '2026-08-01', discovered='2026-10-02T00:00:00+00:00')
    add(service, pid, subject, 'cancelled', '2026-11-01')
    row = timeline(service, pid, cutoff=datetime(2026, 10, 1, tzinfo=UTC))['items'][0]
    assert row['state'] == 'agreement' and len(row['history']) == 1


def test_source_withdrawal_stales_state_and_keeps_history(tmp_path):
    from project_watch.studio.event_state import timeline
    service, pid, subject = setup(tmp_path)
    fact = add(service, pid, subject, 'received', '2026-08-01')
    service.evidence.dependencies.withdraw(pid, fact['refs'][0]['evidence_version_id'], 'source correction', actor='fixture')
    row = timeline(service, pid, cutoff=datetime(2026, 10, 1, tzinfo=UTC))['items'][0]
    assert row['state'] is None and row['history'][0]['support'] == 'pending'
