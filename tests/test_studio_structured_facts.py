import json

from test_studio_competition_research import research_pipeline
from test_studio_evidence_versions import sample


def prepare(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    text = 'Synthetic Optics works with Beta, a pilot customer, in a trial. Synthetic Rule is draft for Beijing medical devices.'
    evidence = pipeline.evidence.save(sample(pid, text).model_copy(update={'source_key': 'public-profile:roles', 'visibility': 'public'}))
    inputs = pipeline.structured_facts.inputs(pid, pipeline.model_binding())
    proposal = {'relations': [{'source': 'B1', 'quote': text, 'peer': 'Beta', 'role': 'customer', 'stage': 'trial',
        'role_text': 'pilot customer', 'stage_text': 'trial', 'basis': 'actual'}],
        'policies': [{'source': 'B1', 'quote': text, 'name': 'Synthetic Rule', 'status': 'draft',
            'jurisdictions': ['Beijing'], 'applicable_to': ['medical devices']}]}
    return pipeline, pid, evidence, inputs, proposal


def test_located_role_policy_extraction_is_immutable_and_idempotent(tmp_path):
    from project_watch.studio.observations import read
    pipeline, pid, _, inputs, proposal = prepare(tmp_path)
    result = pipeline.structured_facts.apply(pid, inputs, proposal)
    assert len(result['fact_ids']) == 2
    assert pipeline.structured_facts.apply(pid, inputs, proposal) == result
    view = read(pipeline, pid)
    assert len(view['items']) == 2
    role = next(r for r in view['items'] if r['kind'] == 'business_relation')
    policy = next(r for r in view['items'] if r['kind'] == 'policy')
    assert role['role'] == 'pilot_partner' and not role['implies_revenue']
    assert policy['applicability'] == 'pending'
    assert pipeline.schedule_details(pid) is None


def test_unlocated_peer_and_date_cannot_create_facts(tmp_path):
    pipeline, pid, _, inputs, proposal = prepare(tmp_path)
    proposal['relations'][0]['peer'] = 'Memory Co'
    proposal['policies'][0]['effective_from'] = '2026-01-01'
    result = pipeline.structured_facts.apply(pid, inputs, proposal)
    assert result['fact_ids'] == [] and result['rejected'] == 2


def test_changed_source_rejects_late_structured_output(tmp_path):
    pipeline, pid, evidence, inputs, proposal = prepare(tmp_path)
    pipeline.evidence.dependencies.withdraw(pid, evidence['id'], 'synthetic correction', actor='fixture')
    assert pipeline.structured_facts.apply(pid, inputs, proposal)['status'] == 'superseded'


def test_structured_job_uses_one_fake_model_call_and_no_search(tmp_path):
    from project_watch.studio.http import Http, Response
    pipeline, pid, _, _, proposal = prepare(tmp_path)
    calls = []
    def transport(method, url, headers, body, timeout):
        calls.append(url)
        assert 'tavily' not in url
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(proposal)}}]}).encode())
    pipeline.http_factory = lambda _: Http(transport)
    queued = pipeline.schedule_details(pid)
    assert queued['kind'] == 'details'
    claimed = pipeline.store.claim()
    pipeline.run(claimed)
    pipeline.run(claimed)
    pipeline.store.finish(claimed['id'], lease_token=claimed['lease_token'])
    assert len(calls) == 1 and pipeline.schedule_details(pid) is None
    assert len(pipeline.structured_facts.get(pid)['fact_ids']) == 2


def test_peer_becoming_private_hides_public_relation_in_all_read_paths(tmp_path):
    from project_watch.studio.monitoring_contracts import ObjectInput
    from project_watch.studio.observations import read
    pipeline, pid, _, inputs, proposal = prepare(tmp_path)
    pipeline.structured_facts.apply(pid, inputs, proposal)
    role = next(r for r in read(pipeline, pid)['items'] if r['kind'] == 'business_relation')
    peer = pipeline.objects.get(pid, role['peer_id'])
    item = ObjectInput.model_validate({k: peer[k] for k in ObjectInput.model_fields}).model_copy(update={'visibility': 'internal'})
    pipeline.objects.save(pid, item, actor='user', object_id=peer['id'], expected_version=peer['version'])
    assert not any(r['kind'] == 'business_relation' for r in read(pipeline, pid)['items'])
    assert not any((r.get('attributes') or {}).get('kind') == 'business_relation'
                   for r in pipeline.objects.facts(pid, inputs['subject_id'], include_internal=False))


def test_owner_and_agent_read_same_public_observations_without_network(tmp_path):
    from fastapi.testclient import TestClient
    from test_studio_application_parity import headers

    from project_watch.studio.app import create_studio_app
    pipeline, pid, _, inputs, proposal = prepare(tmp_path)
    pipeline.structured_facts.apply(pid, inputs, proposal)
    app = create_studio_app(tmp_path, start_worker=False)
    web = TestClient(app)
    grant = web.post(f'/api/studio/projects/{pid}/agent-grants', json={'permission': 'read', 'label': 'fixture'}).json()
    path = f'/api/studio/agent/projects/{pid}/observations'
    owner = web.get(f'/api/studio/projects/{pid}/observations').json()
    response = web.get(path, headers=headers(grant))
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['items'] == owner['items']
    assert web.get(path).status_code == 401


