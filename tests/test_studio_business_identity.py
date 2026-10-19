from datetime import date, timedelta

import pytest
from test_studio_competition_profile import profile_inputs
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import Alias, LocatedRef, ObjectInput

FULL = '合成光学有限公司'
SHORT = '合成光学'
PRODUCT = '合成光学研发 Lens A 用于 drug screening；购买方为 pharma；交付形式为 software。'
MAPPING = FULL + '（以下简称“合成光学”）。'


def setup(tmp_path, *, alias=None, actor='automatic', mapping=None, visibility='public'):
    service, pid, subject, profiles, _, _ = profile_inputs(tmp_path)
    refs = ()
    if mapping is not None:
        ev = service.evidence.save(sample(pid, mapping).model_copy(update={
            'source_key': 'identity-mapping', 'visibility': visibility,
        }))
        refs = (LocatedRef(evidence_version_id=ev['id'], part_index=0, start=0,
                           end=len(mapping), quote=mapping),)
    subject = service.objects.save(pid, ObjectInput(kind='company', name=FULL,
        visibility='public' if visibility == 'public' else 'internal', purpose='subject',
        aliases=(alias,) if alias else (), refs=refs), actor=actor,
        object_id=subject['id'], expected_version=subject['version'])
    return service, pid, subject, profiles


def extract(service, pid, profiles, text=PRODUCT, *, company=FULL):
    service.evidence.save(sample(pid, text).model_copy(update={
        'source_key': 'public-profile:alias-product', 'visibility': 'public',
    }))
    inputs = profiles.inputs(pid, binding='fixture')
    part = next(p for p in inputs['parts'] if p['text'] == text)
    data = {'products': [{'company': company, 'name': 'Lens A', 'source': part['marker'],
        'quote': PRODUCT if PRODUCT in text else text, 'use_case': 'drug screening',
        'buyer_type': 'pharma', 'delivery': 'software', 'basis': 'actual', 'polarity': 'asserted'}]}
    return inputs, data, profiles.apply(pid, inputs, data)


@pytest.mark.parametrize('company', [FULL, SHORT])
def test_same_part_explicit_abbreviation_keeps_raw_quote_and_parent(tmp_path, company):
    service, pid, subject, profiles = setup(tmp_path)
    _, _, result = extract(service, pid, profiles, MAPPING + PRODUCT, company=company)
    assert result['accepted'] == 1
    product = service.objects.get(pid, result['product_ids'][0])
    assert product['parent_id'] == subject['id']
    facts = service.objects.facts(pid, product['id'])
    assert all(f['refs'][0]['quote'] == PRODUCT for f in facts)
    assert all(any(MAPPING.strip('。') in r['quote'] for r in f['refs']) for f in facts)
    assert service.objects.get(pid, subject['id'])['name'] == FULL


@pytest.mark.parametrize('actor,mapping', [('user', None), ('automatic', MAPPING)])
def test_current_scoped_alias_extracts_product(tmp_path, actor, mapping):
    service, pid, _, profiles = setup(tmp_path, alias=Alias(name=SHORT, visibility='public'),
                                      actor=actor, mapping=mapping)
    _, _, result = extract(service, pid, profiles)
    assert result['accepted'] == 1


@pytest.mark.parametrize('alias,actor,mapping', [
    (Alias(name=SHORT, visibility='public'), 'automatic', None),
    (Alias(name=SHORT, visibility='public'), 'automatic', FULL + '与合成光学集团合作。'),
    (Alias(name=SHORT, visibility='internal'), 'user', None),
    (Alias(name=SHORT, visibility='public', valid_to=date.today()-timedelta(days=1)), 'user', None),
    (Alias(name=SHORT, visibility='public', valid_from=date.today()+timedelta(days=1)), 'user', None),
])
def test_unproven_private_or_out_of_date_alias_does_not_establish_ownership(tmp_path, alias, actor, mapping):
    service, pid, _, profiles = setup(tmp_path, alias=alias, actor=actor, mapping=mapping)
    _, _, result = extract(service, pid, profiles)
    assert result['accepted'] == 0


