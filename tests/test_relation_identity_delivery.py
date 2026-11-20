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


def test_delivery_stands_down_when_a_pending_relationship_changes_mid_run(tmp_path):
    """Pending writes no longer bump the generation; delivery must still not act on rows it read before."""
    p, pid, _ = commercial_fixture(tmp_path)
    with p.store._connect() as db:
        fact = json.loads(db.execute('SELECT record FROM studio_fact_records WHERE project_id=?', (pid,)).fetchone()[0])
    scope = {'subject_id': fact['subject_id'], 'peer_id': fact['attributes']['peer_id'],
             'subject_product_id': None, 'peer_product_id': None}
    p.relationships.save_candidate(pid, scope, relation_type='direct', rationale='Synthetic candidate')
    page, calls = p.relationships.page, []

    def racing_page(*args, **kwargs):
        rows = page(*args, **kwargs)
        if not calls:
            p.relationships.save_candidate(pid, scope, relation_type='unrelated', rationale='Synthetic correction')
        calls.append(1)
        return rows

    generation = p.evidence.dependencies.generation(pid)
    p.relationships.page = racing_page
    assert sync(p, pid).get('superseded')
    assert p.evidence.dependencies.generation(pid) == generation
    p.relationships.page = page
    sync(p, pid)
    assert not any(w['relation_type'] == 'competitor' for w in p.monitoring_plan.items(pid))


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


def _competitor(tmp_path):
    p, pid, evidence = commercial_fixture(tmp_path)
    with p.store._connect() as db:
        fact = json.loads(db.execute('SELECT record FROM studio_fact_records WHERE project_id=?', (pid,)).fetchone()[0])
    scope = {'subject_id': fact['subject_id'], 'peer_id': fact['attributes']['peer_id'],
             'subject_product_id': None, 'peer_product_id': None}
    p.relationships.save_candidate(pid, scope, relation_type='direct', rationale='Synthetic candidate')
    sync(p, pid)
    watch = next(w for w in p.monitoring_plan.items(pid) if w['relation_type'] == 'competitor')
    return p, pid, scope, watch, evidence


def _bump_peer(p, pid, scope):
    from project_watch.studio.monitoring_contracts import ObjectInput
    peer = p.objects.get(pid, scope['peer_id'])
    fields = {k: peer[k] for k in ObjectInput.model_fields if k not in ('aliases', 'refs') and k in peer}
    p.objects.save(pid, ObjectInput(**fields, refs=tuple(peer['refs'])), actor='automatic', object_id=peer['id'],
                   expected_version=peer['version'])
    assert p.relationships.page(pid)[0]['scope_identity_changed']


def _status(p, pid, watch):
    return next(w for w in p.monitoring_plan.items(pid) if w['id'] == watch['id'])['status']


def test_a_competitor_watch_survives_its_peer_gaining_a_version(tmp_path):
    """Live M2: each later object update made the relationship stale and retired the watch for good."""
    p, pid, scope, watch, _ = _competitor(tmp_path)
    _bump_peer(p, pid, scope)
    assert sync(p, pid)['retired'] == 0
    assert _status(p, pid, watch) == 'trial'


def test_a_competitor_watch_still_retires_when_its_relationship_is_corrected(tmp_path):
    p, pid, scope, watch, _ = _competitor(tmp_path)
    p.relationships.save_candidate(pid, scope, relation_type='unrelated', rationale='Synthetic correction')
    sync(p, pid)
    assert next(w for w in p.monitoring_plan.items(pid) if w['id'] == watch['id'])['status'] == 'retired'


def test_a_disputed_relationship_that_goes_stale_still_retires_its_watch(tmp_path):
    p, pid, _scope, watch, _ = _competitor(tmp_path)
    page = p.relationships.page
    p.relationships.page = lambda *a, **k: [{**r, 'status': 'stale', 'support': 'pending', 'previous_support': 'disputed',
                                             'scope_identity_changed': True} for r in page(*a, **k)]
    sync(p, pid)
    assert _status(p, pid, watch) == 'retired'


def test_a_competitor_watch_retires_when_its_evidence_is_withdrawn(tmp_path):
    p, pid, scope, watch, evidence = _competitor(tmp_path)
    key = p.relationships.page(pid)[0]['scope_key']
    p.evidence.dependencies.track(pid, 'relationship:' + key, 'relationship', [evidence['id']])
    _bump_peer(p, pid, scope)
    p.evidence.dependencies.withdraw(pid, evidence['id'], 'synthetic withdrawal', actor='test')
    sync(p, pid)
    assert _status(p, pid, watch) == 'retired'
