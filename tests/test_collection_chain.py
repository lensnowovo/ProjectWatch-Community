"""Chain collection: identity, isolation and scheduling. Synthetic fixtures, isolated SQLite."""
from test_collection_ingest import host, rows, trial  # noqa: F401  (host is a pytest fixture)
from test_collection_scope import build, watch

from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.sources import _item


def other_trial(n=2):
    return _item('trial', f'Synthetic study {n}', f'https://clinicaltrials.gov/study/NCT0000000{n}',
                 source='ClinicalTrials.gov', snippet='Synthetic source metadata', published_at='2024-01-03',
                 extra={'source_identity': f'clinical:NCT0000000{n}', 'nct_id': f'NCT0000000{n}',
                        'status': 'RECRUITING',
                        'source_record': {'nctId': f'NCT0000000{n}', 'overallStatus': 'RECRUITING'}})


def test_one_admitted_duplicate_does_not_discard_the_rest(host):  # noqa: F811  (pytest fixture)
    """The failure that emptied whole chain queries: one re-seen record rolled back the batch."""
    research, unit, _ = host
    # The collect job grants source-only ingest, so an already-triaged record is refused.
    ingest = CollectionIngest(research.execution, admit_materials=False)
    first = ingest(unit, [trial()])
    research.store.set_item_status(first['evidence_refs'][0]['item_id'], 'dismissed')
    result = ingest(unit | {'id': 'next-unit'}, [trial(), other_trial()])
    assert result['skipped'] == ['source_only_admitted_item']
    assert result['skipped_count'] == 1
    assert result['saved_count'] == 1
    assert len(rows(research, 'studio_items')) == 2


def test_chain_report_keeps_its_category(host):  # noqa: F811  (pytest fixture)
    research, unit, ingest = host
    ingest(unit | {'id': 'chain-unit', 'category': 'chain'}, [trial()])
    assert [r['category'] for r in rows(research, 'studio_items')] == ['chain']


def test_technology_reports_still_feed_the_industry_page(host):  # noqa: F811  (pytest fixture)
    research, unit, ingest = host
    ingest(unit | {'id': 'tech-unit', 'category': 'technology'}, [trial()])
    assert [r['category'] for r in rows(research, 'studio_items')] == ['industry']


def test_chain_material_only_invalidates_the_chain_page():
    from project_watch.studio.analyze_job import AFFECTS
    assert AFFECTS['chain'] == ('chain',)


def test_chain_and_technology_rounds_reach_source_health():
    from project_watch.studio.health import SOURCE_KINDS
    assert {'chain', 'technology'} <= SOURCE_KINDS


def test_incremental_includes_every_due_chain_relation():
    watches = [watch(id=f'relation-{i}', queries=[f'Synthetic Supplier {i}']) for i in range(7)]
    plan = build(mode='incremental', watches=watches, due_watch_ids=[w['id'] for w in watches])
    due = {u['target_id'] for u in plan['units'] if u['schedule_due'] and u['target_id'].startswith('watch:')}
    assert due == {'watch:' + w['id'] for w in watches}


def test_incremental_does_not_wake_not_due_or_event_relations():
    watches = [watch(id='due'), watch(id='not-due'), watch(id='event', cadence='event')]
    plan = build(mode='incremental', watches=watches, due_watch_ids=['due'])
    due = {u['target_id'] for u in plan['units'] if u['schedule_due'] and u['target_id'].startswith('watch:')}
    assert due == {'watch:due'}


def test_first_and_refresh_still_cover_all_relations():
    watches = [watch(id=f'relation-{i}') for i in range(7)]
    for mode in ('first', 'refresh'):
        plan = build(mode=mode, watches=watches)
        due = {u['target_id'] for u in plan['units'] if u['schedule_due'] and u['target_id'].startswith('watch:')}
        assert due == {'watch:' + w['id'] for w in watches}