@pytest.mark.parametrize('replacement', ['合成光学集团', '合成光学科技有限公司', '合成光学的子公司'])
def test_alias_is_not_a_prefix_for_group_or_subsidiary(tmp_path, replacement):
    service, pid, _, profiles = setup(tmp_path, alias=Alias(name=SHORT, visibility='public'), actor='user')
    _, _, result = extract(service, pid, profiles, PRODUCT.replace(SHORT, replacement))
    assert result['accepted'] == 0


def test_alias_mapping_is_not_borrowed_from_an_unrelated_source(tmp_path):
    service, pid, _, profiles = setup(tmp_path)
    service.evidence.save(sample(pid, MAPPING).model_copy(update={
        'source_key': 'public-profile:unlinked-mapping', 'visibility': 'public'}))
    _, _, result = extract(service, pid, profiles)
    assert result['accepted'] == 0


def test_alias_correction_during_inference_supersedes_saved_inputs(tmp_path):
    service, pid, subject, profiles = setup(tmp_path, alias=Alias(name=SHORT, visibility='public'), actor='user')
    inputs, data, _ = extract(service, pid, profiles)
    subject = service.objects.get(pid, subject['id'])
    service.objects.save(pid, ObjectInput(kind='company', name=FULL, purpose='subject', visibility='public'),
        actor='user', object_id=subject['id'], expected_version=subject['version'])
    assert profiles.inputs(pid, binding='fixture')['input_version'] != inputs['input_version']
    assert profiles.apply(pid, inputs, data)['status'] == 'superseded'
    assert all(f['dependency']['status'] == 'stale' for f in service.objects.facts(pid,
        next(o['id'] for o in service.objects.page(pid) if o['kind'] == 'product')))


def test_revised_mapping_invalidates_existing_alias_facts(tmp_path):
    service, pid, _, profiles = setup(tmp_path, alias=Alias(name=SHORT, visibility='public'), mapping=MAPPING)
    inputs, data, result = extract(service, pid, profiles)
    assert result['accepted'] == 1
    service.evidence.save(sample(pid, FULL + '不使用此简称。').model_copy(update={
        'source_key': 'identity-mapping', 'visibility': 'public'}))
    assert profiles.inputs(pid, binding='fixture')['input_version'] != inputs['input_version']
    assert profiles.apply(pid, inputs, data)['status'] == 'superseded'
    assert all(f['dependency']['status'] == 'stale' for f in
               service.objects.facts(pid, result['product_ids'][0]))


def test_withdrawn_mapping_does_not_resurrect_alias_or_existing_facts(tmp_path):
    service, pid, subject, profiles = setup(tmp_path, alias=Alias(name=SHORT, visibility='public'), mapping=MAPPING)
    _, _, result = extract(service, pid, profiles)
    assert result['accepted'] == 1
    service.evidence.dependencies.withdraw(pid, subject['refs'][0]['evidence_version_id'],
                                          'synthetic correction', actor='user')
    current = profiles.inputs(pid, binding='fixture')
    assert SHORT not in current['alias_bindings']
    assert all(f['dependency']['status'] == 'stale' for f in
               service.objects.facts(pid, result['product_ids'][0]))


def test_shared_model_payload_exposes_names_not_private_aliases_or_mapping_bodies(tmp_path):
    from project_watch.studio.business_profile import model_payload

    service, pid, _, profiles = setup(tmp_path, alias=Alias(name=SHORT, visibility='public'), mapping=MAPPING)
    inputs, _, _ = extract(service, pid, profiles)
    payload = model_payload(inputs)
    source = next(p for p in payload['sources'] if p['text'] == PRODUCT)
    assert set(source['company_names']) == {FULL, SHORT}
    assert source['text'] == PRODUCT
    assert 'alias_bindings' not in payload and 'subject_identity' not in payload


