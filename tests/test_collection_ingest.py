"""Source versions and ingestion recovery on real isolated SQLite storage."""
import json
from contextlib import closing
from copy import deepcopy

import pytest
import test_studio_dsh_monitor as fixtures
from test_collection_plan import candidate, plan

from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.collection_ledger import CollectionLedger
from project_watch.studio.collection_runner import run_collection
from project_watch.studio.http import HttpError
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart
from project_watch.studio.sources import _item


@pytest.fixture
def host(tmp_path):
    research = fixtures.loop.__wrapped__(tmp_path)
    p = plan([candidate() | {'source': 'clinical_term', 'category': 'trial'}],
             round_id=research.execution.job['id'], project_id=research.project_id)
    return research, p['units'][0], CollectionIngest(research.execution)


def trial(status='RECRUITING', url='https://clinicaltrials.gov/study/NCT00000001'):
    return _item('trial', 'Synthetic study', url, source='ClinicalTrials.gov',
                 snippet='Synthetic source metadata', published_at='2024-01-02',
                 extra={'source_identity': 'clinical:NCT00000001', 'nct_id': 'NCT00000001',
                        'status': status, 'source_record': {'nctId': 'NCT00000001', 'overallStatus': status}})


def rows(research, table):
    assert table in ('studio_items', 'studio_evidence_versions', 'studio_collection_observations')
    with closing(research.store._connect()) as db:
        return [dict(r) for r in db.execute('SELECT * FROM ' + table)]


def test_same_source_update_keeps_item_identity_status_and_both_evidence_versions(host):
    research, unit, ingest = host
    first = ingest(unit, [trial()])
    item_id = first['evidence_refs'][0]['item_id']
    research.store.set_item_status(item_id, 'dismissed')
    changed = trial('COMPLETED', 'https://clinicaltrials.gov/study/NCT00000001?updated=1')
    second = ingest(unit | {'id': 'next-unit', 'window_end': '2026-10-27T12:00:00+08:00'}, [changed])
    assert first['added_count'] == second['updated_count'] == 1
    assert second['evidence_refs'][0]['item_id'] == item_id
    [item] = rows(research, 'studio_items')
    assert item['status'] == 'dismissed' and item['triaged'] == 0
    assert json.loads(item['extra'])['status'] == 'COMPLETED'
    assert len(rows(research, 'studio_evidence_versions')) == 2
    old = research.pipeline.evidence.get(research.project_id, first['evidence_refs'][0]['evidence_version_id'])
    assert 'RECRUITING' in old['parts'][0]['text']
    assert old['source_type'] == 'structured_record' and old['read_scope'] == 'partial'
    assert research.store.list_events(project_id=research.project_id) == []


def test_replaying_saved_unit_keeps_original_receipt_counts(host):
    research, unit, ingest = host
    first = ingest(unit, [trial(), trial()])
    second = ingest(unit, [trial()])
    assert first == second and first['saved_count'] == first['added_count'] == 1
    assert len(rows(research, 'studio_items')) == len(rows(research, 'studio_evidence_versions')) == 1
    assert len(rows(research, 'studio_collection_observations')) == 1


def test_later_query_reuses_evidence_but_keeps_own_target_and_direction(host):
    research, unit, ingest = host
    first = ingest(unit, [trial()])
    second = ingest(unit | {'id': 'another-query', 'category': 'technology', 'target_id': 'related-entity'}, [trial()])
    assert second['reused_count'] == 1 and second['added_count'] == 0
    ref = second['evidence_refs'][0]
    assert ref['evidence_version_id'] == first['evidence_refs'][0]['evidence_version_id']
    assert ref['direction'] == 'technology' and ref['target_id'] == 'related-entity'
    assert len(rows(research, 'studio_collection_observations')) == 2


def test_cached_older_record_cannot_replace_newer_fields_or_trigger_dependencies(host, monkeypatch):
    research, unit, ingest = host
    current = ingest(unit, [trial('COMPLETED')])
    execution = research.execution
    older = unit | {'id': 'cached-old-query'}
    execution.checkpoint_chunked('collection_payload:cached-old-query', {
        'cache_provenance': [{'completed_at': '2026-01-01T00:00:00+00:00'}]})
    monkeypatch.setattr(research.store.evidence.dependencies, 'on_saved',
                        lambda *_a, **_k: pytest.fail('historical cache must not invalidate current evidence'))
    result = ingest(older, [trial('RECRUITING')])
    assert result['historical_count'] == 1
    [item] = rows(research, 'studio_items')
    assert json.loads(item['extra'])['status'] == 'COMPLETED'
    with closing(research.store._connect()) as db:
        head = db.execute('SELECT evidence_version_id FROM studio_evidence_heads WHERE source_key LIKE ?',
                          ('collection:%',)).fetchone()[0]
    assert head == current['evidence_refs'][0]['evidence_version_id']
    assert len(rows(research, 'studio_evidence_versions')) == 2


