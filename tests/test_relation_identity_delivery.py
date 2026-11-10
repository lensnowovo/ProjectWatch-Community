"""Synthetic identity and candidate admission; no provider or desktop access."""
import json

import pytest
from test_evidence_delivery import commercial_fixture
from test_studio_structured_facts import prepare

from project_watch.studio.business_identity import located_attribution
from project_watch.studio.business_relations import project_relation
from project_watch.studio.relation_delivery import sync


@pytest.mark.parametrize('confirmed', [False, True])
def test_only_confirmed_short_name_establishes_attribution(tmp_path, confirmed):
    p, pid, _, inputs, _ = prepare(tmp_path)
    subject = p.objects.get(pid, inputs['subject_id'])
    if confirmed:
        with p.store._connect() as db:
            p.objects.confirm_subject_identity(pid, subject['name'], ['SynOptics'], db=db)
        subject = p.objects.get(pid, subject['id'])
    with p.store._connect() as db:
        refs = located_attribution(p.store, pid, subject, 'SynOptics works with Beta.',
            {'text': 'SynOptics works with Beta.', 'evidence_version_id': 'unused', 'part_index': 0}, db=db)
    assert (refs is not None) is confirmed


def test_inline_definition_is_saved_as_attribution_dependency(tmp_path):
    p, pid, _evidence, _, proposal = prepare(tmp_path)
    from test_studio_evidence_versions import sample
    text = 'Synthetic Optics（简称SynOptics）。SynOptics works with Beta, a pilot customer, in a trial.'
    p.evidence.save(sample(pid, text).model_copy(update={'source_key': 'public-profile:roles', 'visibility': 'public'}))
    inputs = p.structured_facts.inputs(pid, p.model_binding())
    source = next(part for part in inputs['parts'] if part['text'] == text)
    proposal['relations'][0].update(source=source['marker'], quote=text.split('。')[1])
    proposal['policies'] = []
    assert 'SynOptics' in inputs['names']
    result = p.structured_facts.apply(pid, inputs, proposal)
    assert len(result['fact_ids']) == 1
    fact = next(f for f in p.objects.facts(pid, inputs['subject_id']) if f['id'] == result['fact_ids'][0])
    assert fact['subject_attribution'] == 'explicit'
    assert len(fact['refs']) == 2
    assert project_relation(p, pid, fact)['support'] == 'source_assertion'
    assert p.structured_facts.apply(pid, inputs, proposal) == result


def test_summary_lead_is_trial_and_does_not_upgrade_fact(tmp_path):
    p, pid, _ = commercial_fixture(tmp_path, read_scope='summary')
    with p.store._connect() as db:
        before = db.execute('SELECT record FROM studio_fact_records WHERE project_id=?', (pid,)).fetchone()[0]
    assert sync(p, pid)['added'] == 1
    watch, = p.monitoring_plan.items(pid)
    assert watch['status'] == 'trial' and '线索' in watch['reason']
    assert project_relation(p, pid, json.loads(before))['support'] == 'pending'
    with p.store._connect() as db:
        assert db.execute('SELECT record FROM studio_fact_records WHERE project_id=?', (pid,)).fetchone()[0] == before
    p.monitoring_plan.update(watch['id'], {'status': 'retired'}, actor='human')
    assert sync(p, pid)['added'] == 0


def test_located_competitor_candidate_enters_trial_without_supported_promotion(tmp_path):
    p, pid, _ = commercial_fixture(tmp_path)
    with p.store._connect() as db:
        fact = json.loads(db.execute('SELECT record FROM studio_fact_records WHERE project_id=?', (pid,)).fetchone()[0])
    scope = {'subject_id': fact['subject_id'], 'peer_id': fact['attributes']['peer_id'],
             'subject_product_id': None, 'peer_product_id': None}
    p.relationships.save_candidate(pid, scope, relation_type='direct', rationale='Synthetic candidate')
    assert sync(p, pid)['added'] == 2
    watch = next(w for w in p.monitoring_plan.items(pid) if w['relation_type'] == 'competitor')
    assert watch['relation_type'] == 'competitor' and watch['status'] == 'trial'
    assert p.relationships.page(pid)[0]['support'] == 'pending'
    assert sync(p, pid)['added'] == 0


def test_unlocated_competitor_name_does_not_create_monitoring(tmp_path):
    from project_watch.studio.monitoring_contracts import ObjectInput
    p, pid, _ = commercial_fixture(tmp_path)
    subject = p.objects.ensure_subject(pid)
    peer = p.objects.save(pid, ObjectInput(kind='company', name='Unlocated Peer', visibility='public'), actor='automatic')
    p.relationships.save_candidate(pid, {'subject_id': subject['id'], 'peer_id': peer['id'],
        'subject_product_id': None, 'peer_product_id': None}, relation_type='direct')
    sync(p, pid)
    assert all(w['relation_target'] != 'Unlocated Peer' for w in p.monitoring_plan.items(pid))


def test_pending_lead_withdrawal_retires_trial_without_deleting_fact(tmp_path):
    p, pid, evidence = commercial_fixture(tmp_path, read_scope='summary')
    sync(p, pid)
    p.evidence.dependencies.withdraw(pid, evidence['id'], 'synthetic withdrawal', actor='test')
    assert sync(p, pid)['retired'] == 1
    with p.store._connect() as db:
        assert db.execute('SELECT count(*) FROM studio_fact_records WHERE project_id=?', (pid,)).fetchone()[0] == 1