def test_saved_business_checkpoint_stops_before_new_model_request(tmp_path):
    import json

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.http import Http, Response
    from project_watch.studio.llm import ModelError

    app = create_studio_app(tmp_path, start_worker=False)
    pipeline, store = app.state.studio_pipeline, app.state.studio_store
    project = pipeline.repository.create_project('Synthetic Optics')
    pid = project.id
    store.upsert_project(pid, {}, consent=True)
    pipeline.settings_store.update({'model': {'protocol': 'openai', 'base_url': 'https://synthetic.example/v1',
        'model': 'fixture', 'api_key': 'synthetic-key'}})
    subject = pipeline.objects.ensure_subject(pid)
    text = 'Synthetic Optics builds Lens A for pharma; delivery is software.'
    pipeline.evidence.save(sample(pid, text).model_copy(update={
        'visibility': 'public', 'source_key': 'public-profile:source'}))
    binding = pipeline.model_binding()
    inputs = pipeline.business_profiles.inputs(pid, binding=binding)
    store.enqueue(pid, 'business', input_version=inputs['input_version'])
    claimed = store.claim()
    execution = JobExecution(store, claimed)
    execution.checkpoint('business_inputs', inputs)
    pipeline.objects.save(pid, ObjectInput(kind='company', name='Changed Optics', visibility='public', purpose='subject'),
        actor='user', object_id=subject['id'], expected_version=subject['version'])
    calls = []

    def transport(*args):
        calls.append(args[0])
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': '{"products":[]}'}}]}).encode())

    pipeline.http_factory = lambda _: Http(transport)
    pipeline._execution_state.current = execution
    with pytest.raises(ModelError, match='旧检查点未重发'):
        pipeline.run_business(claimed)
    assert calls == []


def test_extracted_abbreviations_reach_bilateral_review_and_withdrawal_stops_it(tmp_path):
    from test_studio_bilateral_competition import reviewer

    from project_watch.studio.competition import assess_relation

    service, pid, subject, profiles = setup(tmp_path)
    _, _, own = extract(service, pid, profiles, MAPPING + PRODUCT)
    peer_name, peer_short = '对照光学有限公司', '对照光学'
    peer_text = peer_name + '（以下简称“' + peer_short + '”）。' + PRODUCT.replace(SHORT, peer_short).replace('Lens A', 'Lens B')
    ev = service.evidence.save(sample(pid, peer_text).model_copy(update={
        'source_key': 'public-profile:peer-original', 'visibility': 'public',
        'propagation_family': 'independent-peer-publisher', 'family_basis': 'direct'}))
    ref = LocatedRef(evidence_version_id=ev['id'], part_index=0, start=0, end=len(peer_text), quote=peer_text)
    peer = service.objects.save(pid, ObjectInput(kind='company', name=peer_name, visibility='public', refs=(ref,)), actor='automatic')
    inputs = profiles.inputs(pid, binding='fixture', subject_id=peer['id'], evidence_ids=[ev['id']])
    other = profiles.apply(pid, inputs, {'products': [{'company': peer_name, 'name': 'Lens B',
        'source': 'B1', 'quote': PRODUCT.replace(SHORT, peer_short).replace('Lens A', 'Lens B'),
        'use_case': 'drug screening', 'buyer_type': 'pharma', 'delivery': 'software',
        'basis': 'actual', 'polarity': 'asserted'}]})
    assert own['accepted'] == other['accepted'] == 1
    scope = {'subject_id': subject['id'], 'peer_id': peer['id'],
             'subject_product_id': own['product_ids'][0], 'peer_product_id': other['product_ids'][0]}
    checked = assess_relation(service, pid, scope, 'direct', include_internal=False, review=reviewer)
    assert checked['support'] == 'supported', checked['missing']
    service.relationships.assessment_validator = assess_relation
    service.relationships.save_assessment(pid, checked)
    service.evidence.dependencies.withdraw(pid, ev['id'], 'synthetic peer correction', actor='user')
    assert service.relationships.page(pid)[0]['status'] == 'stale'
    assert assess_relation(service, pid, scope, 'direct', include_internal=False, review=reviewer)['support'] != 'supported'