def test_event_extraction_has_dated_stage_and_read_only_scoped_timeline(tmp_path):
    from fastapi.testclient import TestClient
    from test_studio_application_parity import headers

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.observations import read
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    text = 'Synthetic Optics Series A received on 2026-08-01, disclosed 2026-08-02.'
    pipeline.evidence.save(sample(pid, text).model_copy(update={'source_key': 'public-profile:event', 'visibility': 'public'}))
    inputs = pipeline.structured_facts.inputs(pid, pipeline.model_binding())
    result = pipeline.structured_facts.apply(pid, inputs, {'events': [{'source': 'B1', 'quote': text,
        'name': 'Series A', 'channel': 'funding', 'role': 'recipient', 'state': 'received', 'state_text': 'received',
        'basis': 'actual', 'occurred_at': '2026-08-01', 'disclosed_at': '2026-08-02'}]})
    assert len(result['fact_ids']) == 1
    assert read(pipeline, pid)['items'][0]['support'] == 'source_assertion'
    web = TestClient(create_studio_app(tmp_path, start_worker=False))
    grant = web.post(f'/api/studio/projects/{pid}/agent-grants', json={'permission': 'read', 'label': 'fixture'}).json()
    path = f'/api/studio/agent/projects/{pid}/event-states'
    response = web.get(path, headers=headers(grant))
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['items'][0]['state'] == 'received'
    assert response.json()['items'] == web.get(f'/api/studio/projects/{pid}/event-states').json()['items']
    assert web.get(path).status_code == 401


def test_milestone_and_money_extraction_share_located_evidence(tmp_path):
    from project_watch.studio.milestones import read as milestones
    from project_watch.studio.observations import read
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    text = 'Synthetic Optics Line One original target 2026-12-01 stated 2026-08-01. Synthetic Optics received 100 万元 CNY 2026Q3 公司单体 for Line One on 2026-08-01.'
    pipeline.evidence.save(sample(pid, text).model_copy(update={'source_key': 'public-profile:money', 'visibility': 'public'}))
    inputs = pipeline.structured_facts.inputs(pid, pipeline.model_binding())
    proposal = {'milestones': [{'source': 'B1', 'quote': text, 'name': 'Line One', 'phase': 'original',
        'phase_text': 'original', 'target_date': '2026-12-01', 'occurred_at': '2026-08-01', 'basis': 'forecast'}],
        'funds': [{'source': 'B1', 'quote': text, 'measure': 'funding', 'stage': 'received', 'stage_text': 'received',
        'milestone_name': 'Line One', 'value': '100', 'unit': '万元', 'currency': 'CNY', 'period': '2026Q3',
        'consolidation_scope': '公司单体', 'occurred_at': '2026-08-01', 'basis': 'actual'}]}
    result = pipeline.structured_facts.apply(pid, inputs, proposal)
    assert len(result['fact_ids']) == 2
    assert pipeline.structured_facts.apply(pid, inputs, proposal) == result
    assert milestones(pipeline, pid)['items'][0]['original_dates'] == ['2026-12-01']
    fund = next(r for r in read(pipeline, pid)['items'] if r['kind'] == 'funding_fact')
    assert fund['is_funding_received'] and fund['normalized_value'] == '100.0'
    assert fund['milestone_name'] == 'Line One' and fund['cash_gap'] is None


def test_invented_money_and_milestone_dates_are_rejected(tmp_path):
    pipeline, pid, _, inputs, _ = prepare(tmp_path)
    quote = 'Synthetic Optics works with Beta, a pilot customer, in a trial. Synthetic Rule is draft for Beijing medical devices.'
    result = pipeline.structured_facts.apply(pid, inputs, {
        'milestones': [{'source': 'B1', 'quote': quote, 'name': 'Line One', 'phase': 'original',
            'phase_text': 'original', 'target_date': '2026-12-01'}],
        'funds': [{'source': 'B1', 'quote': quote, 'measure': 'funding', 'stage': 'received', 'stage_text': 'received',
            'value': '100', 'unit': '万元', 'currency': 'CNY', 'period': '2026Q3', 'basis': 'actual'}]})
    assert result['fact_ids'] == [] and result['rejected'] == 2


def test_sources_naming_the_company_by_its_short_name_are_accepted(tmp_path):
    """希格生科(深圳)有限公司 is written “希格生科” in reports; the full name must not be required in every quote."""
    pipeline, pid, _, _, proposal = prepare(tmp_path)
    pipeline.store.upsert_project(pid, {'short_name': 'SynOptics'}, consent=True)
    text = 'SynOptics works with Beta, a pilot customer, in a trial.'
    pipeline.evidence.save(sample(pid, text).model_copy(update={'source_key': 'public-profile:short', 'visibility': 'public'}))
    inputs = pipeline.structured_facts.inputs(pid, pipeline.model_binding())
    assert 'SynOptics' in inputs['names'] and inputs['company'] != 'SynOptics'
    marker = next(p['marker'] for p in inputs['parts'] if p['text'] == text)
    relation = {**proposal['relations'][0], 'source': marker, 'quote': text}
    assert len(pipeline.structured_facts.apply(pid, inputs, {'relations': [relation]})['fact_ids']) == 1


def test_a_quote_naming_neither_form_still_creates_no_fact(tmp_path):
    pipeline, pid, _, inputs, proposal = prepare(tmp_path)
    relation = {**proposal['relations'][0], 'quote': 'Synthetic Rule is draft for Beijing medical devices.'}
    assert pipeline.structured_facts.apply(pid, inputs, {'relations': [relation]})['fact_ids'] == []
