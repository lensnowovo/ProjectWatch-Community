import json

from fastapi.testclient import TestClient
from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import FactInput, LocatedRef, ObjectInput


def populated(service, pid, visibility='public', text='Alpha product for pharma'):
    evidence = service.evidence.save(sample(pid, text).model_copy(update={
        'visibility': visibility, 'source_key': 'D:/private/company-bp.txt',
    }))
    ref = LocatedRef(evidence_version_id=evidence['id'], part_index=0, start=0, end=len(text), quote=text)
    obj = service.objects.save(pid, ObjectInput(kind='company', name='Alpha', visibility=visibility, refs=(ref,)), actor='user')
    fact = service.objects.save_fact(pid, FactInput(subject_id=obj['id'], predicate='business', text=text,
                                                 visibility=visibility, refs=(ref,)))
    return evidence, obj, fact


def test_package_has_located_refs_but_no_raw_body_paths_or_analysis(tmp_path):
    service, pid = make_service(tmp_path / 'package.sqlite3')
    evidence, obj, fact = populated(service, pid)
    result = service.material_package(pid, include_internal=False)
    assert result['objects'][0]['id'] == obj['id']
    assert result['facts'][0]['id'] == fact['id']
    assert result['facts'][0]['assertion_basis'].endswith('not_independent_verification')
    ref = result['facts'][0]['refs'][0]
    assert ref['evidence_version_id'] == evidence['id'] and ref['end'] == len(fact['text'])
    assert 'quote' not in ref
    raw = json.dumps(result)
    assert 'D:/private' not in raw and 'parts' not in result['evidence'][0]
    assert result['disclosure']['raw_files'] is False and result['disclosure']['analysis'] is False
    assert result['health']['business_silence'] == 'not_evaluated'
    assert result['snapshot_id'] == service.material_package(pid, include_internal=False)['snapshot_id']


def test_public_package_hides_internal_facts_objects_and_evidence(tmp_path):
    service, pid = make_service(tmp_path / 'private.sqlite3')
    _, _, fact = populated(service, pid, 'internal', 'PRIVATE-BUSINESS')
    public = service.material_package(pid, include_internal=False)
    assert not public['facts'] and not public['objects'] and not public['evidence']
    assert 'PRIVATE-BUSINESS' not in json.dumps(public)
    private = service.material_package(pid, include_internal=True)
    assert private['facts'][0]['id'] == fact['id']
    assert private['snapshot_id'] != public['snapshot_id']


def test_revision_keeps_old_ref_and_current_stale_status(tmp_path):
    service, pid = make_service(tmp_path / 'revised.sqlite3')
    old, _, _ = populated(service, pid)
    service.evidence.save(sample(pid, 'revised body').model_copy(update={'visibility': 'public', 'source_key': old['source_key']}))
    result = service.material_package(pid, include_internal=False)
    assert result['facts'][0]['dependency']['status'] == 'stale'
    assert result['facts'][0]['refs'][0]['evidence_version_id'] == old['id']
    assert next(r for r in result['evidence'] if r['id'] == old['id'])['current_head'] is False


def test_package_is_bounded_and_read_only(tmp_path):
    service, pid = make_service(tmp_path / 'bounded.sqlite3')
    for i in range(25):
        service.objects.save(pid, ObjectInput(kind='company', name=f'Company {i}', visibility='public'), actor='user')
    before = service.evidence.dependencies.changes(pid)['snapshot_through']
    result = service.material_package(pid, include_internal=False)
    assert len(result['objects']) == 20 and result['truncated']['objects'] is True
    assert result['continuations']['objects']
    assert service.evidence.dependencies.changes(pid)['snapshot_through'] == before
    assert not service.store.jobs(pid)
    assert len(json.dumps(result).encode()) < 512 * 1024


def test_package_same_connection_is_a_consistent_sqlite_snapshot(tmp_path, monkeypatch):
    service, pid = make_service(tmp_path / 'snapshot.sqlite3')
    populated(service, pid)
    # Material compilation must not stitch together live reads from other connections.
    original = service.store._connect
    opened = []
    def connect():
        db = original()
        opened.append(db)
        return db
    monkeypatch.setattr(service.store, '_connect', connect)
    service.material_package(pid, include_internal=False)
    assert len(opened) == 1


def test_scoped_package_api_filters_and_rejects_other_instance_project(tmp_path):
    from project_watch.studio.app import create_studio_app
    app = create_studio_app(tmp_path, start_worker=False)
    service = app.state.studio_application
    project = app.state.studio_pipeline.repository.create_project('Package test')
    service.store.upsert_project(project.id, {}, consent=False)
    populated(service, project.id, 'internal', 'private fixture')
    grant = service.issue_grant(project.id, 'read', 'package')
    client = TestClient(app)
    headers = {'authorization': 'Bearer ' + grant['token'], 'x-projectwatch-instance': service.instance_id}
    path = f'/api/studio/agent/projects/{project.id}/material-package'
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and not response.json()['facts']
    assert client.get(path, headers={**headers, 'x-projectwatch-instance': 'wrong'}).status_code == 403
    assert client.get('/api/studio/agent/projects/other/material-package', headers=headers).status_code == 403
    assert client.get(f'/api/studio/projects/{project.id}/material-package').json()['facts']


def test_package_relationship_projection_reuses_snapshot_and_exclusion_gates(tmp_path):
    from project_watch.studio.relationship_contracts import RelationshipCorrection
    service, pid = make_service(tmp_path / 'relations.sqlite3')
    _, subject, _ = populated(service, pid)
    peer = service.objects.save(pid, ObjectInput(kind='company', name='Beta', visibility='public'), actor='user')
    scope = {'subject_id': subject['id'], 'peer_id': peer['id'], 'subject_product_id': None, 'peer_product_id': None}
    service.relationships.save_candidate(pid, scope, relation_type='direct')
    assert service.material_package(pid)['relationships'][0]['support'] == 'pending'
    service.relationships.correct(service, pid, RelationshipCorrection(expected_version=service.project(pid)['config_version'],
        idempotency_key='exclude', changes=[{**scope, 'decision': 'exclude', 'reason': 'private reason'}]))
    package = service.material_package(pid)
    assert package['relationships'] == [] and package['overrides'][0]['decision'] == 'exclude'
    assert 'private reason' not in json.dumps(package)


def test_byte_bound_truncates_without_losing_disclosure_warning(tmp_path):
    service, pid = make_service(tmp_path / 'bytes.sqlite3')
    _, obj, fact = populated(service, pid)
    for i in range(110):
        service.objects.save_fact(pid, FactInput(subject_id=obj['id'], predicate=f'fact{i}', text='数据' * 1000,
            visibility='public', refs=(LocatedRef(**fact['refs'][0]),)))
    package = service.material_package(pid)
    assert len(json.dumps(package, ensure_ascii=False, separators=(',', ':')).encode()) <= 512 * 1024
    assert package['truncated']['facts'] is True
    assert package['continuations']['facts'] == 'object_facts'
