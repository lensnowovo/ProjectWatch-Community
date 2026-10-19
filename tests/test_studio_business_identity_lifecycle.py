from contextlib import closing
from datetime import date, timedelta

import pytest
from test_studio_bilateral_competition import reviewer
from test_studio_business_identity import FULL, MAPPING, PRODUCT, SHORT, extract, setup
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import Alias, EvidencePart, LocatedRef, ObjectInput
from project_watch.studio.relationship_contracts import RelationshipCorrection, RelationshipDecision


def test_mapping_on_another_page_of_same_current_document_supports_product(tmp_path):
    service, pid, subject, profiles = setup(tmp_path)
    ev = service.evidence.save(sample(pid).model_copy(update={
        'source_key': 'public-profile:multi-page', 'visibility': 'public', 'parts': (
            EvidencePart(locator_kind='pdf_page', locator='1', text=MAPPING),
            EvidencePart(locator_kind='pdf_page', locator='2', text=PRODUCT),
        )}))
    inputs = profiles.inputs(pid, binding='fixture')
    marker = next(p['marker'] for p in inputs['parts'] if p['text'] == PRODUCT)
    proposal = {'products': [{'company': FULL, 'name': 'Lens A', 'source': marker,
        'quote': PRODUCT, 'use_case': 'drug screening', 'buyer_type': 'pharma',
        'delivery': 'software', 'basis': 'actual', 'polarity': 'asserted'}]}
    result = profiles.apply(pid, inputs, proposal)
    assert result['accepted'] == 1
    product = service.objects.get(pid, result['product_ids'][0])
    assert product['parent_id'] == subject['id']
    facts = service.objects.facts(pid, product['id'])
    assert all({r['part_index'] for r in f['refs']} == {0, 1} for f in facts)
    assert all({r['evidence_version_id'] for r in f['refs']} == {ev['id']} for f in facts)
    service.evidence.dependencies.withdraw(pid, ev['id'], 'synthetic correction', actor='user')
    assert profiles.apply(pid, inputs, proposal)['status'] == 'superseded'
    assert all(f['dependency']['status'] == 'stale' for f in service.objects.facts(pid, product['id']))


def test_expiring_alias_interval_is_preserved_on_dependent_facts(tmp_path):
    today = date.today()
    service, pid, _, profiles = setup(tmp_path, actor='user', alias=Alias(name=SHORT,
        visibility='public', valid_from=today-timedelta(days=3), valid_to=today+timedelta(days=1)))
    _, _, result = extract(service, pid, profiles)
    assert result['accepted'] == 1
    facts = service.objects.facts(pid, result['product_ids'][0])
    assert all(f['valid_to'] == (today+timedelta(days=1)).isoformat() for f in facts)
    assert all(f['valid_from'] == (today-timedelta(days=3)).isoformat() for f in facts)


def test_alias_date_correction_changes_inputs_and_invalidates_old_inference(tmp_path):
    today = date.today()
    service, pid, subject, profiles = setup(tmp_path, actor='user', alias=Alias(name=SHORT,
        visibility='public', valid_to=today+timedelta(days=5)))
    inputs, data, _ = extract(service, pid, profiles)
    subject = service.objects.get(pid, subject['id'])
    item = ObjectInput.model_validate({k: subject[k] for k in ObjectInput.model_fields})
    service.objects.save(pid, item.model_copy(update={'aliases': (Alias(name=SHORT,
        visibility='public', valid_to=today+timedelta(days=1)),)}), actor='user',
        object_id=subject['id'], expected_version=subject['version'])
    assert profiles.inputs(pid, binding='fixture')['input_version'] != inputs['input_version']
    assert profiles.apply(pid, inputs, data)['status'] == 'superseded'


