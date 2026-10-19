import pytest
from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import ObjectInput


def profile_inputs(tmp_path, visibility='public', scope='partial'):
    service, pid = make_service(tmp_path / 'business.sqlite3')
    subject = service.objects.ensure_subject(pid)
    text = 'Synthetic Optics builds Lens A for drug screening; buyers are pharma; delivery is software.'
    service.evidence.save(sample(pid, text).model_copy(update={
        'source_key': 'public-profile:company', 'visibility': visibility, 'read_scope': scope,
    }))
    from project_watch.studio.business_profile import BusinessProfiles
    profiles = BusinessProfiles(service.store, service.objects)
    inputs = profiles.inputs(pid, binding='fixture')
    proposal = {'products': [{'name': 'Lens A', 'company': 'Synthetic Optics', 'source': inputs['parts'][0]['marker'],
        'quote': text, 'use_case': 'drug screening', 'buyer_type': 'pharma', 'delivery': 'software', 'basis': 'actual', 'polarity': 'asserted'}]}
    return service, pid, subject, profiles, inputs, proposal


def test_public_only_business_profile_with_exact_product_facts(tmp_path):
    service, pid, subject, profiles, inputs, proposal = profile_inputs(tmp_path)
    result = profiles.apply(pid, inputs, proposal)
    assert result['accepted'] == 1 and result['status'] == 'extracted'
    product = next(o for o in service.objects.page(pid) if o['kind'] == 'product')
    assert product['parent_id'] == subject['id'] and product['visibility'] == 'public'
    facts = service.objects.facts(pid, product['id'])
    assert {f['predicate'] for f in facts} == {'use_case', 'buyer_type', 'delivery'}
    assert all(f['subject_attribution'] == 'explicit' for f in facts)
    assert service.objects.get(pid, subject['id'])['refs']
    before = {f['id'] for f in facts}
    profiles.apply(pid, profiles.inputs(pid, binding='fixture'), proposal)
    assert {f['id'] for f in service.objects.facts(pid, product['id'])} == before


@pytest.mark.parametrize('field,value', [('company', 'Other Optics'), ('name', 'Unmentioned Product'),
                                        ('quote', 'invented source quote')])
def test_invented_company_product_quote_or_dimension_cannot_become_fact(tmp_path, field, value):
    service, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    proposal['products'][0][field] = value
    result = profiles.apply(pid, inputs, proposal)
    assert result['accepted'] == 0 and result['rejected'] == 1
    assert not [o for o in service.objects.page(pid) if o['kind'] == 'product']


def test_summary_remains_summary_and_internal_conflict_is_not_overwritten(tmp_path):
    service, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path, scope='summary')
    result = profiles.apply(pid, inputs, proposal)
    assert result['summary_only'] is True
    product = next(o for o in service.objects.page(pid) if o['kind'] == 'product')
    fact = service.objects.facts(pid, product['id'])[0]
    assert service.evidence.get(pid, fact['refs'][0]['evidence_version_id'])['read_scope'] == 'summary'


def test_new_input_during_model_call_cannot_commit_late_products(tmp_path):
    service, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    service.evidence.save(sample(pid, 'new product text').model_copy(update={'source_key': 'public-profile:company', 'visibility': 'public'}))
    result = profiles.apply(pid, inputs, proposal)
    assert result['status'] == 'superseded'
    assert not [o for o in service.objects.page(pid) if o['kind'] == 'product']


def test_private_parse_only_source_is_never_in_model_inputs(tmp_path):
    service, pid = make_service(tmp_path / 'permissions.sqlite3')
    service.objects.ensure_subject(pid)
    from project_watch.repository import ProjectRepository
    source = ProjectRepository(service.store.db_path).add_source(pid, kind='document', reference='private.txt', content_sha256='a'*64)
    private = service.evidence.save(sample(pid, 'DO NOT SEND').model_copy(update={'internal_source_id': source.id}))
    from contextlib import closing
    with closing(service.store._connect()) as db, db:
        db.execute('INSERT INTO studio_source_permissions VALUES (?,?,?,?,?)', (source.id, pid, '', 'fixture', 'today'))
    from project_watch.studio.business_profile import BusinessProfiles
    profiles = BusinessProfiles(service.store, service.objects)
    assert profiles.inputs(pid, binding='new-provider')['parts'] == []
    assert private['visibility'] == 'internal'


def test_user_identity_is_not_renamed_or_declared_verified_by_extractor(tmp_path):
    service, pid, subject, profiles, _, proposal = profile_inputs(tmp_path)
    service.objects.save(pid, ObjectInput(kind='company', name='Human Company', purpose='subject', visibility='public'),
                         actor='user', object_id=subject['id'], expected_version=subject['version'])
    result = profiles.apply(pid, profiles.inputs(pid, binding='fixture'), proposal)
    assert result['accepted'] == 0
    assert service.objects.get(pid, subject['id'])['name'] == 'Human Company'