@pytest.mark.parametrize('alias', ['公司', '该公司', '本集团', '集团', 'the company'])
@pytest.mark.parametrize('actor', ['automatic', 'user'])
def test_generic_pronoun_is_never_a_cross_document_company_alias(tmp_path, alias, actor):
    mapping = FULL + '（以下简称“' + alias + '”）。'
    service, pid, _, profiles = setup(tmp_path, alias=Alias(name=alias, visibility='public'),
                                      actor=actor, mapping=mapping)
    text = PRODUCT.replace(SHORT, alias)
    _, _, result = extract(service, pid, profiles, text, company=alias)
    assert result['accepted'] == 0
    current = profiles.inputs(pid, binding='fixture')
    assert alias not in current['alias_bindings']


def test_generic_definition_inside_document_does_not_become_an_alias(tmp_path):
    from project_watch.studio.business_identity import explicit_aliases

    service, pid, _, profiles = setup(tmp_path)
    mapping = FULL + '（以下简称“公司”）。'
    _, _, result = extract(service, pid, profiles, mapping + PRODUCT.replace(SHORT, '公司'), company='公司')
    assert result['accepted'] == 0
    assert explicit_aliases(FULL, mapping) == ()


def test_legal_name_parentheses_spelling_keeps_exact_source_and_canonical_parent(tmp_path):
    name = '合成光学(测试)有限公司'
    quoted_name = '合成光学（测试）有限公司'
    service, pid, subject, profiles = setup(tmp_path)
    service.objects.save(pid, ObjectInput(kind='company', name=name, purpose='subject', visibility='public'),
                         actor='user', object_id=subject['id'], expected_version=subject['version'])
    text = PRODUCT.replace(SHORT, quoted_name)
    inputs, _, _ = extract(service, pid, profiles, text, company=quoted_name)
    part = next(p for p in inputs['parts'] if p['text'] == text)
    data = {'products': [{'company': quoted_name, 'name': 'Lens A', 'source': part['marker'],
                         'quote': text, 'use_case': 'drug screening', 'buyer_type': 'pharma',
                         'delivery': 'software', 'basis': 'actual', 'polarity': 'asserted'}]}
    result = profiles.apply(pid, inputs, data)
    assert result['accepted'] == 1
    product = service.objects.get(pid, result['product_ids'][0])
    assert product['parent_id'] == subject['id']
    assert product['refs'][0]['quote'] == text
    assert service.objects.get(pid, subject['id'])['name'] == name


def test_explicit_definition_accepts_parentheses_spelling_not_a_different_entity():
    from project_watch.studio.business_identity import exact_company_mention, explicit_aliases

    name = '合成光学(测试)有限公司'
    assert explicit_aliases(name, '合成光学（测试）有限公司（简称“合成光学”）')[0].name == SHORT
    assert exact_company_mention(name, '合成光学（测试）有限公司研发 Lens A')
    assert not exact_company_mention(name, '合成光学（另一地区）有限公司研发 Lens A')
    assert not exact_company_mention(name, '合成光学（测试）有限公司的子公司研发 Lens A')


def contextual_product(service, pid, profiles, text, quote, context, *, company=FULL):
    service.evidence.save(sample(pid, text).model_copy(update={
        'source_key': 'public-profile:context-product', 'visibility': 'public'}))
    inputs = profiles.inputs(pid, binding='fixture')
    part = next(p for p in inputs['parts'] if p['text'] == text)
    return inputs, {'products': [{'company': company, 'name': 'Lens A', 'source': part['marker'],
        'quote': quote, 'ownership_quote': context, 'use_case': 'drug screening',
        'buyer_type': 'pharma', 'delivery': 'software', 'basis': 'actual', 'polarity': 'asserted'}]}


