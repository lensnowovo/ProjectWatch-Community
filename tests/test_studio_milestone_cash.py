from datetime import UTC, datetime

from test_studio_event_state import setup
from test_studio_evidence_versions import sample


def save(service, pid, subject, attrs, *, key, day='2026-09-01', basis='actual', value=None, unit='', period='', currency='', scope='unknown'):
    from unittest.mock import patch

    from project_watch.studio.monitoring_contracts import FactInput, LocatedRef
    text = 'Alpha ' + ' '.join(str(v) for v in attrs.values() if v is not None) + f' {day} {value or ""} {unit} {period} {currency} {scope}'
    e = service.evidence.save(sample(pid, text).model_copy(update={'source_key': key, 'visibility': 'public'}))
    ref = LocatedRef(evidence_version_id=e['id'], part_index=0, start=0, end=len(text), quote=text)
    with patch('project_watch.studio.objects.now', return_value=day+'T00:00:00+00:00'):
        return service.objects.save_fact(pid, FactInput(subject_id=subject['id'], predicate=attrs['kind'], text=text,
            basis=basis, subject_attribution='explicit', occurred_at=day, visibility='public', refs=(ref,), attributes=attrs,
            value=value, unit=unit, period=period, currency=currency, consolidation_scope=scope))


def test_original_adjusted_actual_dates_are_not_overwritten(tmp_path):
    from project_watch.studio.milestones import read
    service, pid, subject = setup(tmp_path)
    for phase, target, day in [('original', '2026-10-01', '2026-07-01'),
                               ('adjusted', '2026-11-01', '2026-08-01'), ('actual', '2026-11-05', '2026-11-05')]:
        save(service, pid, subject, {'kind': 'milestone', 'name': 'Line One', 'phase': phase,
            'phase_text': phase, 'target_date': target}, key=phase, day=day, basis='actual' if phase == 'actual' else 'forecast')
    row = read(service, pid, cutoff=datetime(2026, 12, 1, tzinfo=UTC))['items'][0]
    assert row['original_dates'] == ['2026-10-01'] and row['adjusted_dates'] == ['2026-11-01']
    assert row['actual_dates'] == ['2026-11-05'] and len(row['history']) == 3
    assert read(service, pid, cutoff=datetime(2026, 10, 1, tzinfo=UTC))['items'][0]['actual_dates'] == []


def test_plan_does_not_become_funding_received_and_profit_is_not_cash(tmp_path):
    from project_watch.studio.funding_facts import project_funding
    service, pid, subject = setup(tmp_path)
    for measure, stage, basis in [('funding', 'plan', 'forecast'), ('net_profit', 'profit', 'actual')]:
        fact = save(service, pid, subject, {'kind': 'funding_fact', 'measure': measure, 'stage': stage,
            'stage_text': stage}, key=measure, basis=basis, value='123456789012345.6789', unit='元', period='2026Q3', currency='CNY', scope='公司单体')
        result = project_funding(service, pid, fact)
        assert not result['is_funding_received'] and not result['is_cash_balance']
        assert result['normalized_value'] == '12345678901.23456789'
        assert result['raw_value'] == '123456789012345.6789' and result['cash_gap'] is None


def test_money_scopes_stay_separate_and_source_revision_stales_link(tmp_path):
    from project_watch.studio.funding_facts import project_funding
    service, pid, subject = setup(tmp_path)
    attrs = {'kind': 'funding_fact', 'measure': 'funding', 'stage': 'received', 'stage_text': 'received', 'milestone_name': 'Line One'}
    fact = save(service, pid, subject, attrs, key='cash', value='100', unit='万元', period='2026Q3', currency='CNY', scope='公司单体')
    other = save(service, pid, subject, attrs, key='subsidiary', value='100', unit='万美元', period='2026Q2', currency='USD', scope='子公司')
    a, b = project_funding(service, pid, fact), project_funding(service, pid, other)
    assert a['series_key'] != b['series_key']
    assert a['is_funding_received'] and a['milestone_name'] == 'Line One'
    service.evidence.dependencies.withdraw(pid, fact['refs'][0]['evidence_version_id'], 'changed use', actor='fixture')
    stale = project_funding(service, pid, fact)
    assert stale['support'] == 'pending' and not stale['is_funding_received']


def test_missing_scope_currency_date_or_value_is_not_zero(tmp_path):
    from project_watch.studio.funding_facts import project_funding
    service, pid, subject = setup(tmp_path)
    fact = save(service, pid, subject, {'kind': 'funding_fact', 'measure': 'cash_balance', 'stage': 'balance', 'stage_text': 'balance'}, key='missing')
    result = project_funding(service, pid, fact)
    assert result['support'] == 'pending' and result['normalized_value'] is None and result['cash_gap'] is None
