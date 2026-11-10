"""Synthetic end-to-end evidence handoff regressions; no provider calls."""
import pytest
from test_studio_bilateral_competition import reviewer, setup_pair

from project_watch.studio.competition import assess_relation
from project_watch.studio.monitoring_contracts import ObjectInput


def test_confirmed_company_can_use_its_owned_product_identity_evidence(tmp_path):
    service, pid, scope, _ = setup_pair(tmp_path)
    company = service.objects.get(pid, scope['subject_id'])
    item = ObjectInput.model_validate({k: company[k] for k in ObjectInput.model_fields})
    service.objects.save(pid, item.model_copy(update={'refs': ()}), actor='user',
                         object_id=company['id'], expected_version=company['version'])
    result = assess_relation(service, pid, scope, 'direct', review=reviewer)
    assert result['support'] == 'supported', result['missing']
    assert service.objects.get(pid, company['id'])['refs'] == []


def test_appending_product_source_does_not_discard_previous_dimensions(tmp_path):
    from test_studio_evidence_versions import sample

    from project_watch.studio.monitoring_contracts import LocatedRef

    service, pid, scope, facts = setup_pair(tmp_path)
    product = service.objects.get(pid, scope['subject_product_id'])
    text = 'Alpha organ also has a new source description.'
    e = service.evidence.save(sample(pid, text).model_copy(update={'source_key': 'additional'}))
    ref = LocatedRef(evidence_version_id=e['id'], part_index=0, start=0, end=len(text), quote=text)
    item = ObjectInput.model_validate({k: product[k] for k in ObjectInput.model_fields})
    service.objects.save(pid, item.model_copy(update={'refs': (*item.refs, ref)}), actor='user',
                         object_id=product['id'], expected_version=product['version'])
    assert all(f['dependency']['status'] == 'current' for f in service.objects.facts(pid, product['id']))
    service.evidence.dependencies.withdraw(pid, facts[0][0]['refs'][0]['evidence_version_id'], 'withdrawn', actor='test')
    assert all(f['dependency']['status'] == 'stale' for f in service.objects.facts(pid, product['id']))


def test_identical_assessment_does_not_keep_invalidating_judgment(tmp_path):
    service, pid, scope, _ = setup_pair(tmp_path)
    result = assess_relation(service, pid, scope, 'direct', review=reviewer)
    service.relationships.save_assessment(pid, result)
    generation = service.evidence.dependencies.generation(pid)
    same = assess_relation(service, pid, scope, 'direct', review=reviewer)
    service.relationships.save_assessment(pid, same)
    assert service.evidence.dependencies.generation(pid) == generation
    disputed = assess_relation(service, pid, scope, 'direct', review=lambda context: {
        **reviewer(context), 'verdict': 'disputed', 'reason': 'Conflicting source assertion'})
    service.relationships.save_assessment(pid, disputed)
    assert service.evidence.dependencies.generation(pid) > generation


def commercial_fixture(tmp_path, *, read_scope='full', role='customer', stage='trial'):
    from test_studio_evidence_versions import sample

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.monitoring_contracts import FactInput, LocatedRef
    p = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = p.repository.create_project('Synthetic Optics')
    pid = project.id
    p.store.upsert_project(pid, {'legal_name': project.name}, consent=True)
    subject = p.objects.ensure_subject(pid)
    text = 'Synthetic Optics and Customer Beta agreed a product trial.'
    e = p.evidence.save(sample(pid, text).model_copy(update={'source_key': 'commercial-source',
                                                           'visibility': 'public', 'read_scope': read_scope}))
    ref = LocatedRef(evidence_version_id=e['id'], part_index=0, start=0, end=len(text), quote=text)
    peer = p.objects.save(pid, ObjectInput(kind='company', name='Customer Beta', visibility='public', refs=(ref,)), actor='automatic')
    p.objects.save_fact(pid, FactInput(subject_id=subject['id'], predicate='business_relation', text=text,
        basis='actual', subject_attribution='explicit', visibility='public', refs=(ref,),
        attributes={'kind': 'business_relation', 'peer_id': peer['id'], 'role': role, 'stage': stage}))
    return p, pid, e


