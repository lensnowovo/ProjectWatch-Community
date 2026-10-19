"""Synthetic registry fixtures; not a live CT.gov coverage or accuracy benchmark."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from test_studio_evidence_versions import sample

TARGET_TEXT = ('Synthetic Cell 的主要产品 Cellex 是一款靶向CD19的自体CAR-T细胞免疫治疗产品，'
               '用于治疗复发难治大B细胞淋巴瘤。')
TARGET_USE = '靶向CD19的自体CAR-T细胞免疫治疗产品'


def study(nct, sponsor, cls, interventions, arms, conditions=('Large B-cell Lymphoma',),
          phases=('PHASE2',), status='RECRUITING', title=None):
    return {'protocolSection': {
        'identificationModule': {'nctId': nct, 'briefTitle': title or f'Study of {interventions[0]["name"]}'},
        'sponsorCollaboratorsModule': {'leadSponsor': {'name': sponsor, 'class': cls}},
        'armsInterventionsModule': {'armGroups': arms, 'interventions': interventions},
        'conditionsModule': {'conditions': list(conditions)},
        'designModule': {'phases': list(phases)},
        'statusModule': {'overallStatus': status, 'lastUpdatePostDateStruct': {'date': '2026-09-01'}},
    }}


def experimental(*names, label='Arm A'):
    return [{'label': label, 'type': 'EXPERIMENTAL', 'interventionNames': list(names)}]


STUDIES = [
    study('NCT90000001', 'Alpha Bio Co., Ltd.', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'AB-19 CAR-T cells', 'otherNames': ['alphacel'],
         'description': 'Autologous anti-CD19 CAR-T cells', 'armGroupLabels': ['Arm A']},
        {'type': 'DRUG', 'name': 'Fludarabine', 'description': 'Lymphodepletion', 'armGroupLabels': ['Arm A']}],
        experimental('Biological: AB-19 CAR-T cells', 'Drug: Fludarabine')),
    study('NCT90000002', 'Children Research Hospital', 'OTHER', [
        {'type': 'BIOLOGICAL', 'name': 'CD19 CAR-T', 'description': 'CD19 CAR-T cells', 'armGroupLabels': ['Arm A']}],
        experimental('Biological: CD19 CAR-T')),
    study('NCT90000003', 'Gamma Therapeutics', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'GT-01', 'description': 'Allogeneic CAR-T targeting CD19',
         'armGroupLabels': ['GT-01 arm']},
        {'type': 'BIOLOGICAL', 'name': 'Rituximab', 'description': 'anti-CD20 antibody', 'armGroupLabels': ['Control']}],
        [{'label': 'GT-01 arm', 'type': 'EXPERIMENTAL'}, {'label': 'Control', 'type': 'ACTIVE_COMPARATOR'}],
        conditions=('B-cell Acute Lymphoblastic Leukemia',), phases=('PHASE1',)),
    study('NCT90000004', 'Delta Cell', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'DC-BCMA', 'description': 'Autologous BCMA CAR-T', 'armGroupLabels': ['Arm A']}],
        experimental('Biological: DC-BCMA'), conditions=('Multiple Myeloma',), phases=('PHASE3',), status='COMPLETED'),
    study('NCT90000005', 'Synthetic Cell Therapeutics Inc', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'SC-19', 'description': 'Autologous CD19 CAR-T', 'armGroupLabels': ['Arm A']}],
        experimental('Biological: SC-19')),
]


def registry_pipeline(tmp_path, *, studies=STUDIES, target_text=TARGET_TEXT, target_use=TARGET_USE, registry_status=200,
                      product='Cellex', reject_filter=False, web_peers=()):
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.http import Http, Response

    app = create_studio_app(tmp_path, start_worker=False)
    pipeline, store = app.state.studio_pipeline, app.state.studio_store
    pid = pipeline.repository.create_project('Synthetic Cell').id
    store.upsert_project(pid, {}, consent=True)
    store.put_plan(pid, {'trial_sponsors': ['Synthetic Cell Therapeutics Inc'], 'is_biomed': True})
    pipeline.settings_store.update({'model': {'protocol': 'openai', 'base_url': 'https://synthetic.example/v1',
        'model': 'fixture', 'api_key': 'synthetic-key'}, 'search': {'provider': 'tavily', 'api_key': 'synthetic-search-key'}})
    company = pipeline.objects.ensure_subject(pid)
    seed = pipeline.evidence.save(sample(pid, target_text).model_copy(update={'visibility': 'public'}))
    inputs = pipeline.business_profiles.inputs(pid, binding=pipeline.model_binding(), subject_id=company['id'],
                                               evidence_ids=[seed['id']])
    pipeline.business_profiles.apply(pid, inputs, {'products': [{
        'company': 'Synthetic Cell', 'name': product, 'source': 'B1', 'quote': target_text,
        'use_case': target_use, 'buyer_type': '', 'delivery': '', 'basis': 'actual', 'polarity': 'asserted'}]})
    calls = {'registry': [], 'search': [], 'model': []}

    def transport(method, url, headers, body, timeout):
        if 'clinicaltrials.gov' in url:
            calls['registry'].append(parse_qs(urlsplit(url).query))
            if registry_status != 200:
                return Response(registry_status, {}, b'{}')
            if reject_filter and 'filter.advanced' in calls['registry'][-1]:
                return Response(400, {}, b'{}')
            return Response(200, {'content-type': 'application/json'}, json.dumps({'studies': studies}).encode())
        data = json.loads(body)
        if 'api.tavily.com' in url:
            calls['search'].append(data['query'])
            results = [{'title': 'Peer news', 'url': 'https://news.test/peers', 'content': f'{name} develops CD19 CAR-T'}
                       for name in web_peers][:1] if web_peers else []
            return Response(200, {}, json.dumps({'results': results}).encode())
        calls['model'].append(url)
        context = json.loads(data['messages'][1]['content'])
        if 'facts' in context:
            answer = {'verdict': 'unknown', 'reason': 'fixture', 'fact_ids': [f['id'] for f in context['facts']]}
        elif 'product_contexts' in context:
            text = context['sources'][0]['text'] if context['sources'] else ''
            answer = {'candidates': [{'name': name, 'source': 'D1', 'quote': f'{name} develops CD19 CAR-T'}
                                     for name in web_peers if name in text]}
        else:
            answer = {'products': []}
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}]}).encode())

    pipeline.http_factory = lambda _: Http(transport)
    pipeline.article_reader = SimpleNamespace(read=lambda url: {'status': 'unavailable', 'text': '', 'error_code': 'fetch_failed'})
    pipeline.app = app
    return pipeline, pid, calls


def run_once(pipeline, pid):
    queued = pipeline.schedule_competition(pid)
    assert queued and queued['kind'] == 'competition'
    job = pipeline.store.claim()
    pipeline.run(job)
    return job


# ---------------------------------------------------------------- descriptors

def test_descriptors_locate_source_spellings_without_inventing_terms():
    from project_watch.studio.registry_peers import descriptors

    found = descriptors('靶向CD19的自体CAR-T细胞免疫治疗产品')
    assert found['targets'] == {'CD19': 'CD19'}
    assert found['modalities'] == {'car-t': 'CAR-T'}
    assert found['cell_sources'] == {'autologous': '自体'}
    english = descriptors('Universal allogeneic chimeric antigen receptor T cells targeting BCMA and CD19')
    assert set(english['targets']) == {'BCMA', 'CD19'}
    assert english['modalities'] == {'car-t': 'chimeric antigen receptor T'}
    assert english['cell_sources'] == {'allogeneic': 'allogeneic'}
    # Embedded tokens are not targets; no modality without a source word.
    assert descriptors('ABCD190 platform for screening')['targets'] == {}
    assert descriptors('cell therapy manufacturing')['modalities'] == {}


def test_target_profile_requires_both_target_and_modality(tmp_path):
    from project_watch.studio.competition_research import research_inputs
    from project_watch.studio.registry_peers import target_profiles

    pipeline, pid, _ = registry_pipeline(tmp_path)
    profiles = target_profiles(pipeline, pid, research_inputs(pipeline, pid))
    assert [p['product_name'] for p in profiles] == ['Cellex']
    assert profiles[0]['targets'] == {'CD19': 'CD19'} and profiles[0]['modalities'] == {'car-t': 'CAR-T'}


def test_non_therapeutic_project_never_calls_registry(tmp_path):
    text = 'Synthetic Cell 的产品 Cellex 用于药物筛选，客户是药企。'
    pipeline, pid, calls = registry_pipeline(tmp_path, target_text=text, target_use='药物筛选')
    run_once(pipeline, pid)
    assert calls['registry'] == []
    saved = pipeline.competition_research.saved(pid)
    assert saved['registry']['status'] == 'not_applicable'


# ---------------------------------------------------------------- end to end

def test_registry_peers_are_typed_located_and_graded(tmp_path):
    from project_watch.studio.business_identity import has_current_product_ownership

    pipeline, pid, calls = registry_pipeline(tmp_path)
    run_once(pipeline, pid)
    assert 1 <= len(calls['registry']) <= 3
    assert calls['registry'][0]['query.spons'] == ['"Synthetic Cell" OR "Synthetic Cell Therapeutics Inc"']
    query = calls['registry'][1]['query.intr'][0]
    assert 'CD19' in query and 'CAR-T' in query
    saved = pipeline.competition_research.saved(pid)
    registry = saved['registry']
    assert registry['status'] == 'completed'
    names = [p['name'] for p in registry['peers']]
    # Hospital and the target's own sponsor are excluded; industry peers kept.
    assert names == ['Alpha Bio Co., Ltd.', 'Gamma Therapeutics', 'Delta Cell']
    assert registry['excluded']['non_industry'] == 1
    assert registry['excluded']['self'] + registry['excluded']['own_product'] == 1  # own sponsor record
    by_name = {p['name']: p for p in registry['peers']}
    alpha = by_name['Alpha Bio Co., Ltd.']['products']
    assert [p['name'] for p in alpha] == ['AB-19 CAR-T cells']  # chemo conditioning is not a product
    assert alpha[0]['level'] == 'same_target_and_modality'
    assert alpha[0]['cell_source'] == 'same'
    assert alpha[0]['nct_ids'] == ['NCT90000001'] and alpha[0]['indications'] == ['Large B-cell Lymphoma']
    gamma = by_name['Gamma Therapeutics']['products']
    assert [p['name'] for p in gamma] == ['GT-01']  # comparator arm excluded
    assert gamma[0]['level'] == 'same_target_and_modality' and gamma[0]['cell_source'] == 'different'
    delta = by_name['Delta Cell']['products']
    assert delta[0]['level'] == 'same_modality' and delta[0]['shared_targets'] == []
    # Located evidence, product ownership and facts pass the existing lexical gates.
    peer = pipeline.objects.get(pid, by_name['Alpha Bio Co., Ltd.']['id'], include_internal=False)
    product = pipeline.objects.get(pid, alpha[0]['product_id'], include_internal=False)
    assert peer['purpose'] == 'related' and product['parent_id'] == peer['id']
    assert has_current_product_ownership(pipeline.store, pid, peer, product, include_internal=False)
    facts = pipeline.objects.facts(pid, product['id'], include_internal=False)
    values = {(f['predicate'], f['value']) for f in facts}
    assert ('registry_target', 'CD19') in values and ('registry_modality', 'CAR-T') in values
    assert ('indication', 'Large B-cell Lymphoma') in values and ('development_phase', 'PHASE2') in values
    assert all(f['value'] in r['quote'] and product['name'] in r['quote'] for f in facts for r in f['refs'])
    evidence = pipeline.evidence.get(pid, facts[0]['refs'][0]['evidence_version_id'], include_internal=False)
    assert evidence['source_type'] == 'public_page' and evidence['read_scope'] != 'summary'
    assert 'clinicaltrials.gov/study/NCT90000001' in evidence['parts'][0]['locator']
    # Registry candidates surface alongside web candidates, labelled by origin.
    origins = {c['name']: c.get('origin') for c in saved['candidates']}
    assert origins['Gamma Therapeutics'] == 'registry'
    # Not a supported competitive relationship.
    assert saved['supported_count'] == 0


def test_replay_does_not_repeat_registry_requests_or_duplicate_facts(tmp_path):
    pipeline, pid, calls = registry_pipeline(tmp_path)
    job = run_once(pipeline, pid)
    before = len(calls['registry'])
    first = pipeline.competition_research.saved(pid)['registry']
    product_id = first['peers'][0]['products'][0]['product_id']
    facts = len(pipeline.objects.facts(pid, product_id, include_internal=False))
    pipeline.run(job)
    assert len(calls['registry']) == before
    assert len(pipeline.objects.facts(pid, product_id, include_internal=False)) == facts
    assert pipeline.competition_research.saved(pid)['registry']['peers'] == first['peers']


def test_second_round_reuses_peer_and_product_objects(tmp_path):
    pipeline, pid, _ = registry_pipeline(tmp_path)
    job = run_once(pipeline, pid)
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    first = pipeline.competition_research.saved(pid)['registry']
    from project_watch.studio.registry_peers import RegistryPeers
    with pipeline.store._connect() as db:
        db.execute('DELETE FROM studio_competition_run_state')
    second = run_once(pipeline, pid)
    assert second['id'] != job['id']
    again = pipeline.competition_research.saved(pid)['registry']
    assert [p['id'] for p in again['peers']] == [p['id'] for p in first['peers']]
    assert RegistryPeers is not None


def test_company_wide_exclusion_skips_registry_sponsor(tmp_path):
    from fastapi.testclient import TestClient

    pipeline, pid, _ = registry_pipeline(tmp_path)
    web = TestClient(pipeline.app)
    version = web.get(f'/api/studio/projects/{pid}').json()['project']['config_version']
    reply = web.post(f'/api/studio/projects/{pid}/competitors/selection', json={
        'name': 'Gamma Therapeutics', 'decision': 'exclude', 'expected_version': version, 'idempotency_key': 'x'})
    assert reply.status_code == 200, reply.text
    run_once(pipeline, pid)
    names = [p['name'] for p in pipeline.competition_research.saved(pid)['registry']['peers']]
    assert 'Gamma Therapeutics' not in names


def test_known_registry_failure_is_recorded_and_web_path_continues(tmp_path):
    pipeline, pid, calls = registry_pipeline(tmp_path, registry_status=503)
    run_once(pipeline, pid)
    saved = pipeline.competition_research.saved(pid)
    assert saved['registry']['status'] == 'failed'
    assert any(f.startswith('registry:') for f in saved['failures'])
    assert calls['search']  # discovery still ran


def test_old_research_checkpoint_stops_before_registry_dispatch(tmp_path):
    from project_watch.studio.competition_research import research_inputs
    from project_watch.studio.execution import JobExecution

    pipeline, pid, calls = registry_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job)
    old = {**research_inputs(pipeline, pid), 'prompt_version': 'research-v18-scoped-publishers'}
    execution.checkpoint('competition_inputs', old)
    with pytest.raises(ValueError, match='old matching contract'):
        pipeline.run(job)
    assert calls['registry'] == [] and calls['search'] == []


ANCHOR_STUDIES = [
    study('NCT90000011', 'Partner Pharma', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'XB-17M CAR-T cells', 'otherNames': ['partnercel'],
         'description': 'Autologous BCMA-directed CAR-T', 'armGroupLabels': ['Arm A']}],
        experimental('Biological: XB-17M CAR-T cells'), conditions=('Multiple Myeloma',)),
    study('NCT90000012', 'Epsilon Cell', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'EP-BC1', 'description': 'Autologous CAR-T targeting BCMA',
         'armGroupLabels': ['Arm A']}],
        experimental('Biological: EP-BC1'), conditions=('Multiple Myeloma',)),
]


def test_code_named_product_anchors_missing_target_on_its_own_registry_record(tmp_path):
    from project_watch.studio.registry_peers import anchor_terms, name_descriptors

    assert anchor_terms('XB-17MCAR-T') == ['XB-17M']
    assert name_descriptors('XB-17MCAR-T')['modalities'] == {'car-t': 'CAR-T'}
    assert anchor_terms('倍诺达') == []
    assert anchor_terms('CD19 CAR-T') == [] and anchor_terms('CD19/CD22 module') == []
    text = 'Synthetic Cell 收到 XB-17MCAR-T 用于自体回输的临床试验批件'
    pipeline, pid, calls = registry_pipeline(tmp_path, studies=ANCHOR_STUDIES, target_text=text,
                                             target_use='用于自体回输', product='XB-17MCAR-T')
    run_once(pipeline, pid)
    registry = pipeline.competition_research.saved(pid)['registry']
    assert calls['registry'][0]['query.intr'] == ['"XB-17M"']
    assert 'query.spons' in calls['registry'][1]  # own sponsor records
    assert 'BCMA' in calls['registry'][2]['query.intr'][0]
    assert registry['target_products'][0]['basis'] == 'own_registry_record'
    assert registry['target_products'][0]['anchor_nct_ids'] == ['NCT90000011']
    # The partner-run record of the target's own product is not a peer.
    assert [p['name'] for p in registry['peers']] == ['Epsilon Cell']
    assert registry['excluded']['own_product'] == 1
    assert registry['peers'][0]['products'][0]['level'] == 'same_target_and_modality'
    assert len(calls['registry']) <= 4
    assert registry['target_products'][0]['indication_families'] == ['multiple-myeloma']


def test_anchor_profile_fills_only_missing_descriptors_and_excludes_own_product():
    from project_watch.studio.registry_peers import anchor_profile, grade

    profile = {'product_id': 'p', 'product_name': 'XB-17MCAR-T', 'targets': {}, 'modalities': {'car-t': 'CAR-T'},
               'cell_sources': {'autologous': '自体'}, 'basis': 'cited_facts', 'anchor_terms': ['XB-17M'], 'anchor_nct_ids': [],
               'indication_families': [], 'indication_basis': [], 'own_conditions': [], 'own_names': []}
    anchored = anchor_profile(profile, ANCHOR_STUDIES)
    assert anchored['targets'] == {'BCMA': 'BCMA'} and anchored['modalities'] == {'car-t': 'CAR-T'}
    assert anchored['basis'] == 'own_registry_record' and anchored['anchor_nct_ids'] == ['NCT90000011']
    assert anchor_profile(profile, ANCHOR_STUDIES[1:]) == profile  # no exact code, nothing borrowed
    peer = {'targets': {'BCMA': 'BCMA'}, 'modalities': {'car-t': 'CAR-T'}, 'cell_sources': {'autologous': 'Autologous'}}
    assert grade([anchored], peer)['level'] == 'same_target_and_modality'


def test_english_project_label_excludes_target_sponsor(tmp_path):
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=[
        *STUDIES, study('NCT90000021', 'Cellforge Therapeutics (Shanghai) Co., Ltd.', 'INDUSTRY', [
            {'type': 'BIOLOGICAL', 'name': 'CF-19', 'description': 'Autologous CD19 CAR-T', 'armGroupLabels': ['Arm A']}],
            experimental('Biological: CF-19'))])
    with pipeline.store._connect() as db:
        db.execute('UPDATE projects SET name=? WHERE id=?', ('合成细胞 / Cellforge Therapeutics', pid))
    run_once(pipeline, pid)
    registry = pipeline.competition_research.saved(pid)['registry']
    assert 'Cellforge Therapeutics (Shanghai) Co., Ltd.' not in [p['name'] for p in registry['peers']]
    # Both own-sponsor records were recognised as the target's own and never reached peers.
    assert set(registry['target_products'][0]['anchor_nct_ids']) >= {'NCT90000005', 'NCT90000021'}
    assert registry['excluded']['own_product'] == 2


def test_peer_queries_ask_server_for_industry_sponsors_and_fall_back_once(tmp_path):
    pipeline, pid, calls = registry_pipeline(tmp_path)
    run_once(pipeline, pid)
    peer_calls = [q for q in calls['registry'] if 'query.intr' in q]
    assert peer_calls and all(q['filter.advanced'] == ['AREA[LeadSponsorClass]INDUSTRY'] for q in peer_calls)
    assert all('filter.advanced' not in q for q in calls['registry'] if 'query.spons' in q)
    assert pipeline.competition_research.saved(pid)['registry']['industry_filter'] == 'applied'

    pipeline, pid, calls = registry_pipeline(tmp_path / 'fallback', reject_filter=True)
    run_once(pipeline, pid)
    registry = pipeline.competition_research.saved(pid)['registry']
    assert registry['industry_filter'] == 'rejected' and registry['failures'] == []
    # One rejected filtered request, then unfiltered for the rest; sponsor class still checked locally.
    assert ['filter.advanced' in q for q in calls['registry'] if 'query.intr' in q] == [True, False, False]
    assert registry['requests'] == 4 and registry['excluded']['non_industry'] == 1
    assert [p['name'] for p in registry['peers']] == ['Alpha Bio Co., Ltd.', 'Gamma Therapeutics', 'Delta Cell']


def test_supportive_drug_described_with_the_therapy_is_not_a_peer_product(tmp_path):
    studies = [study('NCT90000031', 'Zeta Cell', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'ZC-19', 'description': 'Universal CD19 CAR-T', 'armGroupLabels': ['Arm A']},
        {'type': 'DRUG', 'name': 'MMF Immunosuppression', 'armGroupLabels': ['Arm A'],
         'description': 'Start after fludarabine; adjust by CAR-T cell expansion'}],
        experimental('Biological: ZC-19', 'Drug: MMF Immunosuppression'))]
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=studies)
    run_once(pipeline, pid)
    products = pipeline.competition_research.saved(pid)['registry']['peers'][0]['products']
    assert [p['name'] for p in products] == ['ZC-19']


def test_bounded_peer_slots_go_to_closest_programmes(tmp_path):
    from project_watch.studio import registry_peers

    weak = [study(f'NCT9100{i:04d}', f'Weak Sponsor {i}', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': f'W-{i}', 'description': 'Autologous BCMA CAR-T', 'armGroupLabels': ['Arm A']}],
        experimental(f'Biological: W-{i}'), phases=('PHASE3',)) for i in range(registry_peers.MAX_PEERS)]
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=weak + STUDIES[:1])
    run_once(pipeline, pid)
    registry = pipeline.competition_research.saved(pid)['registry']
    # The only same-target sponsor is listed first although it came last in search order.
    assert registry['peers'][0]['name'] == 'Alpha Bio Co., Ltd.'
    assert len(registry['peers']) == registry_peers.MAX_PEERS and registry['excluded']['beyond_peer_limit'] == 1


def arms(*pairs):
    return [{'label': label, 'type': 'EXPERIMENTAL', 'description': text} for label, text in pairs]


def test_umbrella_study_attributes_conditions_to_each_arm_only(tmp_path):
    umbrella = study('NCT90000041', 'Omega Bio', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'CD19/CD22 module', 'description': 'Autologous CD19/CD22 CAR-T', 'armGroupLabels': ['A1']},
        {'type': 'BIOLOGICAL', 'name': 'BCMA/CD19 module', 'description': 'Autologous BCMA/CD19 CAR-T', 'armGroupLabels': ['B1']}],
        arms(('A1', 'R/R B-ALL and B-cell lymphoma'), ('B1', 'R/R multiple myeloma')),
        conditions=('Relapsed B-cell Acute Lymphoblastic Leukemia', 'Relapsed B-cell Non-Hodgkin Lymphoma',
                    'Relapsed Multiple Myeloma', 'T-cell Acute Lymphoblastic Leukemia'))
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=[umbrella])
    run_once(pipeline, pid)
    products = {p['name']: p for p in pipeline.competition_research.saved(pid)['registry']['peers'][0]['products']}
    assert products['CD19/CD22 module']['indications'] == ['Relapsed B-cell Acute Lymphoblastic Leukemia',
                                                            'Relapsed B-cell Non-Hodgkin Lymphoma']
    assert products['BCMA/CD19 module']['indications'] == ['Relapsed Multiple Myeloma']
    assert products['BCMA/CD19 module']['trials'][0]['indication_basis'] == 'arm'
    facts = pipeline.objects.facts(pid, products['BCMA/CD19 module']['product_id'], include_internal=False)
    assert {f['value'] for f in facts if f['predicate'] == 'indication'} == {'Relapsed Multiple Myeloma'}
    # Target is lymphoma (cited Chinese text), so the lymphoma arm outranks myeloma.
    assert products['CD19/CD22 module']['indication_overlap'] == 'same'
    assert products['BCMA/CD19 module']['indication_overlap'] == 'different'


def test_alternative_product_groups_are_not_one_asset(tmp_path):
    grouped = [
        study('NCT90000051', 'Sigma Cell', 'INDUSTRY', [
            {'type': 'DRUG', 'name': 'BCMA CAR-T or CD19 CAR-T or CD19xBCMA', 'armGroupLabels': ['Arm A']}],
            experimental('Drug: x'), conditions=('UC',)),
        study('NCT90000052', 'Sigma Cell', 'INDUSTRY', [
            {'type': 'BIOLOGICAL', 'name': 'CAR-T Autologous T cell injection',
             'otherNames': ['CD19 CAR-T', 'CD20 CAR-T', 'BCMA CAR-T'], 'armGroupLabels': ['Arm A']}],
            experimental('Biological: x'), conditions=('Lymphoma', 'Multiple Myeloma')),
        study('NCT90000053', 'Sigma Cell', 'INDUSTRY', [
            {'type': 'BIOLOGICAL', 'name': 'SG-19', 'description': 'CD19 CAR-T', 'armGroupLabels': ['Arm A']}],
            experimental('Biological: SG-19'))]
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=grouped)
    run_once(pipeline, pid)
    registry = pipeline.competition_research.saved(pid)['registry']
    assert [p['name'] for p in registry['peers'][0]['products']] == ['SG-19']
    assert registry['excluded']['grouped_alternatives'] == 2


def test_single_product_title_and_shared_codes_resolve_one_asset(tmp_path):
    records = [
        study('NCT90000061', 'Kappa Bio', 'INDUSTRY', [
            {'type': 'DRUG', 'name': 'KB19 CAR-T Cells', 'otherNames': ['Fludarabine'], 'armGroupLabels': ['Arm A']}],
            experimental('Drug: KB19 CAR-T Cells'), title='KB19 in CD19-positive Non-Hodgkin Lymphoma'),
        study('NCT90000062', 'Kappa Bio', 'INDUSTRY', [
            {'type': 'BIOLOGICAL', 'name': 'KB19 CAR-T', 'description': 'anti-CD19 CAR-T', 'armGroupLabels': ['Arm A']}],
            experimental('Biological: KB19 CAR-T'), conditions=('Leukemia',), phases=('PHASE1', 'PHASE2'))]
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=records)
    run_once(pipeline, pid)
    products = pipeline.competition_research.saved(pid)['registry']['peers'][0]['products']
    # One asset; the conditioning drug listed among registry otherNames is not an alias.
    assert len(products) == 1 and products[0]['aliases'] == ['KB19 CAR-T']
    assert products[0]['level'] == 'same_target_and_modality' and products[0]['shared_targets'] == ['CD19']
    assert [t['nct'] for t in products[0]['trials']] == ['NCT90000061', 'NCT90000062']
    assert {t['phase'] for t in products[0]['trials']} == {'PHASE2', 'PHASE1/PHASE2'}


def test_own_sponsor_records_exclude_the_same_asset_under_another_sponsor(tmp_path):
    own = study('NCT90000071', 'Synthetic Cell Therapeutics Inc', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'synthacabtagene', 'otherNames': ['Synth-cel'],
         'description': 'Autologous CD19 CAR-T', 'armGroupLabels': ['Arm A']}],
        experimental('Biological: synthacabtagene'), conditions=('Follicular Lymphoma',))
    subsidiary = study('NCT90000072', 'Shanghai Synthetic Sub Co., Ltd.', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'Synth-cel', 'description': 'CD19 CAR T cells', 'armGroupLabels': ['Arm A']}],
        experimental('Biological: Synth-cel'), conditions=('Systemic Lupus Erythematosus',))
    coded = study('NCT90000073', 'Another Sub Ltd.', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'SYN019 (Synthacabtagene Autoleucel)', 'description': 'CD19 CAR-T',
         'armGroupLabels': ['Arm A']}], experimental('Biological: SYN019'))
    own['protocolSection']['armsInterventionsModule']['interventions'][0]['otherNames'].append('SYN019')
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=[own, subsidiary, coded, *STUDIES[:1]])
    run_once(pipeline, pid)
    registry = pipeline.competition_research.saved(pid)['registry']
    assert [p['name'] for p in registry['peers']] == ['Alpha Bio Co., Ltd.']  # subsidiaries running own assets
    target = registry['target_products'][0]
    assert 'Synth-cel' in target['own_names'] and 'Follicular Lymphoma' in target['own_conditions']
    assert target['indication_basis'] == ['cited_text', 'own_registry_record']


def test_same_indication_ranks_before_same_target_in_another_disease(tmp_path):
    from project_watch.studio import registry_peers

    autoimmune = [study(f'NCT9200{i:04d}', f'Lupus Sponsor {i}', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': f'L-{i}', 'description': 'CD19 CAR-T', 'armGroupLabels': ['Arm A']}],
        experimental(f'Biological: L-{i}'), conditions=('Systemic Lupus Erythematosus',), phases=('PHASE3',))
        for i in range(registry_peers.MAX_PEERS)]
    lymphoma = study('NCT92009999', 'Lymphoma Peer', 'INDUSTRY', [
        {'type': 'BIOLOGICAL', 'name': 'LP-22', 'description': 'CD22 CAR-T', 'armGroupLabels': ['Arm A']}],
        experimental('Biological: LP-22'), conditions=('Diffuse Large B-cell Lymphoma',), phases=('PHASE1',))
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=[*autoimmune, lymphoma])
    run_once(pipeline, pid)
    peers = pipeline.competition_research.saved(pid)['registry']['peers']
    assert peers[0]['name'] == 'Lymphoma Peer'
    assert peers[0]['products'][0]['indication_overlap'] == 'same'
    assert peers[1]['products'][0]['indication_overlap'] == 'different'


def test_indication_families_keep_t_cell_and_unknown_wording_apart():
    from project_watch.studio.registry_peers import indication_families

    assert indication_families('T-cell Acute Lymphoblastic Leukemia') == {'t-cell-malignancy'}
    assert indication_families('Relapsed/Refractory B-cell Acute Lymphoblastic Leukemia') == {'b-all'}
    assert indication_families('复发难治大B细胞淋巴瘤') == {'lymphoma'}
    assert indication_families('Leukemia, B-cell') == set()
    assert indication_families('Light Chain (AL) Amyloidosis') == {'al-amyloidosis'}
    assert indication_families('CD33/CLL1 dual-target module for AML') == {'myeloid'}  # CLL1 is an antigen


def test_only_distinctive_registered_names_identify_own_assets():
    from project_watch.studio.registry_peers import distinctive

    assert distinctive('Synth-cel') and distinctive('Relma-cel') and distinctive('relmacabtagene autoleucel')
    assert not distinctive('CD19 CAR-T cells') and not distinctive('Autologous BCMA CAR-T injection')


def test_company_pipeline_overview_is_not_the_product_indication(tmp_path):
    text = ('Synthetic Cell 的产品管线涵盖血液肿瘤及系统性红斑狼疮等自身免疫性疾病。'
            'Synthetic Cell 的主要产品 Cellex 是一款靶向CD19的自体CAR-T细胞免疫治疗产品。')
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=[], target_text=text)
    run_once(pipeline, pid)
    target = pipeline.competition_research.saved(pid)['registry']['target_products'][0]
    assert target['indication_families'] == [] and target['indication_basis'] == []


def test_generic_shared_alias_does_not_merge_two_programmes():
    from project_watch.studio.registry_peers import merge_assets

    items = [{'name': 'Human BCMA Targeted T Cells Injection', 'others': ['BCMA CAR-T']},
             {'name': 'Anti-BCMA CAR-T cells', 'others': ['BCMA CAR-T']},
             {'name': 'SENL103', 'others': []}, {'name': 'SENL103 injection', 'others': []},
             {'name': 'BCMA CAR-T cells', 'others': []}, {'name': 'BCMA CAR-T Cells', 'others': []}]
    # Same primary name (case aside) within one sponsor is one asset even when generic.
    assert [len(g['items']) for g in merge_assets(items)] == [1, 1, 2, 2]


def test_registry_covered_projects_keep_web_leads_but_skip_per_peer_reading(tmp_path):
    pipeline, pid, calls = registry_pipeline(tmp_path, web_peers=('Beta Corp',))
    run_once(pipeline, pid)
    saved = pipeline.competition_research.saved(pid)
    assert saved['web_peer_research'] == 'skipped_structured_sources_covered'
    leads = [c for c in saved['candidates'] if c['origin'] == 'search']
    assert [c['name'] for c in leads] == ['Beta Corp'] and leads[0]['basis'] == 'search_snippet_lead_not_verified'
    # Discovery (3 incl. the company query), the target's own original queries (2)
    # and at most one replacement lookup for an unreadable target page; no per-peer queries.
    assert len(calls['search']) <= 6 and not any(q.startswith('Beta Corp') for q in calls['search'])


def test_institutions_are_not_discovered_as_competitors(tmp_path):
    pipeline, pid, _ = registry_pipeline(tmp_path, studies=[], web_peers=('Children Research Hospital',))
    run_once(pipeline, pid)
    saved = pipeline.competition_research.saved(pid)
    assert not [c for c in saved['candidates'] if c['origin'] == 'search']
    assert saved['candidate_rejections'] == {'institution': 1}


def test_antibody_programmes_are_a_modality():
    from project_watch.studio.registry_peers import descriptors

    found = descriptors('信迪利单抗是一种PD-1抑制剂')
    assert found['targets'] == {'PD-1': 'PD-1'} and 'antibody' in found['modalities']
    assert 'antibody' in descriptors('Anti-PD-1 monoclonal antibody')['modalities']


def test_an_attached_structured_source_feeds_peers_without_core_imports(tmp_path):
    from project_watch.studio.monitoring_contracts import ObjectInput

    class LicensedSource:
        name = 'licensed_db'
        def run(self, service, project_id, inputs, collectors, *, execution, guard, self_names, blocked):
            assert 'Synthetic Cell' in self_names
            peer = service.objects.save(project_id, ObjectInput(kind='company', name='Licensed Peer Co',
                                        purpose='related', visibility='public'), actor='automatic')
            return {'status': 'completed', 'peers': [{'id': peer['id'], 'name': peer['name']}], 'failures': []}

    text = 'Synthetic Cell 的产品 Cellex 用于药物筛选，客户是药企。'
    pipeline, pid, calls = registry_pipeline(tmp_path, target_text=text, target_use='药物筛选', web_peers=('Beta Corp',))
    pipeline.competition_research.extra_sources.append(LicensedSource())
    run_once(pipeline, pid)
    saved = pipeline.competition_research.saved(pid)
    assert saved['sources']['clinical_trials']['status'] == 'not_applicable'
    assert saved['sources']['licensed_db'] == {'status': 'completed', 'requests': None, 'failures': [], 'peer_count': 1}
    assert any(c['origin'] == 'licensed_db' and c['name'] == 'Licensed Peer Co' for c in saved['candidates'])
    assert saved['web_peer_research'] == 'skipped_structured_sources_covered'
    assert calls['registry'] == []
