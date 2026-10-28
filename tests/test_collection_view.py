"""Saved source visibility is independent of model admission and task success."""
from contextlib import closing

import pytest
import test_collection_decisions as fixtures
from test_collection_ingest import trial
from test_collection_plan import candidate, plan

from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.collection_view import discovered_sources
from project_watch.studio.execution import JobExecution
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart


@pytest.fixture
def owner(tmp_path):
    return fixtures.owner.__wrapped__(tmp_path)


def save(owner):
    pipe, _client, pid, _ = owner
    pipe.store.enqueue(pid, 'external')
    execution = JobExecution(pipe.store, pipe.store.claim())
    unit = plan([candidate() | dict(source='clinical_term', category='trial')],
                project_id=pid, round_id=execution.job['id'])['units'][0]
    ingest = CollectionIngest(execution)
    receipt = ingest(unit, [trial()])
    return execution, unit, ingest, receipt['evidence_refs'][0]


def test_source_visible_before_review_after_failure_and_reopen(owner):
    pipe, client, pid, _ = owner
    execution, _, _, ref = save(owner)
    route = f'/api/studio/projects/{pid}/discovered-sources'
    first = client.get(route).json()
    assert first['total'] == first['pending'] == 1
    item = first['items'][0]
    assert item['id'] == ref['item_id'] and item['processing_status'] == 'pending'
    assert item['evidence_version_id'] == ref['evidence_version_id']
    assert item['source_type'] == 'structured_record' and item['read_scope'] == 'partial'
    assert pipe.store.list_items(project_id=pid) == []  # not accepted news
    pipe.store.finish(execution.job['id'], lease_token=execution.job['lease_token'], error='request_outcome_unknown')
    assert client.get(route).json() == first
    from project_watch.studio.store import StudioStore
    reopened = StudioStore(pipe.store.db_path)
    assert discovered_sources(reopened, pid) == first
    assert not execution.usage()


def test_replay_deduplicates_and_reads_do_not_write(owner):
    pipe, client, pid, _ = owner
    execution, unit, ingest, _ = save(owner)
    ingest(unit, [trial()])
    with closing(pipe.store._connect()) as db:
        before = list(db.iterdump())
    assert client.get(f'/api/studio/projects/{pid}/discovered-sources').json()['total'] == 1
    with closing(pipe.store._connect()) as db:
        assert list(db.iterdump()) == before
    assert not execution.usage()


@pytest.mark.parametrize('boundary', ['dismissed', 'irrelevant', 'private', 'foreign_evidence', 'withdrawn'])
def test_hidden_or_foreign_sources_never_leak(owner, boundary):
    pipe, client, pid, _ = owner
    _, _, _, ref = save(owner)
    private = pipe.evidence.save(EvidenceInput(project_id=pid, source_key='synthetic-private',
        extraction_version='test-v1', source_type='document', visibility='internal', read_scope='full',
        parts=[EvidencePart(locator_kind='stored_text', locator='page 1', text='Synthetic private material')])) if boundary == 'private' else None
    if boundary == 'withdrawn':
        pipe.evidence.dependencies.withdraw(pid, ref['evidence_version_id'], 'synthetic correction', actor='test')
    with closing(pipe.store._connect()) as db, db:
        if boundary == 'dismissed':
            db.execute("UPDATE studio_items SET status='dismissed' WHERE id=?", (ref['item_id'],))
        elif boundary == 'irrelevant':
            db.execute('UPDATE studio_items SET relevant=0,triaged=1 WHERE id=?', (ref['item_id'],))
        elif boundary == 'private':
            db.execute('UPDATE studio_collection_sources SET evidence_version_id=? WHERE item_id=?', (private['id'], ref['item_id']))
        elif boundary == 'foreign_evidence':
            other = pipe.repository.create_project('Another Synthetic Company')
            db.execute('UPDATE studio_collection_sources SET project_id=? WHERE item_id=?', (other.id, ref['item_id']))
    assert discovered_sources(pipe.store, pid)['total'] == 0
    assert discovered_sources(pipe.store, 'another-project')['total'] == 0
    assert client.get('/api/studio/projects/missing/discovered-sources').status_code == 404


def test_legacy_empty_and_page_validation(owner):
    pipe, _, pid, _ = owner
    assert discovered_sources(pipe.store, pid)['items'] == []
    for kwargs in ({'limit': 0}, {'offset': -1}, {'limit': True}):
        with pytest.raises(ValueError):
            discovered_sources(pipe.store, pid, **kwargs)


def test_pending_and_organized_status_dates_and_pages(owner):
    pipe, _, pid, _ = owner
    _, unit, ingest, ref = save(owner)
    second = trial(url='https://example.org/another')
    second['extra']['source_identity'] = 'clinical:NCT00000002'
    second['published_at'] = None
    ingest(unit, [second])
    with closing(pipe.store._connect()) as db, db:
        db.execute('UPDATE studio_items SET triaged=1,relevant=1 WHERE id=?', (ref['item_id'],))
    first = discovered_sources(pipe.store, pid, limit=1)
    last = discovered_sources(pipe.store, pid, limit=1, offset=first['next_offset'])
    assert first['total'] == 2 and first['pending'] == 1
    assert last['next_offset'] is None
    assert first['items'][0]['id'] != last['items'][0]['id']
    items = first['items'] + last['items']
    assert {i['processing_status'] for i in items} == {'pending', 'organized'}
    assert any(i['published_at'] is None for i in items)


def test_body_quality_refresh_keeps_discovery_reference_and_failed_read_is_not_full(owner):
    pipe, client, pid, _ = owner
    _, unit, ingest, _ = save(owner)
    source = trial(url='https://example.org/news')
    source['extra'] = {}
    ref = ingest(unit, [source])['evidence_refs'][0]
    route = f'/api/studio/projects/{pid}/discovered-sources'

    def row():
        return next(i for i in client.get(route).json()['items'] if i['id'] == ref['item_id'])

    assert row()['read_scope'] == 'summary'
    pipe.store.save_body(ref['item_id'], 'article_body', 'Synthetic own article text')
    body = row()
    assert body['source_type'] == 'public_page' and body['read_scope'] == 'partial'
    assert body['evidence_version_id'] == ref['evidence_version_id']
    assert body['body_evidence_version_id'] != ref['evidence_version_id']
    pipe.store.save_body(ref['item_id'], 'failed', '')
    assert row()['read_scope'] == 'summary' and row()['read_status'] == 'failed'
    # A retained but withdrawn body is not replaced with the old discovery
    # snapshot to disguise withdrawal as an ordinary pending source.
    pipe.evidence.dependencies.withdraw(pid, body['body_evidence_version_id'], 'synthetic correction', actor='test')
    assert client.get(route).json()['total'] == 1
    assert all(i['id'] != ref['item_id'] for i in client.get(route).json()['items'])


def test_donor_body_does_not_upgrade_own_source_quality(owner):
    pipe, _, pid, _ = owner
    _, unit, ingest, donor = save(owner)
    source = trial(url='https://example.org/news')
    source['extra'] = {}
    ref = ingest(unit, [source])['evidence_refs'][0]
    pipe.store.save_body(donor['item_id'], 'article_body', 'Synthetic donor article')
    pipe.store.save_body(ref['item_id'], 'twin_body', 'Synthetic donor article',
                        donor={'id': donor['item_id'], 'title': 'Donor', 'url': 'https://example.org/donor'})
    item = next(i for i in discovered_sources(pipe.store, pid)['items'] if i['id'] == ref['item_id'])
    assert item['reading_basis'] == 'donor_text' and item['read_scope'] == 'summary'
