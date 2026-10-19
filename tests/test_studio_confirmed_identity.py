"""Explicit company names are not legacy brand/product search aliases."""
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_studio_evidence_versions import sample

from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.llm import ModelError
from project_watch.studio.monitoring_contracts import Alias, ObjectInput

FULL, SHORT = '合成视界科技有限公司', '合成视界'
TEXT = SHORT + '生产 Lens A，用于 drug screening；买方为 pharma；交付为 software。'


def create(client, *, names=None, entity=None):
    body = {'entity': entity or {'legal_name': FULL, 'short_name': SHORT,
                                'aliases': ['Lens A', 'Synthetic Brand'], 'entity_source': 'ai_search'},
            'consent': True}
    if names is not None:
        body['confirmed_company_names'] = names
    response = client.post('/api/studio/projects', json=body)
    assert response.status_code == 201, response.text
    return response.json()['id']


def extract(pipeline, pid):
    pipeline.evidence.save(sample(pid, TEXT).model_copy(update={
        'source_key': 'public-profile:synthetic-confirmed', 'visibility': 'public'}))
    inputs = pipeline.business_profiles.inputs(pid, binding='synthetic-local')
    result = pipeline.business_profiles.apply(pid, inputs, {'products': [{
        'company': FULL, 'name': 'Lens A', 'source': 'B1', 'quote': TEXT,
        'use_case': 'drug screening', 'buyer_type': 'pharma', 'delivery': 'software',
        'basis': 'actual', 'polarity': 'asserted'}]})
    return inputs, result