def test_generic_industry_and_negated_proposals_do_not_create_products(tmp_path):
    service, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    assert profiles.apply(pid, inputs, {'products': []})['status'] == 'insufficient_product_evidence'
    # Use a new input revision to avoid replay of the same completed negative input.
    service.evidence.save(sample(pid, inputs['parts'][0]['text'] + ' revised').model_copy(update={
        'source_key': 'public-profile:company', 'visibility': 'public'}))
    proposal['products'][0]['polarity'] = 'negated'
    assert profiles.apply(pid, profiles.inputs(pid, binding='fixture'), proposal)['accepted'] == 0
    assert not [o for o in service.objects.page(pid) if o['kind'] == 'product']


def test_conflicting_current_buyer_facts_coexist_without_last_write_winning(tmp_path):
    service, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    text = inputs['parts'][0]['text'] + ' Synthetic Optics Lens A buyers are hospitals.'
    service.evidence.save(sample(pid, text).model_copy(update={'source_key': 'public-profile:company', 'visibility': 'public'}))
    other = {**proposal['products'][0], 'quote': 'Synthetic Optics Lens A buyers are hospitals.',
             'buyer_type': 'hospitals', 'use_case': '', 'delivery': ''}
    proposal['products'].append(other)
    profiles.apply(pid, profiles.inputs(pid, binding='fixture'), proposal)
    product = next(o for o in service.objects.page(pid) if o['kind'] == 'product')
    buyers = [f for f in service.objects.facts(pid, product['id']) if f['predicate'] == 'buyer_type']
    assert {f['value'] for f in buyers} == {'pharma', 'hospitals'}
    assert all(f['dependency']['status'] == 'current' for f in buyers)


def test_forecast_remains_forecast_not_existing_product_proof(tmp_path):
    service, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    proposal['products'][0]['basis'] = 'forecast'
    profiles.apply(pid, inputs, proposal)
    product = next(o for o in service.objects.page(pid) if o['kind'] == 'product')
    assert all(f['basis'] == 'forecast' for f in service.objects.facts(pid, product['id']))


def test_completed_profile_is_not_invalidated_by_unrelated_financing_news(tmp_path):
    service, pid, _, profiles, inputs, proposal = profile_inputs(tmp_path)
    service.evidence.save(sample(pid, 'Synthetic Optics announced funding').model_copy(update={
        'source_key': 'unrelated-funding-news', 'visibility': 'public'}))
    assert profiles.inputs(pid, binding='fixture')['input_version'] == inputs['input_version']
    assert profiles.apply(pid, inputs, proposal)['status'] == 'extracted'


@pytest.mark.parametrize('malformed_row', [False, True])
def test_automatic_business_job_replays_once_and_stops_for_pause_or_same_failed_input(tmp_path, malformed_row):
    import json

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.http import Http, Response
    app = create_studio_app(tmp_path, start_worker=False)
    pipeline, store = app.state.studio_pipeline, app.state.studio_store
    project = pipeline.repository.create_project('Synthetic Optics')
    pid = project.id
    store.upsert_project(pid, {}, consent=True)
    pipeline.settings_store.update({'model': {'protocol': 'openai', 'base_url': 'https://synthetic.example/v1',
        'model': 'fixture', 'api_key': 'synthetic-key'}})
    pipeline.objects.ensure_subject(pid)
    text = 'Synthetic Optics builds Lens A for pharma; delivery is software.'
    pipeline.evidence.save(sample(pid, text).model_copy(update={'visibility': 'public', 'source_key': 'public-profile:source'}))
    proposal = {'products': [{'company': 'Synthetic Optics', 'name': 'Lens A', 'source': 'B1', 'quote': text,
                            'use_case': '', 'buyer_type': 'pharma', 'delivery': 'software', 'basis': 'actual', 'polarity': 'asserted'}]}
    if malformed_row:
        proposal['products'].insert(0, {**proposal['products'][0], 'use_case': 'x' * 151})
    calls = []
    def transport(*args):
        calls.append(args[0])
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(proposal)}}]}).encode())
    pipeline.http_factory = lambda _: Http(transport)
    queued = pipeline.schedule_business(pid)
    assert queued['kind'] == 'business' and pipeline.schedule_business(pid)['id'] == queued['id']
    claimed = store.claim()
    pipeline.run(claimed)
    pipeline.run(claimed)
    store.finish(claimed['id'], lease_token=claimed['lease_token'])
    assert calls == ['POST'] and pipeline.schedule_business(pid) is None
    assert pipeline.business_profiles.get(pid)['rejected_schema'] == int(malformed_row)
    assert len([o for o in pipeline.objects.page(pid) if o['kind'] == 'product']) == 1
    store.set_status(pid, 'paused')
    assert pipeline.schedule_business(pid) is None
    store.set_status(pid, 'active')
    bad = store.enqueue(pid, 'business', input_version='same-failed-input')
    store.finish(bad['id'], error='invalid schema')
    assert store.enqueue(pid, 'business', automatic=True, input_version='same-failed-input')['id'] == bad['id']
    assert store.jobs(pid)[0]['retry_after'] == '9999-12-31'
