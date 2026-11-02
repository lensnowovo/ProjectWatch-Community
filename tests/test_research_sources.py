"""Professional views read saved public sources without admitting model events."""
import json
from contextlib import closing

import pytest
import test_collection_view as fixtures
from test_collection_ingest import trial
from test_collection_view import save

from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart
from project_watch.studio.research_sources import research_source_counts, research_sources


@pytest.fixture
def owner(tmp_path):
    return fixtures.owner.__wrapped__(tmp_path)


def route(pid, category='trial'):
    return f'/api/studio/projects/{pid}/research-sources?category={category}'


def test_new_project_and_legacy_pending_source_need_no_collection_round(owner):
    pipe, client, pid, _ = owner
    assert client.get(f'/api/studio/projects/{pid}').json()['research_source_counts'] == {'trial': 0, 'paper': 0, 'patent': 0}
    source = trial()
    pipe.store.add_items(pid, 'trial', [source])
    page = client.get(route(pid)).json()
    assert page['total'] == 1 and page['items'][0]['processing_status'] == 'pending'
    assert page['items'][0]['evidence_version_id'] is None
    pipe.store.set_item_status(page['items'][0]['id'], 'dismissed')
    assert client.get(route(pid)).json()['total'] == 0


def test_pending_sources_visible_after_unknown_without_event_or_write(owner):
    pipe, client, pid, _ = owner
    execution, _, _, ref = save(owner)
    pipe.store.finish(execution.job['id'], lease_token=execution.job['lease_token'], error='request_outcome_unknown')
    with closing(pipe.store._connect()) as db:
        before = list(db.iterdump())
    result = client.get(route(pid)).json()
    assert result['total'] == 1
    [source] = result['items']
    assert source['id'] == ref['item_id'] and source['processing_status'] == 'pending'
    assert source['extra']['nct_id'] == 'NCT00000001'
    assert source['source_type'] == 'structured_record'
    assert not {'summary', 'impact', 'importance', 'source_record'} & source.keys()
    assert 'source_record' not in source['extra']
    assert pipe.store.list_events(project_id=pid) == []
    assert research_source_counts(pipe.store, pid, review=None) == {'trial': 1, 'paper': 0, 'patent': 0}
    with closing(pipe.store._connect()) as db:
        assert list(db.iterdump()) == before
    assert execution.usage() == {}


@pytest.mark.parametrize('boundary', ['dismissed', 'irrelevant_pending', 'private', 'foreign', 'withdrawn',
                                    'private_head', 'withdrawn_head', 'private_body', 'withdrawn_body'])
def test_exclusions_and_evidence_heads_apply_before_counting(owner, boundary):
    pipe, client, pid, _ = owner
    _, _, _, ref = save(owner)
    if boundary.endswith('body'):
        pipe.store.save_body(ref['item_id'], 'article_body', 'Synthetic public article')
    with closing(pipe.store._connect()) as db, db:
        if boundary == 'dismissed':
            db.execute("UPDATE studio_items SET status='dismissed'")
        elif boundary == 'irrelevant_pending':
            db.execute('UPDATE studio_items SET relevant=0,triaged=0')
        elif boundary == 'foreign':
            other = pipe.repository.create_project('Synthetic Other')
            db.execute('UPDATE studio_collection_sources SET project_id=?', (other.id,))
    if boundary in ('private', 'private_head', 'private_body', 'withdrawn_head'):
        with closing(pipe.store._connect()) as db:
            key = db.execute('SELECT source_key FROM studio_evidence_versions WHERE id=?', (ref['evidence_version_id'],)).fetchone()[0]
        record = pipe.evidence.save(EvidenceInput(project_id=pid, source_key=key if boundary.endswith('head') else 'synthetic-private',
            extraction_version='new-v2', source_type='document', visibility='public' if boundary=='withdrawn_head' else 'internal',
            read_scope='full', parts=[EvidencePart(locator_kind='stored_text', locator='one', text='Synthetic private change')]))
        with closing(pipe.store._connect()) as db, db:
            if boundary == 'private':
                db.execute('UPDATE studio_collection_sources SET evidence_version_id=?', (record['id'],))
            elif boundary == 'private_body':
                db.execute('UPDATE studio_item_evidence SET evidence_version_id=?', (record['id'],))
        if boundary == 'withdrawn_head':
            pipe.evidence.dependencies.withdraw(pid, record['id'], 'synthetic correction', actor='test')
    if boundary in ('withdrawn', 'withdrawn_body'):
        target = ref['evidence_version_id'] if boundary == 'withdrawn' else pipe.store.item_evidence(pid, ref['item_id'])['evidence_version_id']
        pipe.evidence.dependencies.withdraw(pid, target, 'synthetic correction', actor='test')
    assert client.get(route(pid)).json()['total'] == 0
    assert research_source_counts(pipe.store, pid, review=None)['trial'] == 0


def test_legacy_records_remain_and_source_updates_deduplicate(owner):
    pipe, client, pid, _ = owner
    _, unit, ingest, _ = save(owner)
    legacy_source = trial(url='https://example.org/legacy')
    legacy_source['extra']['source_identity'] = 'clinical:NCT00000099'
    legacy_source['extra']['nct_id'] = 'NCT00000099'
    pipe.store.add_items(pid, 'trial', [legacy_source])
    with closing(pipe.store._connect()) as db, db:
        db.execute("UPDATE studio_items SET status='dismissed' WHERE key LIKE 'collection:%'")
    legacy = client.get(route(pid)).json()
    assert legacy['total'] == 1 and legacy['items'][0]['evidence_version_id'] is None
    legacy_source['extra']['status'] = 'COMPLETED'
    ingest(unit | {'id': 'new-unit', 'window_end': '2026-10-29T12:00:00+08:00'}, [legacy_source])
    current = client.get(route(pid)).json()
    assert current['total'] == 1 and current['items'][0]['id'] == legacy['items'][0]['id']
    assert current['items'][0]['extra']['status'] == 'COMPLETED'
    assert current['revision'] != legacy['revision']


