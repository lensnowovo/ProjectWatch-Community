"""Receipt-to-page projection with synthetic stores and no provider calls."""
from test_collection_plan import candidate, plan, success

from project_watch.studio.collection_plan import channel_status, reconcile


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


