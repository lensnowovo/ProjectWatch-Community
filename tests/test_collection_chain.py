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


def test_one_admitted_duplicate_does_not_discard_the_rest(host):
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


def test_chain_report_keeps_its_category(host):
    research, unit, ingest = host
    ingest(unit | {'id': 'chain-unit', 'category': 'chain'}, [trial()])
    assert [r['category'] for r in rows(research, 'studio_items')] == ['chain']


def test_technology_reports_still_feed_the_industry_page(host):
    research, unit, ingest = host
    ingest(unit | {'id': 'tech-unit', 'category': 'technology'}, [trial()])
    assert [r['category'] for r in rows(research, 'studio_items')] == ['industry']


def test_chain_material_only_invalidates_the_chain_page():
    from project_watch.studio.analyze_job import AFFECTS
    assert AFFECTS['chain'] == ('chain',)


def test_chain_and_technology_rounds_reach_source_health():
    from project_watch.studio.health import SOURCE_KINDS
    assert {'chain', 'technology'} <= SOURCE_KINDS


def test_incremental_round_rotates_two_chain_relations():
    """Relations used to run only on first/refresh, so ordinary rounds never searched a supplier."""
    watches = [watch(id=f'relation-{i}', queries=[f'Synthetic Supplier {i}']) for i in range(5)]
    plan = build(mode='incremental', watches=watches)
    chain = [u for u in plan['units'] if u['category'] == 'chain' and u['target_id'].startswith('watch:')]
    assert len(chain) == 5
    assert sum(u['schedule_due'] for u in chain) == 2


def test_non_chain_relations_are_still_not_pulled_into_incremental_rounds():
    watches = [watch(id='competitor-1', relation_type='competitor', queries=['Synthetic Rival'])]
    plan = build(mode='incremental', watches=watches)
    competitor = [u for u in plan['units'] if u['target_id'].startswith('watch:')]
    assert competitor and not any(u['schedule_due'] for u in competitor)


def test_rotation_picks_the_least_recently_run_relations():
    """Ordering by last_run_at only works if the field reaches the plan; it used to be stripped."""
    from project_watch.studio.collect_job import WATCH_FIELDS
    assert {'cadence', 'last_run_at'} <= set(WATCH_FIELDS)
    watches = [watch(id=f'relation-{i}', queries=[f'Synthetic Supplier {i}'],
                     last_run_at=f'2026-10-{i + 1:02d}T00:00:00+08:00') for i in range(5)]
    plan = build(mode='incremental', watches=watches)
    due = {u['target_id'] for u in plan['units']
           if u['category'] == 'chain' and u['schedule_due'] and u['target_id'].startswith('watch:')}
    assert due == {'watch:relation-0', 'watch:relation-1'}


def test_event_driven_and_unscoped_chain_relations_are_not_rotated_in():
    """Rotation must respect the same gates as cadence scheduling, or it re-arms event watches."""
    watches = [watch(id='event-1', cadence='event'), watch(id='unscoped-1', queries=[]),
               watch(id='plain-1', queries=['Synthetic Supplier'])]
    plan = build(mode='incremental', watches=watches)
    due = {u['target_id'] for u in plan['units']
           if u['category'] == 'chain' and u['schedule_due'] and u['target_id'].startswith('watch:')}
    assert due == {'watch:plain-1'}


def test_rotation_does_not_spend_a_slot_on_a_relation_already_due():
    watches = [watch(id='relation-0', last_run_at='2026-09-01T00:00:00+08:00'),
               watch(id='relation-1', last_run_at='2026-09-02T00:00:00+08:00'),
               watch(id='relation-2', last_run_at='2026-09-03T00:00:00+08:00')]
    plan = build(mode='incremental', watches=watches, due_watch_ids=['relation-0'])
    added = {u['target_id'] for u in plan['units']
             if u['category'] == 'chain' and u['schedule_due']
             and u['target_id'].startswith('watch:')} - {'watch:relation-0'}
    assert added == {'watch:relation-1', 'watch:relation-2'}