@pytest.mark.parametrize('legacy_facts', [False, True])
def test_expired_alias_facts_stop_research_and_downgrade_saved_assessment(tmp_path, monkeypatch, legacy_facts):
    from project_watch.studio import (
        business_identity,
        competition,
        competition_research,
        relationships,
    )

    today = date.today()
    service, pid, subject, profiles = setup(tmp_path, actor='user', mapping=FULL + '为目标主体。',
        alias=Alias(name=SHORT, visibility='public', valid_to=today))
    if legacy_facts:
        # Emulate the pre-v4 extraction contract without editing historical rows.
        with monkeypatch.context() as legacy:
            legacy.setattr('project_watch.studio.business_profile.alias_validities', lambda *_: {})
            _, _, own = extract(service, pid, profiles)
        assert all(f['valid_to'] is None for f in service.objects.facts(pid, own['product_ids'][0]))
    else:
        _, _, own = extract(service, pid, profiles)
    peer_text = '对照光学有限公司研发 Lens B 用于 drug screening；购买方为 pharma；交付形式为 software。'
    ev = service.evidence.save(sample(pid, peer_text).model_copy(update={
        'source_key': 'public-profile:independent-peer', 'visibility': 'public',
        'propagation_family': 'independent-peer', 'family_basis': 'source_only'}))
    ref = LocatedRef(evidence_version_id=ev['id'], part_index=0, start=0, end=len(peer_text), quote=peer_text)
    peer = service.objects.save(pid, ObjectInput(kind='company', name='对照光学有限公司', visibility='public', refs=(ref,)), actor='automatic')
    inputs = profiles.inputs(pid, binding='fixture', subject_id=peer['id'], evidence_ids=[ev['id']])
    other = profiles.apply(pid, inputs, {'products': [{'company': peer['name'], 'name': 'Lens B',
        'source': 'B1', 'quote': peer_text, 'use_case': 'drug screening', 'buyer_type': 'pharma',
        'delivery': 'software', 'basis': 'actual', 'polarity': 'asserted'}]})
    scope = {'subject_id': subject['id'], 'peer_id': peer['id'],
        'subject_product_id': own['product_ids'][0], 'peer_product_id': other['product_ids'][0]}
    assessed = competition.assess_relation(service, pid, scope, 'direct', review=reviewer)
    assert assessed['support'] == 'supported', assessed['missing']
    service.relationships.assessment_validator = competition.assess_relation
    service.relationships.save_assessment(pid, assessed)
    assert service.relationships.page(pid)[0]['support'] == 'supported'

    class NextDay(date):
        @classmethod
        def today(cls):
            return today + timedelta(days=1)

    for module in (business_identity, competition, competition_research):
        monkeypatch.setattr(module, 'date', NextDay)
    monkeypatch.setattr(relationships, 'date', NextDay, raising=False)
    view = service.relationships.page(pid)[0]
    assert view['status'] == 'stale' and view['support'] == 'pending'
    assert view['previous_support'] == 'supported'
    assert service.relationships.history(pid)[0]['support'] == 'supported'
    assert competition_research.research_inputs(service, pid)['products'] == []
    receipt = service.relationships.correct(service, pid, RelationshipCorrection(
        expected_version=service.store.get_project(pid)['config_version'],
        idempotency_key='synthetic-expired-user-choice',
        changes=(RelationshipDecision(**scope, decision='include', relation_type='direct',
                                      reason='Synthetic user tracking choice'),)))
    assert receipt['job_status'] == 'not_requested'
    chosen = service.relationships.page(pid)[0]
    assert chosen['origin'] == 'user_override' and chosen['support'] == 'user_specified'
    assert chosen['status'] == 'current'
    assert service.relationships.history(pid)[0]['support'] == 'supported'


def test_definition_after_former_five_part_cutoff_is_read_and_located(tmp_path):
    service, pid, _, profiles = setup(tmp_path)
    parts = tuple(EvidencePart(locator_kind='pdf_page', locator=str(i), text=text)
                  for i, text in enumerate((PRODUCT, 'one', 'two', 'three', 'four', MAPPING), start=1))
    evidence = service.evidence.save(sample(pid).model_copy(update={
        'source_key': 'public-profile:bounded-document', 'visibility': 'public', 'parts': parts}))
    inputs = profiles.inputs(pid, binding='fixture')
    part = next(p for p in inputs['parts'] if p['text'] == PRODUCT)
    assert SHORT in part['company_names']
    assert {p['part_index'] for p in inputs['parts']} == set(range(6))
    refs = part['company_names'][SHORT]
    assert refs and all(r['part_index'] == 5 and r['evidence_version_id'] == evidence['id'] for r in refs)
    for ref in refs:
        service.evidence.resolve(pid, LocatedRef(**ref))


def test_parse_only_private_document_cannot_supply_public_alias_definition(tmp_path):
    from project_watch.repository import ProjectRepository

    service, pid, _, profiles = setup(tmp_path)
    source = ProjectRepository(service.store.db_path).add_source(pid, kind='document',
        reference='synthetic-private.txt', content_sha256='b'*64)
    service.evidence.save(sample(pid, MAPPING).model_copy(update={
        'source_key': 'synthetic-private', 'internal_source_id': source.id}))
    with closing(service.store._connect()) as db, db:
        db.execute('INSERT INTO studio_source_permissions VALUES (?,?,?,?,?)',
                   (source.id, pid, '', 'fixture', 'today'))
    inputs, _, result = extract(service, pid, profiles)
    assert all(p['visibility'] == 'public' for p in inputs['parts'])
    assert all(SHORT not in p['company_names'] for p in inputs['parts'])
    assert result['accepted'] == 0


def test_document_definition_is_independent_of_manual_alias_expiry(tmp_path, monkeypatch):
    from project_watch.studio import business_identity, competition_research

    today = date.today()
    service, pid, _, profiles = setup(tmp_path, actor='user', alias=Alias(name=SHORT,
        visibility='public', valid_to=today))
    _, _, result = extract(service, pid, profiles, MAPPING + PRODUCT)
    assert result['accepted'] == 1
    facts = service.objects.facts(pid, result['product_ids'][0])
    assert all(f['valid_to'] is None for f in facts)
    assert all(len(f['refs']) == 2 for f in facts)

    class NextDay(date):
        @classmethod
        def today(cls):
            return today + timedelta(days=1)

    monkeypatch.setattr(business_identity, 'date', NextDay)
    monkeypatch.setattr(competition_research, 'date', NextDay)
    assert competition_research.research_inputs(service, pid)['products'] == result['product_ids']