def test_commercial_fact_enters_monitoring_once_and_preserves_trial_role(tmp_path):
    from project_watch.studio.relation_delivery import sync
    p, pid, _ = commercial_fixture(tmp_path)
    assert sync(p, pid)['added'] == 1
    assert sync(p, pid)['added'] == 0
    item, = p.monitoring_plan.items(pid)
    assert item['relation_type'] == 'pilot_partner'
    assert item['scope_kind'] == 'relation' and item['axis'] == 'partners'
    p.monitoring_plan.update(item['id'], {'status': 'retired'}, actor='human', reason='owner choice')
    assert sync(p, pid)['added'] == 0
    assert p.monitoring_plan.items(pid)[0]['status'] == 'retired'


def test_withdrawal_retires_only_untouched_automatic_relationship(tmp_path):
    from project_watch.studio.relation_delivery import sync
    p, pid, evidence = commercial_fixture(tmp_path)
    sync(p, pid)
    p.evidence.dependencies.withdraw(pid, evidence['id'], 'correction', actor='test')
    assert sync(p, pid)['retired'] == 1
    assert p.monitoring_plan.items(pid)[0]['status'] == 'retired'


@pytest.mark.parametrize('reason', ['summary', 'paused', 'human_edit'])
def test_relationship_delivery_respects_source_and_owner_boundaries(tmp_path, reason):
    from project_watch.studio.relation_delivery import sync
    p, pid, evidence = commercial_fixture(tmp_path, read_scope='summary' if reason == 'summary' else 'full')
    if reason == 'paused':
        with p.store._connect() as db:
            db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (pid,))
    assert sync(p, pid)['added'] == (0 if reason == 'paused' else 1)
    if reason == 'summary':
        item, = p.monitoring_plan.items(pid)
        assert item['status'] == 'trial' and '线索' in item['reason']
    if reason == 'human_edit':
        item, = p.monitoring_plan.items(pid)
        p.monitoring_plan.update(item['id'], {'reason': 'owner-managed'}, actor='human')
        p.evidence.dependencies.withdraw(pid, evidence['id'], 'correction', actor='test')
        assert sync(p, pid)['retired'] == 0


def test_system_replan_does_not_disable_source_withdrawal(tmp_path):
    from project_watch.studio.relation_delivery import sync
    p, pid, evidence = commercial_fixture(tmp_path)
    sync(p, pid)
    item, = p.monitoring_plan.items(pid)
    p.monitoring_plan.update(item['id'], {'status': 'reduced'}, actor='system')
    p.evidence.dependencies.withdraw(pid, evidence['id'], 'correction', actor='test')
    assert sync(p, pid)['retired'] == 1


def test_existing_sixteen_directions_do_not_block_relation_delivery(tmp_path):
    from project_watch.studio.relation_delivery import sync
    p, pid, _ = commercial_fixture(tmp_path)
    for index in range(16):
        p.monitoring_plan.add(pid, {'text': f'Manual direction {index}'}, actor='human', origin='human')
    assert sync(p, pid) == {'added': 1, 'retired': 0}
    assert len(p.monitoring_plan.items(pid)) == 17


def test_worker_reconciles_saved_facts_without_model_configuration(tmp_path):
    from project_watch.studio.pipeline import StudioWorker
    p, pid, evidence = commercial_fixture(tmp_path)
    assert not p.settings_store.load().model.ready
    worker = StudioWorker(p)
    worker.schedule()
    assert len(p.monitoring_plan.items(pid)) == 1
    p.evidence.dependencies.withdraw(pid, evidence['id'], 'correction', actor='test')
    worker._analysis_checked_at = 0
    worker.schedule()
    assert p.monitoring_plan.items(pid)[0]['status'] == 'retired'