def test_legacy_url_item_is_adopted_without_losing_status_or_user_metadata(host):
    research, unit, ingest = host
    old = trial()
    old['extra']['user_note'] = 'Synthetic saved note'
    research.store.add_items(research.project_id, 'trial', [old])
    [before] = rows(research, 'studio_items')
    research.store.set_item_status(before['id'], 'read')
    result = ingest(unit, [trial('COMPLETED')])
    [after] = rows(research, 'studio_items')
    assert after['id'] == before['id'] and after['status'] == 'read'
    assert json.loads(after['extra'])['user_note'] == 'Synthetic saved note'
    assert result['updated_count'] == 1


def test_legacy_stable_source_id_survives_url_change(host):
    research, unit, ingest = host
    old = trial()
    old['extra'].pop('source_identity')
    research.store.add_items(research.project_id, 'trial', [old])
    [before] = rows(research, 'studio_items')
    result = ingest(unit, [trial('COMPLETED', 'https://clinicaltrials.gov/study/NCT00000001?new=1')])
    assert len(rows(research, 'studio_items')) == 1
    assert result['evidence_refs'][0]['item_id'] == before['id']


def test_older_cached_search_cannot_downgrade_structured_record(host):
    research, unit, ingest = host
    ingest(unit, [trial('COMPLETED')])
    old = _item('web', 'Old search title', trial()['url'], source='Synthetic', snippet='Older search excerpt')
    result = ingest(unit | {'id': 'old-search', 'window_end': '2026-01-01T00:00:00+00:00'}, [old])
    assert result['historical_count'] == 1
    [item] = rows(research, 'studio_items')
    assert item['title'] == 'Synthetic study' and json.loads(item['extra'])['status'] == 'COMPLETED'


def test_source_batch_is_atomic_when_evidence_save_fails(host, monkeypatch):
    research, unit, ingest = host
    second = trial()
    second['extra']['source_identity'] = 'clinical:NCT00000002'
    second['url'] = 'https://clinicaltrials.gov/study/NCT00000002'
    original = research.store.evidence.save
    calls = []

    def fail(evidence, **kwargs):
        calls.append(evidence)
        if len(calls) == 2:
            raise RuntimeError('synthetic storage failure')
        return original(evidence, **kwargs)

    monkeypatch.setattr(research.store.evidence, 'save', fail)
    with pytest.raises(RuntimeError, match='synthetic storage failure'):
        ingest(unit, [trial(), second])
    assert rows(research, 'studio_items') == rows(research, 'studio_evidence_versions') == []
    assert rows(research, 'studio_collection_observations') == []


def test_expired_lease_cannot_import_source_data(host):
    research, unit, ingest = host
    with closing(research.store._connect()) as db, db:
        db.execute('UPDATE studio_jobs SET lease_token=? WHERE id=?', ('replacement', research.execution.job['id']))
    with pytest.raises(RuntimeError, match='lease'):
        ingest(unit, [trial()])
    assert rows(research, 'studio_items') == []


def test_conflicting_source_versions_are_not_silently_merged(host):
    research, unit, ingest = host
    with pytest.raises(HttpError, match='invalid_collection_record'):
        ingest(unit, [trial(), trial('COMPLETED')])
    assert rows(research, 'studio_items') == []


def test_search_summary_stays_summary_and_does_not_mark_article_read(host):
    research, unit, ingest = host
    item = _item('web', 'Synthetic news', 'https://example.org/news', source='Synthetic', snippet='Short excerpt')
    result = ingest(unit | {'category': 'chain', 'source': 'search'}, [item])
    ref = result['evidence_refs'][0]
    assert ref['source_type'] == 'search_summary' and ref['read_scope'] == 'summary'
    [stored] = rows(research, 'studio_items')
    assert stored['body_status'] == '' and stored['triaged'] == 0
    assert ref['direction'] == 'chain'


def test_host_runner_retains_ingestion_ids_and_replays_without_new_items(host):
    research, unit, ingest = host
    p = plan([candidate() | {'source': unit['source'], 'category': unit['category']}],
             round_id=research.execution.job['id'], project_id=research.project_id)
    ledger = CollectionLedger(research.execution, p)
    calls = []

    def collect(_unit):
        calls.append(_unit['id'])
        return dict(items=[trial()], request_ids=['synthetic-request'], truncated=False)

    for _ in range(2):
        result = run_collection(ledger, collect=collect, ingest=ingest, guard=research.guard)
    assert result['status'] == 'complete' and len(calls) == 1
    assert ledger.receipts[0]['added_count'] == 1
    assert ledger.receipts[0]['evidence_refs'][0]['source_type'] == 'structured_record'
    assert len(rows(research, 'studio_items')) == 1


def test_structured_record_contract_cannot_claim_full_document_read():
    data = dict(project_id='synthetic', source_key='source:1', extraction_version='v1',
                visibility='public', source_type='structured_record', read_scope='partial',
                parts=[EvidencePart(locator_kind='stored_text', locator='source JSON', text='{"id":1}')])
    assert EvidenceInput(**data).source_type == 'structured_record'
    for changes in ({'read_scope': 'full'}, {'visibility': 'internal'},
                    {'parts': [EvidencePart(locator_kind='pdf_page', locator='1', text='text')]}):
        with pytest.raises(ValueError):
            EvidenceInput(**(deepcopy(data) | changes))