def test_pages_over_200_use_stable_ids_and_reject_mixed_revision(owner):
    pipe, client, pid, _ = owner
    _, unit, ingest, _ = save(owner)
    # Distinct saved sources, not generated event projections.
    for batch in range(3):
        sources = []
        for n in range(batch*70+2, batch*70+72):
            source = trial(url=f'https://example.org/study/{n}')
            source['extra']['source_identity'] = f'clinical:NCT{n:08d}'
            source['extra']['nct_id'] = f'NCT{n:08d}'
            sources.append(source)
        ingest(unit | {'id': f'unit-{batch}'}, sources)
    page = client.get(route(pid) + '&limit=100').json()
    assert page['total'] == 211
    ids = [i['id'] for i in page['items']]
    while page['next_offset'] is not None:
        page = client.get(route(pid) + f"&limit=100&offset={page['next_offset']}&revision={page['revision']}").json()
        ids += [i['id'] for i in page['items']]
    assert len(ids) == len(set(ids)) == 211
    pipe.store.set_item_status(ids[0], 'dismissed')
    response = client.get(route(pid) + f"&offset=100&revision={page['revision']}")
    assert response.status_code == 409 and response.json()['code'] == 'research_sources_changed'
    assert client.get(route(pid) + '&limit=0').status_code == 400
    assert client.get(route(pid, 'company')).status_code == 400
    assert client.get(route('missing')).status_code == 404


def test_paper_patent_metadata_does_not_invent_attribution_or_dates(owner):
    _pipe, client, pid, _ = owner
    _, unit, ingest, _ = save(owner)
    for category in ('paper', 'patent'):
        source = trial(url=f'https://example.org/{category}')
        source.update(title=f'Synthetic {category}', published_at=None)
        source['extra'] = {'assignee_query': 'Synthetic Co', 'authors': ['A'], 'institutions': ['Independent University'],
                           'paper_search': {'query': 'Synthetic Co', 'mode': 'affiliation'}, 'secret': 'do not expose'}
        ingest(unit | {'id': category, 'category': category}, [source])
        result = client.get(route(pid, category)).json()
        assert result['total'] == 1
        item = result['items'][0]
        assert item['published_at'] is None
        assert item['extra']['institutions'] == ['Independent University']
        assert 'secret' not in json.dumps(item) and 'owner' not in item and 'applicants' not in item['extra']
    assert client.get(f'/api/studio/projects/{pid}').json()['research_source_counts'] == {'trial': 1, 'paper': 1, 'patent': 1}


def test_body_refresh_changes_revision_without_promoting_donor_to_own_text(owner):
    pipe, _, pid, _ = owner
    _, unit, ingest, donor = save(owner)
    source = trial(url='https://example.org/news')
    source['extra'] = {}
    ref = ingest(unit, [source])['evidence_refs'][0]
    first = research_sources(pipe.store, pid, 'trial')
    pipe.store.save_body(donor['item_id'], 'article_body', 'Synthetic donor article')
    pipe.store.save_body(ref['item_id'], 'twin_body', 'Synthetic donor article',
                         donor={'id': donor['item_id'], 'title': 'Synthetic donor', 'url': 'https://example.org/donor'})
    latest = research_sources(pipe.store, pid, 'trial')
    row = next(i for i in latest['items'] if i['id'] == ref['item_id'])
    assert row['basis'] == 'donor_text' and row['read_scope'] == 'summary'
    assert first['revision'] != latest['revision']


def test_one_sided_patent_versions_and_duplicate_publication_survive_pagination(owner):
    pipe, client, pid, _ = owner
    for n, number, versions in [(1, 'US123456A1', ['US123456B2']), (2, 'US123456B2', []), (3, 'US123456A1', [])]:
        source = trial(url=f'https://example.org/patent/{n}')
        source['extra'] = dict(patent_number=number, other_versions=versions, legal_status='unknown')
        pipe.store.add_items(pid, 'patent', [source])
    page = client.get(route(pid, 'patent') + '&limit=1').json()
    items = list(page['items'])
    while page['next_offset'] is not None:
        page = client.get(route(pid, 'patent') + f"&limit=1&offset={page['next_offset']}&revision={page['revision']}").json()
        items += page['items']
    assert page['total'] == 3 and page['family_count'] == 1
    assert len({i['id'] for i in items}) == 3
    assert all(i['family']['key'] == 'versions:US123456A1' and i['family']['basis'] == 'listed_versions'
               and i['family']['document_count'] == 3 for i in items)
    assert all(i['extra']['legal_status'] == 'unknown' for i in items)


def test_review_state_separates_confirmed_from_unreviewed_fallbacks():
    import json

    from project_watch.studio.research_sources import review_state

    bound = json.dumps({'triage_binding': {'version': 1, 'status': 'bound'}})
    assert review_state(False, bound) == 'pending'
    assert review_state(True, bound) == 'confirmed'
    assert review_state(True, '{}') == 'confirmed'  # triaged before bindings existed
    for status in ('quote_mismatch', 'title_mismatch', 'missing_binding', 'duplicate_id', 'no_model', 'model_rejected'):
        assert review_state(True, json.dumps({'triage_binding': {'status': status}})) == 'unreviewed'
    assert review_state(True, 'not json') == 'confirmed'
