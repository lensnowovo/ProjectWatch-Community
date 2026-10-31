"""Saved raw materials remain usable without any published event or collector."""
import sqlite3
from contextlib import closing

import pytest
import test_collection_ingest as collection_fixtures
from test_collection_ingest import trial

from project_watch.studio import material_snapshot


@pytest.fixture
def host(tmp_path):
    return collection_fixtures.host.__wrapped__(tmp_path)


def test_snapshot_uses_saved_originals_without_events_and_is_immutable(host):
    research, unit, ingest = host
    ingest(unit, [trial()])
    saved = material_snapshot.capture(research.execution)
    assert research.store.list_events(project_id=research.project_id) == []
    assert len(saved['materials']) == 1
    context = material_snapshot.read(research.store, research.project_id, saved['id'])
    assert 'RECRUITING' in context['materials'][0]['evidence']['parts'][0]['text']
    assert context['materials'][0]['classification'] == 'candidate'
    assert material_snapshot.capture(research.execution) == saved
    with closing(research.store._connect()) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute('UPDATE studio_insight_versions SET record=? WHERE id=?', ('{}', saved['id']))


def test_explicit_successor_keeps_other_material_and_old_manifest(host):
    research, unit, ingest = host
    ingest(unit, [trial()])
    second = trial(url='https://clinicaltrials.gov/study/NCT00000002')
    second['extra'].update(source_identity='clinical:NCT00000002', nct_id='NCT00000002')
    ingest(unit | {'id': 'second'}, [second])
    saved = material_snapshot.capture(research.execution)
    assert len(saved['materials']) == 2
    old_id = saved['materials'][0]['evidence_version_id']
    research.store.evidence.dependencies.withdraw(research.project_id, old_id, 'Synthetic withdrawal', actor='test')
    successor = material_snapshot.derive_permitted(research.execution, saved['id'])
    assert successor['parent_snapshot_id'] == saved['id']
    assert len(successor['materials']) == 1
    assert successor['materials'][0] == saved['materials'][1]
    assert material_snapshot.derive_permitted(research.execution, saved['id']) == successor
    assert len(material_snapshot.read(research.store, research.project_id, successor['id'])['materials']) == 1
    assert material_snapshot.capture(research.execution) == saved
    with pytest.raises(ValueError, match='withdrawn'):
        material_snapshot.read(research.store, research.project_id, saved['id'])


def test_changed_source_keeps_fixed_snapshot_but_revocation_blocks_read(host):
    research, unit, ingest = host
    ingest(unit, [trial()])
    saved = material_snapshot.capture(research.execution)
    ingest(unit | {'id': 'later', 'window_end': '2026-10-28T12:00:00+08:00'}, [trial('COMPLETED')])
    assert 'RECRUITING' in material_snapshot.read(research.store, research.project_id, saved['id'])['materials'][0]['evidence']['parts'][0]['text']
    old = saved['materials'][0]['evidence_version_id']
    research.store.evidence.dependencies.withdraw(research.project_id, old, 'Synthetic withdrawal', actor='test')
    with pytest.raises(ValueError, match='withdrawn'):
        material_snapshot.read(research.store, research.project_id, saved['id'])


def test_snapshot_does_not_cross_project_or_truncate_long_material(host):
    research, unit, ingest = host
    item = trial()
    item['snippet'] = '甲' * 21000 + '最后一段反证'
    ingest(unit, [item])
    saved = material_snapshot.capture(research.execution)
    context = material_snapshot.read(research.store, research.project_id, saved['id'])
    parts = context['materials'][0]['evidence']['parts']
    assert len(parts) > 1 and '最后一段反证' in ''.join(p['text'] for p in parts)
    with pytest.raises(KeyError):
        material_snapshot.read(research.store, 'another-project', saved['id'])




def test_later_item_exclusion_blocks_historical_snapshot(host):
    research, unit, ingest = host
    ref = ingest(unit, [trial()])['evidence_refs'][0]
    saved = material_snapshot.capture(research.execution)
    ingest(unit | {'id': 'later', 'window_end': '2026-10-28T12:00:00+08:00'}, [trial('COMPLETED')])
    research.store.set_item_status(ref['item_id'], 'dismissed')
    with pytest.raises(ValueError, match='excluded'):
        material_snapshot.read(research.store, research.project_id, saved['id'])


def test_legacy_adopted_item_exclusion_blocks_snapshot(host):
    research, unit, ingest = host
    research.store.add_items(research.project_id, 'trial', [trial()])
    ref = ingest(unit, [trial()])['evidence_refs'][0]
    saved = material_snapshot.capture(research.execution)
    research.store.set_item_status(ref['item_id'], 'dismissed')
    with pytest.raises(ValueError, match='excluded'):
        material_snapshot.read(research.store, research.project_id, saved['id'])










