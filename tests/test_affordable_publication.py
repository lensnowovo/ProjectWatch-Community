"""Low explicit budgets through normal Pipeline dispatch, without paid traffic."""
import pytest
from test_event_insight import install

from project_watch.studio import collection_host, event_insight_work


@pytest.mark.parametrize('monitor', [False, True])
@pytest.mark.parametrize('cap', [3, 4])
def test_small_cap_delivers_events_and_same_job_replays_without_new_calls(tmp_path, monkeypatch, monitor, cap):
    pipeline, execution, seen = install(tmp_path, monkeypatch, monitor=monitor)
    limit = collection_host.limit_budgets

    def bounded(current, mode):
        limit(current, mode)
        current.budgets['model'] = min(current.budgets['model'], cap)

    monkeypatch.setattr(collection_host, 'limit_budgets', bounded)
    pipeline.run(execution.job)
    state = execution.read_checkpoint(event_insight_work.ROUND)
    assert state['cap'] == state['collection_cap'] == cap
    assert state['publication_complete']
    events = pipeline.store.list_events(project_id=execution.job['project_id'])
    assert events and state['event_versions']
    used = execution.budget_used('model')
    assert 2 <= used <= cap
    assert execution.usage()['model']['unknown'] == 0
    calls = len(seen['models'])
    pipeline.run(execution.job)
    assert execution.budget_used('model') == used and len(seen['models']) == calls
    assert pipeline.store.list_events(project_id=execution.job['project_id']) == events


def test_previously_frozen_small_round_and_lower_executor_limit_are_not_expanded(tmp_path, monkeypatch):
    _pipeline, execution, _seen = install(tmp_path, monkeypatch)
    saved = dict(cap=4, collection_cap=2, before={}, phase='collecting', origin_job_id=execution.job['id'])
    execution.checkpoint(event_insight_work.ROUND, saved)
    execution.budgets['model'] = 1
    assert event_insight_work.begin(execution) == saved
    assert execution.budgets['model'] == 1
    assert not execution.usage()
