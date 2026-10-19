"""Original business passages must not be crowded out by newer search snippets."""
import pytest

from project_watch.repository import ProjectRepository
from project_watch.studio.business_profile import BusinessProfiles
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart
from project_watch.studio.objects import ObjectStore
from project_watch.studio.store import StudioStore


@pytest.mark.parametrize('peer_mode', [False, True])
def test_new_summaries_cannot_displace_existing_current_company_original(tmp_path, peer_mode):
    path = tmp_path / 'synthetic.sqlite3'
    project = ProjectRepository(path).create_project('Synthetic Vector Ltd')
    store = StudioStore(path)
    store.upsert_project(project.id, {'legal_name': project.name}, consent=False)
    objects = ObjectStore(store, store.evidence)
    subject = objects.ensure_subject(project.id)
    business = BusinessProfiles(store, objects)
    store.add_items(project.id, 'company', [{'key': 'original', 'title': 'Synthetic product release',
        'url': 'https://example.org/original', 'source': 'synthetic', 'snippet': '', 'published_at': None}])
    item = store.item_by_key(project.id, 'original')
    store.save_body(item['id'], 'article_body', 'Synthetic Vector Ltd supplies Vector One to laboratories.')
    with store._connect() as db:
        original_id = db.execute('SELECT evidence_version_id FROM studio_item_evidence WHERE item_id=?', (item['id'],)).fetchone()[0]
    selected = [original_id]
    for n in range(12):
        summary = store.evidence.save(EvidenceInput(project_id=project.id, source_key=f'public-profile:synthetic-{n}',
            extraction_version='synthetic-v1', source_type='search_summary', visibility='public', read_scope='summary',
            parts=[EvidencePart(locator_kind='stored_text', locator='synthetic excerpt', text=f'Synthetic Vector Ltd search snippet {n}.')]))
        selected.append(summary['id'])
    options = {'subject_id': subject['id'], 'evidence_ids': selected[:12]} if peer_mode else {}
    inputs = business.inputs(project.id, binding='synthetic-model', **options)
    assert inputs['parts'][0]['read_scope'] != 'summary'
    assert 'Vector One' in inputs['parts'][0]['text']
    assert len(inputs['parts']) <= 20
    assert len({p['evidence_version_id'] for p in inputs['parts']}) == (12 if peer_mode else 13)
    with store._connect() as db:
        assert db.execute('SELECT count(*) FROM studio_evidence_versions').fetchone()[0] == 13
