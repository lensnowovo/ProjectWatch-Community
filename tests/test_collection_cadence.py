"""Confidence and explicit collection frequency are independent, without migration."""
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_studio_relation_monitoring import RELATION

from project_watch.studio.app import create_studio_app
from project_watch.studio.collection_scope import build_collection_plan


@pytest.fixture
def scope(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project('Synthetic cadence')
    pipeline.store.upsert_project(project.id, {'legal_name': 'Synthetic cadence'}, consent=True)
    return pipeline, project.id


def add(pipeline, pid, **fields):
    base = {k: v for k, v in RELATION.items() if k != 'cadence'}
    return pipeline.monitoring_plan.add(pid, base | fields, actor='system', origin='system', visibility='public')


def test_default_trial_gets_initial_query_then_weekly_and_manual_refresh_bypasses_only_schedule(scope):
    pipeline, pid = scope
    plan = pipeline.monitoring_plan
    watch = add(pipeline, pid)
    assert watch['status'] == 'trial' and watch['cadence'] == 'medium'
    assert [w['id'] for w in plan.due(pid)] == [watch['id']]
    plan.mark_query(watch['id'], watch['queries'][0], success=True,
                    lifecycle_version=str(watch['lifecycle_version']))
    at = datetime.now(UTC) + timedelta(days=1)
    assert not plan.due(pid, at=at)
    assert [w['id'] for w in plan.due(pid, at=at + timedelta(days=6))] == [watch['id']]
    for mode, expected in [('incremental', 'not_due'), ('refresh', 'pending')]:
        collection = build_collection_plan(round_id='synthetic-round',
            project=pipeline.store.get_project(pid), binding='synthetic', public_plan={},
            watches=plan.items(pid), capabilities={'search': 'available'}, at=at.isoformat(),
            mode=mode, due_watch_ids=[w['id'] for w in plan.due(pid, at=at)])
        [unit] = [u for u in collection['units'] if u['target_id'] == 'watch:' + watch['id']]
        assert unit['disposition'] == expected
    assert plan.items(pid)[0]['cadence'] == 'medium'


@pytest.mark.parametrize('cadence,days,due', [('high', 1, True), ('medium', 1, False),
    ('medium', 7, True), ('low', 7, False), ('low', 28, True)])
def test_trial_honors_scope_cadence_without_monthly_override(scope, cadence, days, due):
    pipeline, pid = scope
    watch = add(pipeline, pid, cadence=cadence)
    assert watch['cadence'] == cadence and watch['status'] == 'trial'
    assert watch['id'] in {w['id'] for w in pipeline.monitoring_plan.due(pid)}
    pipeline.monitoring_plan.mark_run([watch['id']])
    later = datetime.now(UTC) + timedelta(days=days)
    assert (watch['id'] in {w['id'] for w in pipeline.monitoring_plan.due(pid, at=later)}) is due


def test_existing_low_trial_and_owner_choices_are_not_rewritten(scope):
    pipeline, pid = scope
    watch = add(pipeline, pid, cadence='low')
    with closing(pipeline.store._connect()) as db:
        original = dict(db.execute('SELECT * FROM studio_watchpoints WHERE id=?', (watch['id'],)).fetchone())
    pipeline.monitoring_plan.items(pid)
    pipeline.monitoring_plan.due(pid)
    with closing(pipeline.store._connect()) as db:
        assert dict(db.execute('SELECT * FROM studio_watchpoints WHERE id=?', (watch['id'],)).fetchone()) == original
    edited = pipeline.monitoring_plan.update(watch['id'], {'cadence': 'high'}, actor='human', reason='Owner cadence')
    pipeline.monitoring_plan.mark_run([watch['id']])
    assert edited['status'] == 'trial' and edited['cadence'] == 'high'
    assert watch['id'] in {w['id'] for w in pipeline.monitoring_plan.due(pid)}
    with pytest.raises(PermissionError):
        pipeline.monitoring_plan.update(watch['id'], {'cadence': 'low'}, actor='system', reason='Cannot overrule owner')


def test_retired_and_event_gated_questions_do_not_gain_initial_lookup(scope):
    pipeline, pid = scope
    watch = add(pipeline, pid)
    pipeline.monitoring_plan.update(watch['id'], {'status': 'retired'}, actor='human')
    question = pipeline.monitoring_plan.add(pid, dict(text='Synthetic event question', queries=['Synthetic event'],
        tier='topic', cadence='event', exit_rule='A public answer is available'),
        actor='system', origin='system', visibility='public')
    pipeline.monitoring_plan.explorations.refresh(pid)
    assert question['cadence'] == 'event' and not pipeline.monitoring_plan.due(pid)


def test_explicit_reduction_keeps_reduced_schedule(scope):
    pipeline, pid = scope
    watch = add(pipeline, pid, cadence='high')
    pipeline.monitoring_plan.update(watch['id'], {'status': 'reduced'}, actor='human')
    pipeline.monitoring_plan.mark_run([watch['id']])
    assert not pipeline.monitoring_plan.due(pid, at=datetime.now(UTC) + timedelta(days=7))