def test_confirmed_abbreviation_reaches_product_ownership(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    with TestClient(app) as client:
        pid = create(client, names=[SHORT, SHORT, FULL])
        subject = pipeline.objects.ensure_subject(pid)
        assert subject['user_override'] and subject['identity_basis'] == 'user_specified'
        assert [a['name'] for a in subject['aliases']] == [SHORT]
        inputs, result = extract(pipeline, pid)
        assert set(inputs['parts'][0]['company_names']) == {FULL, SHORT}
        assert result['accepted'] == 1
        assert len(pipeline.objects.facts(pid, result['product_ids'][0])) == 3


def test_search_only_names_and_legacy_activation_are_not_ownership(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    with TestClient(app) as client:
        pid = create(client)
        assert pipeline.objects.ensure_subject(pid)['aliases'] == []
        client.post('/api/studio/projects/' + pid + '/activate')
        _, result = extract(pipeline, pid)
        assert result['accepted'] == 0


@pytest.mark.parametrize('names', [[''], ['   '], ['公司'], ['the company'], ['x' * 201], [None],
                                   [str(i) for i in range(13)], 'not a list'])
def test_malformed_company_intent_is_rejected_before_any_write(tmp_path, names):
    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        response = client.post('/api/studio/projects', json={
            'entity': {'legal_name': FULL}, 'consent': True, 'confirmed_company_names': names})
        assert response.status_code == 422
        assert client.get('/api/studio/projects').json() == []


@pytest.mark.parametrize('name', ['   ', '公司'])
def test_invalid_canonical_name_is_rejected_before_write(tmp_path, name):
    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        assert client.post('/api/studio/projects', json={
            'entity': {'legal_name': name}, 'consent': True}).status_code == 422
        assert client.get('/api/studio/projects').json() == []


def test_alias_removal_preserves_id_and_history_but_stales_dependent_facts(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    with TestClient(app) as client:
        pid = create(client, names=[SHORT])
        subject = pipeline.objects.ensure_subject(pid)
        before, result = extract(pipeline, pid)
        response = client.patch('/api/studio/projects/' + pid, json={
            'entity': {'legal_name': FULL, 'short_name': SHORT}, 'confirmed_company_names': []})
        assert response.status_code == 200, response.text
        current = pipeline.objects.ensure_subject(pid)
        assert current['id'] == subject['id'] and current['version'] > subject['version']
        assert current['aliases'] == []
        assert pipeline.objects.history(pid, subject['id'])[0]['aliases'][0]['name'] == SHORT
        assert pipeline.business_profiles.inputs(pid, binding='synthetic-local')['input_version'] != before['input_version']
        assert all(f['dependency']['status'] == 'stale' for f in pipeline.objects.facts(pid, result['product_ids'][0]))
        assert pipeline.business_profiles.apply(pid, before, {'products': []})['status'] == 'superseded'


def test_idempotent_confirmation_preserves_private_fields_and_refs(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    with TestClient(app) as client:
        pid = create(client, names=[SHORT])
        subject = pipeline.objects.ensure_subject(pid)
        item = ObjectInput.model_validate({k: subject[k] for k in ObjectInput.model_fields}).model_copy(update={
            'website': 'https://synthetic.example', 'jurisdiction': 'Synthetic jurisdiction',
            'aliases': (Alias(name='Private Name', visibility='internal'), Alias(name=SHORT, visibility='public'))})
        subject = pipeline.objects.save(pid, item, actor='user', object_id=subject['id'], expected_version=subject['version'])
        body = {'entity': {'legal_name': FULL}, 'confirmed_company_names': [SHORT]}
        assert client.patch('/api/studio/projects/' + pid, json=body).status_code == 200
        current = pipeline.objects.ensure_subject(pid)
        assert current == subject
        assert client.patch('/api/studio/projects/' + pid, json=body).status_code == 200
        assert pipeline.objects.ensure_subject(pid)['version'] == subject['version']


def test_failed_object_write_rolls_back_profile_correction(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    with TestClient(app) as client:
        pid = create(client, names=[SHORT])
        before = pipeline.store.get_project(pid)
        subject = pipeline.objects.ensure_subject(pid)
        def fail(*args, **kwargs):
            raise ValueError('synthetic object failure')
        monkeypatch.setattr(pipeline.objects, 'save', fail)
        response = client.patch('/api/studio/projects/' + pid, json={
            'entity': {'legal_name': 'Corrected Synthetic Ltd'}, 'confirmed_company_names': ['Corrected']})
        assert response.status_code == 422
        assert pipeline.store.get_project(pid) == before
        assert pipeline.objects.ensure_subject(pid) == subject


def test_missing_entity_cannot_apply_name_intent_or_modify_note(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        pid = create(client, names=[SHORT])
        response = client.patch('/api/studio/projects/' + pid, json={
            'confirmed_company_names': [], 'note': 'must not apply'})
        assert response.status_code == 422
        assert not app.state.studio_store.get_project(pid).get('note')


def test_automatic_store_updates_do_not_promote_or_erase_names(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    with TestClient(app) as client:
        pid = create(client, names=[SHORT])
        subject = pipeline.objects.ensure_subject(pid)
        pipeline.store.upsert_project(pid, {**pipeline.store.get_project(pid),
            'aliases': ['Automatic Brand'], 'industry': 'synthetic industry'}, consent=False)
        assert pipeline.objects.ensure_subject(pid) == subject
        other = create(client, entity={'legal_name': 'Other Synthetic Ltd'})
        assert pipeline.objects.ensure_subject(other)['aliases'] == []


def test_canonical_only_edit_does_not_promote_automatic_aliases(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    project = pipeline.repository.create_project(FULL)
    pipeline.store.upsert_project(project.id, {'legal_name': FULL}, consent=True)
    old = pipeline.objects.ensure_subject(project.id)
    item = ObjectInput(kind='company', name=FULL, purpose='subject', visibility='public',
                       aliases=(Alias(name=SHORT, visibility='public'),))
    pipeline.objects.save(project.id, item, actor='automatic', object_id=old['id'], expected_version=old['version'])
    with TestClient(app) as client:
        assert client.patch('/api/studio/projects/' + project.id, json={
            'entity': {'legal_name': FULL, 'short_name': SHORT}}).status_code == 200
    assert pipeline.objects.ensure_subject(project.id)['aliases'] == []
    assert extract(pipeline, project.id)[1]['accepted'] == 0


def test_same_spelling_private_and_public_alias_remain_separate(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    with TestClient(app) as client:
        pid = create(client, names=[SHORT])
        old = pipeline.objects.ensure_subject(pid)
        item = ObjectInput(kind='company', name=FULL, purpose='subject', visibility='public', aliases=(
            Alias(name=SHORT, visibility='internal'), Alias(name=SHORT, visibility='public')))
        old = pipeline.objects.save(pid, item, actor='user', object_id=old['id'], expected_version=old['version'])
        assert client.patch('/api/studio/projects/' + pid, json={
            'entity': {'legal_name': FULL}, 'confirmed_company_names': [SHORT]}).status_code == 200
        assert pipeline.objects.ensure_subject(pid) == old
        assert client.patch('/api/studio/projects/' + pid, json={
            'entity': {'legal_name': FULL}, 'confirmed_company_names': []}).status_code == 200
        assert pipeline.objects.ensure_subject(pid)['aliases'] == [Alias(name=SHORT, visibility='internal').model_dump(mode='json')]


def test_withdrawal_after_http_confirmation_still_invalidates_products(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    with TestClient(app) as client:
        pid = create(client, names=[SHORT])
        inputs, result = extract(pipeline, pid)
        pipeline.evidence.dependencies.withdraw(pid, inputs['parts'][0]['evidence_version_id'],
            'synthetic source correction', actor='user')
        assert all(f['dependency']['status'] == 'stale' for f in pipeline.objects.facts(pid, result['product_ids'][0]))


def test_http_name_correction_fences_saved_checkpoint_before_transport(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline, store = app.state.studio_pipeline, app.state.studio_store
    with TestClient(app) as client:
        pid = create(client, names=[SHORT])
        # Finish the synthetic onboarding queue without executing it.
        pending = store.claim()
        store.finish(pending['id'], lease_token=pending['lease_token'])
        pipeline.settings_store.update({'model': {'protocol': 'openai',
            'base_url': 'https://synthetic.example/v1', 'model': 'fixture', 'api_key': 'synthetic-key'}})
        pipeline.evidence.save(sample(pid, TEXT).model_copy(update={
            'source_key': 'public-profile:synthetic-confirmed', 'visibility': 'public'}))
        inputs = pipeline.business_profiles.inputs(pid, binding=pipeline.model_binding())
        store.enqueue(pid, 'business', input_version=inputs['input_version'])
        job = store.claim()
        execution = JobExecution(store, job)
        execution.checkpoint('business_inputs', inputs)
        assert client.patch('/api/studio/projects/' + pid, json={
            'entity': {'legal_name': FULL}, 'confirmed_company_names': []}).status_code == 200
        calls = []
        def transport(*args):
            calls.append(args[0])
            return Response(200, {}, b'{"choices":[{"message":{"content":"{\\"products\\":[]}"}}]}')
        pipeline.http_factory = lambda _: Http(transport)
        pipeline._execution_state.current = execution
        try:
            with pytest.raises(ModelError, match='旧检查点未重发'):
                pipeline.run_business(job)
        finally:
            pipeline._execution_state.current = None
        assert calls == []


def test_failed_create_is_atomic_including_repository_row(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    def fail(*args, **kwargs):
        raise ValueError('synthetic object failure')
    monkeypatch.setattr(app.state.studio_pipeline.objects, 'save', fail)
    with TestClient(app) as client:
        response = client.post('/api/studio/projects', json={
            'entity': {'legal_name': FULL}, 'confirmed_company_names': [SHORT], 'consent': True})
        assert response.status_code == 422
        with closing(app.state.studio_store._connect()) as db:
            assert db.execute('SELECT count(*) FROM projects').fetchone()[0] == 0
