"""Receipt-to-page projection with synthetic stores and no provider calls."""
import pytest
import test_collection_dsh as fixtures
import test_collection_supplements as supplement_fixtures
from test_collection_plan import candidate, plan, success

from project_watch.studio import collection_dsh
from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.collection_plan import channel_status, reconcile
from project_watch.studio.dsh_bootstrap import PublicBootstrap


@pytest.fixture
def loop(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path)


@pytest.mark.parametrize('outcome', ['success', 'failed', 'unknown', 'budget_deferred', 'running',
                                    'pending', 'not_configured', 'not_integrated', 'needs_scope',
                                    'disabled', 'not_applicable', 'not_due'])
def test_saved_source_status_does_not_follow_configuration_or_content_count(loop, outcome):
    unit = candidate() | dict(source='search', category='technology')
    if outcome in ('not_configured', 'not_integrated', 'needs_scope', 'disabled', 'not_applicable'):
        unit.update(capability=outcome, reason='Synthetic source choice')
    if outcome == 'not_due':
        unit['schedule_due'] = False
    p = plan([unit], round_id=loop.execution.job['id'], project_id=loop.project_id)
    ledger = CollectionLedger(loop.execution, p)
    if outcome == 'success':
        ledger.record(success(p))
    elif outcome in ('failed', 'unknown', 'budget_deferred', 'running'):
        ledger.record(dict(round_id=p['round_id'], unit_id=p['units'][0]['id'], outcome=outcome,
                           reason='synthetic_failure'))
    research = PublicBootstrap(loop.pipeline, loop.execution, loop.collectors, None)
    research.tool('pw_finish', {'gaps': {}})
    stored = loop.store.get_plan(loop.project_id)
    report = stored['collection_report']
    assert report['job_id'] == stored['round_id'] == p['round_id']
    assert report['status'] == ledger.coverage()['status']
    channel = report['channels']['technology']
    expected = 'empty' if outcome == 'success' else outcome
    assert channel['execution'] == expected
    assert channel['checked'] == (1 if outcome == 'success' else 0)
    assert channel['added'] == 0
    assert channel['deferred'] == (outcome not in ('success', 'disabled', 'not_applicable', 'not_due'))
    assert stored['has_search'] == (unit['capability'] == 'available')
    view = loop.pipeline.monitoring_plan.coverage(loop.project_id)
    item = next(r for r in view['science'] if r['key'] == 'technology')
    assert item['execution'] == expected
    assert item['capabilities'] == [unit['capability']]
    assert item['due_count'] == (outcome != 'not_due')
    assert item['rows'][0]['window_start'] == unit['window_start']
    assert item['state'] == ('on' if outcome == 'success' else 'error' if outcome in ('unknown', 'failed')
                             else 'off' if outcome in ('disabled', 'not_applicable', 'not_configured', 'not_integrated')
                             else 'pending')
    missing = next(r for r in view['science'] if r['key'] == 'paper')
    assert missing['execution'] == 'not_in_round' and missing['state'] == 'pending'
    assert not loop.execution.usage()


def test_source_adapter_beats_model_category_and_preserves_real_zero(loop):
    fixtures.imported(loop, [])
    research = PublicBootstrap(loop.pipeline, loop.execution, loop.collectors, None)
    research.tool('pw_finish', {'gaps': {}})
    stored = loop.store.get_plan(loop.project_id)
    assert set(stored['last_report']) == {'trial'}
    assert stored['last_report']['trial']['execution'] == 'empty'
    assert not stored['has_search']
    trial = next(r for r in loop.pipeline.monitoring_plan.coverage(loop.project_id)['asset'] if r['key'] == 'trial')
    assert trial['execution'] == 'empty' and trial['count'] == 0
    assert trial['window'] == '近 30 天'  # stored content count, not last-round query window
    assert trial['rows'][0]['result_count'] == 0


def test_supplement_and_host_both_count_without_model_search_double_count(tmp_path):
    research, calls = supplement_fixtures.research.__wrapped__(tmp_path)
    research.tool('pw_search', {'query': 'extra', 'category': 'competitor'})
    research.tool('pw_search', {'query': 'extra', 'category': 'competitor'})
    research.tool('pw_finish', {'gaps': {}})
    report = research.store.get_plan(research.project_id)['last_report']
    assert calls == ['base', 'extra']
    assert report['company']['checked'] == report['competitor']['checked'] == 1
    assert report['company']['added'] == report['competitor']['added'] == 1
    assert report['company']['count_kind'] == 'saved_source_items'
    assert not research.state['records']  # reports originate from saved retrieval, not model claims
    assert collection_dsh.report(research, collection_dsh.coverage(research))['channels'] == report


def test_mixed_queries_never_turn_one_success_into_channel_completion():
    p = plan([candidate(), candidate() | {'query_version': '2'}])
    rows = reconcile(p, [success(p)])['rows']
    assert channel_status(rows)['execution'] == 'partial'
    assert channel_status(rows)['successful_count'] == 1
    assert channel_status(rows)['query_count'] == 2


def test_unknown_remains_visible_beside_a_success():
    p = plan([candidate(), candidate() | {'query_version': '2'}])
    receipt = dict(round_id=p['round_id'], unit_id=p['units'][1]['id'], outcome='unknown')
    assert channel_status(reconcile(p, [success(p), receipt])['rows'])['execution'] == 'unknown'


def test_old_snapshot_is_not_projected_over_another_round(loop):
    fixtures.imported(loop, [])
    research = PublicBootstrap(loop.pipeline, loop.execution, loop.collectors, None)
    research.tool('pw_finish', {'gaps': {}})
    stored = loop.store.get_plan(loop.project_id)
    loop.store.put_plan(loop.project_id, {**stored, 'round_id': 'new-legacy-round', 'last_report': {}})
    rows = [row for group in loop.pipeline.monitoring_plan.coverage(loop.project_id).values() for row in group]
    current = next(row for row in rows if row['key'] == 'trial')
    assert current['round_id'] == loop.execution.job['id'] and current['execution'] == 'empty'
    assert current['round_id'] != 'new-legacy-round'  # source of truth is the job, not the plan blob
