"""Owner source choices preserve audit versions without launching research."""
import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_collection_scope import build

from project_watch.studio import collection_targets
from project_watch.studio.app import create_studio_app


@pytest.fixture
def owner(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipe = app.state.studio_pipeline
    project = pipe.repository.create_project('Synthetic Owner Company')
    pipe.store.upsert_project(project.id, {'legal_name': 'Synthetic Owner Company'}, consent=True)
    client = TestClient(app)
    return pipe, client, project.id, f'/api/studio/projects/{project.id}/collection-sources'


def payload(**changes):
    return dict(selection='not_applicable', reason='Owner confirmed nonclinical scope', expected_version=0) | changes


def test_explicit_decisions_are_versioned_idempotent_and_do_not_enqueue_or_change_configuration(owner):
    pipe, client, pid, url = owner
    project_before = pipe.store.get_project(pid)
    settings_before = pipe.settings_store.path.read_bytes() if pipe.settings_store.path.exists() else None
    assert client.get(url).json()['decisions'] == {}
    first = client.put(url + '/clinical_sponsor', json=payload())
    assert first.status_code == 200
    record = first.json()
    assert record['basis'] == 'human_confirmation' and record['actor'] == 'human' and record['version'] == 1
    assert client.put(url + '/clinical_sponsor', json=payload()).json() == record
    assert client.put(url + '/clinical_sponsor', json=payload(selection='enabled')).status_code == 409
    second = client.put(url + '/clinical_sponsor', json=payload(selection='enabled', expected_version=1)).json()
    assert second['version'] == 2 and second['id'] != record['id']
    assert client.put(url + '/clinical_sponsor', json=payload()).status_code == 409
    with closing(pipe.store._connect()) as db:
        history = [json.loads(r[0]) for r in db.execute(
            'SELECT record FROM studio_collection_source_decisions ORDER BY version')]
        assert history == [record, second]
        assert db.execute('SELECT COUNT(*) FROM studio_jobs').fetchone()[0] == 0
    assert pipe.store.get_project(pid) == project_before
    assert (pipe.settings_store.path.read_bytes() if pipe.settings_store.path.exists() else None) == settings_before


@pytest.mark.parametrize('changes', [dict(reason='  '), dict(selection='available'),
                                    dict(expected_version=True), dict(expected_version=-1)])
def test_bad_input_does_not_store_decision(owner, changes):
    _, client, _, url = owner
    assert client.put(url + '/clinical_term', json=payload(**changes)).status_code == 422
    assert client.get(url).json()['decisions'] == {}


def test_project_and_owner_authority_cannot_be_bypassed(owner):
    pipe, client, _, url = owner
    assert client.put(url + '/unknown', json=payload()).status_code == 422
    assert client.put('/api/studio/projects/missing/collection-sources/search', json=payload()).status_code == 404
    assert client.put(url + '/search', headers={'Authorization': 'Bearer synthetic-agent'}, json=payload()).status_code == 403
    other = pipe.repository.create_project('Other Synthetic')
    pipe.store.upsert_project(other.id, {'legal_name': 'Other Synthetic'}, consent=True)
    assert client.put(url + '/search', json=payload()).status_code == 200
    assert collection_targets.source_decisions(pipe.store, other.id) == {}


@pytest.mark.parametrize('selection', ['disabled', 'not_applicable'])
def test_owner_decision_overrides_registered_professional_query_and_default(selection):
    target = dict(adapter='openalex_affiliation', id='synthetic-scope', query='Other Institute',
                  capability='available', reason='Public query candidate', historical=True,
                  ref={'evidence_version_id': 'public-ref'})
    p = build(source_queries=[target], source_decisions={'openalex_affiliation': dict(
        capability=selection, reason='Explicit owner choice', evidence_ids=['source-decision:synthetic'])})
    rows = [u for u in p['units'] if u['source'] == 'openalex_affiliation']
    assert len(rows) == 2  # Concrete extra query and original company query both retained.
    assert all(u['capability'] == selection and not u['required'] for u in rows)
    assert all(u['applicability_evidence_ids'] == ['source-decision:synthetic'] for u in rows)




