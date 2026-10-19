from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from test_studio_signals import _event, _setup


def old_project(tmp_path):
    _, store, project, monitor, _ = _setup(tmp_path)
    with store._connect() as db:
        db.execute('UPDATE studio_projects SET created_at=? WHERE project_id=?',
            ((datetime.now(UTC)-timedelta(days=200)).isoformat(), project.id))
    for n in range(7):
        _event(store, project.id, f'历史新闻{n}', days=80+n*10, importance='routine')
    return store, project.id, monitor


def healthy_history(store, pid, hours=24):
    from project_watch.studio.health import HealthStore
    health = HealthStore(store)
    for age in range(65*24, -1, -hours):
        with patch('project_watch.studio.health.now', return_value=(datetime.now(UTC)-timedelta(hours=age)).isoformat()):
            health.record(pid, {'company': {'checked': 1, 'added': 0}})


def test_missing_short_and_broken_coverage_do_not_report_company_silence(tmp_path):
    from project_watch.studio.health import HealthStore
    from project_watch.studio.signals import silence
    store, pid, monitor = old_project(tmp_path)
    assert not any(s['kind'] == 'news_quiet' for s in silence(store, monitor, pid))
    HealthStore(store).record(pid, {'company': {'checked': 1}})
    assert not any(s['kind'] == 'news_quiet' for s in silence(store, monitor, pid))
    healthy_history(store, pid)
    HealthStore(store).record(pid, {'company': {'checked': 0, 'errors': ['offline']}})
    assert not any(s['kind'] == 'news_quiet' for s in silence(store, monitor, pid))


def test_observed_source_quiet_is_not_business_decline(tmp_path):
    from project_watch.studio.signals import silence
    store, pid, monitor = old_project(tmp_path)
    healthy_history(store, pid)
    result = next(s for s in silence(store, monitor, pid) if s['kind'] == 'news_quiet')
    assert '业务放缓' not in result['detail'] and '来源' in result['detail']


def test_first_pass_is_not_silence_or_a_permanent_gap_after_followup(tmp_path):
    from project_watch.studio.health import HealthStore, silence_coverage

    store, pid, _ = old_project(tmp_path)
    healthy_history(store, pid)
    health = HealthStore(store)
    health.record(pid, {'company': {'checked': 2, 'deferred': True}})
    assert not silence_coverage(store, pid)
    health.record(pid, {'company': {'checked': 4}})
    assert silence_coverage(store, pid)


def test_weekly_coverage_respects_user_interval_and_stale_site_is_not_quiet(tmp_path):
    from project_watch.studio.signals import silence
    store, pid, monitor = old_project(tmp_path)
    healthy_history(store, pid, hours=168)
    assert any(s['kind'] == 'news_quiet' for s in silence(store, monitor, pid, base_hours=168))
    assert not any(s['kind'] == 'news_quiet' for s in silence(store, monitor, pid, base_hours=24))
    page = monitor.add_page(pid, 'https://synthetic.example/news', 'News', 'news', 'auto')
    monitor.save_check(page['id'], status='ok', blocks=['x'])
    old = (datetime.now(UTC)-timedelta(days=200)).isoformat()
    with monitor._db() as db:
        db.execute('UPDATE studio_site_pages SET created_at=?,last_changed=?,last_checked=? WHERE id=?', (old, old, old, page['id']))
    assert not any(s['kind'] == 'site_frozen' for s in silence(store, monitor, pid, base_hours=168))