def test_source_local_context_preserves_product_sentence_and_ownership_refs(tmp_path):
    from project_watch.studio.business_identity import has_current_product_ownership

    service, pid, subject, profiles = setup(tmp_path)
    quote = PRODUCT.replace(SHORT, '该公司')
    context = FULL + '研发光学软件。' + quote
    inputs, data = contextual_product(service, pid, profiles, context, quote, context)
    result = profiles.apply(pid, inputs, data)
    assert result['accepted'] == 1
    product = service.objects.get(pid, result['product_ids'][0])
    assert product['parent_id'] == subject['id']
    assert product['refs'][0]['quote'] == quote
    assert any(r['quote'] == context for r in product['refs'])
    facts = service.objects.facts(pid, product['id'])
    assert all(f['refs'][0]['quote'] == quote for f in facts)
    assert all(any(r['quote'] == context for r in f['refs']) for f in facts)
    assert has_current_product_ownership(service.objects.store, pid,
        service.objects.get(pid, subject['id']), product, include_internal=False)
    assert all('该公司' not in p['company_names'] for p in inputs['parts'])
    service.evidence.dependencies.withdraw(pid, product['refs'][0]['evidence_version_id'],
                                          'synthetic withdrawal', actor='user')
    assert all(f['dependency']['status'] == 'stale' for f in
               service.objects.facts(pid, product['id']))


@pytest.mark.parametrize('context', [
    FULL + '研发光学软件。',  # A separate identity sentence cannot replace product ownership context.
    FULL + '研发光学软件。' + PRODUCT.replace(SHORT, '该公司') + '模型添加的说明',
    '合成光学集团研发光学软件。' + PRODUCT.replace(SHORT, '该公司'),
    FULL + '的子公司研发光学软件。' + PRODUCT.replace(SHORT, '该公司'),
])
def test_ownership_context_must_be_same_exact_containing_passage(tmp_path, context):
    service, pid, _, profiles = setup(tmp_path)
    quote = PRODUCT.replace(SHORT, '该公司')
    text = FULL + '研发光学软件。' + quote
    inputs, data = contextual_product(service, pid, profiles, text, quote, context)
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


def test_named_other_actor_in_context_does_not_borrow_target_identity(tmp_path):
    service, pid, _, profiles = setup(tmp_path)
    quote = PRODUCT.replace(SHORT, '对照光学有限公司')
    text = FULL + '与对照光学有限公司合作。' + quote
    inputs, data = contextual_product(service, pid, profiles, text, quote, text,
                                      company='对照光学有限公司')
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


def test_unique_whitespace_product_span_preserves_source_spelling(tmp_path):
    service, pid, _, profiles = setup(tmp_path)
    text = FULL + '研发LensA，用于drug screening。'
    inputs, data = contextual_product(service, pid, profiles, text, text, '')
    data['products'][0].update(buyer_type='', delivery='')
    result = profiles.apply(pid, inputs, data)
    assert result['accepted'] == 1
    product = service.objects.get(pid, result['product_ids'][0])
    assert product['name'] == 'LensA'
    assert product['refs'][0]['quote'] == text


@pytest.mark.parametrize('name,text', [
    ('Lens A', 'LensA / Lens\nA'),  # Two different source spellings: do not guess.
    ('Lens A for pharma', 'LensA'),
    ('lens a', 'LensA'),
    ('Lens A', 'LensAB'),
    ('Lens A（软件）', 'LensA(软件)'),
])
def test_product_span_repair_does_not_rewrite_words_or_choose_ambiguity(tmp_path, name, text):
    service, pid, _, profiles = setup(tmp_path)
    quote = FULL + '研发' + text + '，用于drug screening。'
    inputs, data = contextual_product(service, pid, profiles, quote, quote, '')
    data['products'][0].update(name=name, buyer_type='', delivery='')
    assert profiles.apply(pid, inputs, data)['accepted'] == 0


def test_unquoted_dimension_is_omitted_without_losing_located_product(tmp_path):
    service, pid, _, profiles = setup(tmp_path)
    text = FULL + '研发 Lens A 用于 drug screening。'
    inputs, data = contextual_product(service, pid, profiles, text, text, '')
    result = profiles.apply(pid, inputs, data)
    assert result['accepted'] == 1 and result['rejected_fields'] == 2
    facts = service.objects.facts(pid, result['product_ids'][0])
    assert [(f['predicate'], f['value']) for f in facts] == [('use_case', 'drug screening')]
