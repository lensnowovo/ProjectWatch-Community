"""Saved raw materials remain usable without any published event or collector."""
import sqlite3
from contextlib import closing

import pytest
import test_collection_ingest as collection_fixtures
from test_collection_ingest import trial

from project_watch.studio import material_snapshot
from project_watch.studio.http import HttpError, Response


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


def test_failed_analysis_can_be_redone_without_recollecting_or_publishing(host, monkeypatch):
    research, unit, ingest = host
    ingest(unit, [trial()])
    saved = material_snapshot.capture(research.execution)
    from project_watch.studio import collection_runner
    monkeypatch.setattr(collection_runner, 'run_collection', lambda *a, **k: pytest.fail('must not collect'))
    before = material_snapshot.read(research.store, research.project_id, saved['id'])
    def fail(_context):
        raise ValueError('synthetic analysis failure')
    with pytest.raises(ValueError):
        material_snapshot.analyze_saved(research.execution, saved['id'], attempt_key='first', analyze=fail)
    # Replaying a failed attempt is not an implicit model retry.
    with pytest.raises(ValueError, match='new analysis attempt'):
        material_snapshot.analyze_saved(research.execution, saved['id'], attempt_key='first', analyze=fail)
    calls = []
    def analyze(context):
        calls.append(context['id'])
        return {'gaps': ['synthetic missing financial evidence']}
    result = material_snapshot.analyze_saved(research.execution, saved['id'], attempt_key='second', analyze=analyze)
    assert material_snapshot.analyze_saved(research.execution, saved['id'], attempt_key='second', analyze=analyze) == result
    assert calls == [saved['id']]
    assert result['status'] == 'draft'  # Successful generation does not publish unreviewed claims.
    assert material_snapshot.read(research.store, research.project_id, saved['id']) == before
    assert research.store.list_events(project_id=research.project_id) == []


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


def test_empty_snapshot_never_invokes_analysis(host):
    research, _unit, _ingest = host
    empty = material_snapshot.capture(research.execution)
    with pytest.raises(ValueError, match='no permitted material'):
        material_snapshot.analyze_saved(research.execution, empty['id'], attempt_key='empty',
                                        analyze=lambda _: pytest.fail('no materials'))


def test_revocation_during_generation_retains_failure_not_usable_draft(host):
    research, unit, ingest = host
    ref = ingest(unit, [trial()])['evidence_refs'][0]
    saved = material_snapshot.capture(research.execution)
    def revoke(_):
        research.store.evidence.dependencies.withdraw(research.project_id, ref['evidence_version_id'],
                                                     'Synthetic withdrawal during generation', actor='test')
        return {'headline': 'must not become a usable judgment'}
    with pytest.raises(ValueError, match='withdrawn'):
        material_snapshot.analyze_saved(research.execution, saved['id'], attempt_key='revoked', analyze=revoke)
    with closing(research.store._connect()) as db:
        rows = db.execute("SELECT stage FROM studio_job_checkpoints WHERE job_id=? AND stage LIKE 'snapshot-analysis:%'",
                          (research.execution.job['id'],)).fetchall()
    assert len(rows) == 1
    receipt = research.execution.read_checkpoint(rows[0][0])
    assert receipt['status'] == 'failed' and 'draft' not in receipt


def test_manual_pause_prevents_new_attempt_without_changing_snapshot(host):
    research, unit, ingest = host
    ingest(unit, [trial()])
    saved = material_snapshot.capture(research.execution)
    research.store.set_status(research.project_id, 'paused')
    with pytest.raises(HttpError, match='project_paused'):
        material_snapshot.analyze_saved(research.execution, saved['id'], attempt_key='paused',
                                        analyze=lambda _: pytest.fail('paused project must not dispatch'))
    assert material_snapshot.read(research.store, research.project_id, saved['id'])['id'] == saved['id']


def test_revoked_project_consent_prevents_analysis_but_not_local_read(host):
    research, unit, ingest = host
    ingest(unit, [trial()])
    saved = material_snapshot.capture(research.execution)
    with closing(research.store._connect()) as db, db:
        db.execute('UPDATE studio_projects SET consent_at=NULL WHERE project_id=?', (research.project_id,))
    with pytest.raises(HttpError, match='project_consent_required'):
        material_snapshot.analyze_saved(research.execution, saved['id'], attempt_key='no-consent',
                                        analyze=lambda _: pytest.fail('no disclosure consent'))
    assert material_snapshot.read(research.store, research.project_id, saved['id'])['id'] == saved['id']


def test_unknown_is_never_retried_and_received_calls_survive_new_attempt(host):
    research, unit, ingest = host
    ingest(unit, [trial()])
    saved = material_snapshot.capture(research.execution)
    execution = research.execution
    def settled_failure(_):
        execution.call('model', 'received', lambda: Response(200, {}, b'{}'), provider='synthetic')
        raise ValueError('received invalid draft')
    with pytest.raises(ValueError):
        material_snapshot.analyze_saved(execution, saved['id'], attempt_key='received', analyze=settled_failure)
    before = execution.usage()['model']['requests']
    def unknown(_):
        def timeout():
            raise HttpError('connection_failed')
        execution.call('model', 'uncertain', timeout, provider='synthetic')
    with pytest.raises(HttpError):
        material_snapshot.analyze_saved(execution, saved['id'], attempt_key='uncertain', analyze=unknown)
    assert execution.usage()['model']['requests'] == before + 1
    assert execution.usage()['model']['unknown'] == 1
    with closing(research.store._connect()) as db:
        stages = [r[0] for r in db.execute("SELECT stage FROM studio_job_checkpoints WHERE job_id=? "
                                           "AND stage LIKE 'snapshot-analysis:%'", (execution.job['id'],))]
    assert sorted(execution.read_checkpoint(stage)['status'] for stage in stages) == ['failed', 'outcome_unknown']
    for key in ('uncertain', 'yet-another-key'):
        with pytest.raises(HttpError, match='request_outcome_unknown'):
            material_snapshot.analyze_saved(execution, saved['id'], attempt_key=key,
                                            analyze=lambda _: pytest.fail('unknown cannot be replayed'))
    assert execution.usage()['model']['requests'] == before + 1
    assert material_snapshot.read(research.store, research.project_id, saved['id'])['id'] == saved['id']
