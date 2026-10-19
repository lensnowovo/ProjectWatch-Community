import pytest
from test_studio_bilateral_competition import setup_pair


@pytest.mark.parametrize(('role', 'stage', 'expected'), [('customer', 'trial', 'pilot_partner'),
    ('customer', 'order', 'customer'), ('partner', 'strategic', 'partner'), ('investor', 'announced', 'investor'),
    ('supplier', 'delivery', 'supplier')])
def test_relation_roles_do_not_imply_sales_or_financing(tmp_path, role, stage, expected):
    from project_watch.studio.business_relations import project_relation
    service, pid, scope, facts = setup_pair(tmp_path)
    row = {**facts[0][0], 'attributes': {'kind': 'business_relation', 'peer_id': scope['peer_id'], 'role': role, 'stage': stage}}
    result = project_relation(service, pid, row)
    # Fixture's old product quote does not identify the other company.
    assert result['support'] == 'pending'
    assert result['role'] == expected
    assert not result['implies_revenue'] and not result['implies_funding_received']


def test_relation_schema_rejects_foreign_or_missing_peer(tmp_path):
    from project_watch.studio.monitoring_contracts import FactInput
    service, pid, scope, facts = setup_pair(tmp_path)
    with pytest.raises((ValueError, KeyError)):
        service.objects.save_fact(pid, FactInput(subject_id=scope['subject_id'], predicate='business_relation', text='Alpha and foreign',
            refs=tuple(facts[0][0]['refs']), attributes={'kind': 'business_relation', 'peer_id': 'foreign', 'role': 'customer', 'stage': 'trial'}))


def test_both_roles_have_located_original_and_peer_revision_stales_relation(tmp_path):
    from test_studio_evidence_versions import sample

    from project_watch.studio.business_relations import project_relation
    from project_watch.studio.monitoring_contracts import FactInput, LocatedRef, ObjectInput
    service, pid, scope, _ = setup_pair(tmp_path)
    text = 'Alpha and Beta agreed a trial; Beta is a pilot customer, not a commercial buyer.'
    e = service.evidence.save(sample(pid, text).model_copy(update={'source_key': 'trial-role'}))
    ref = LocatedRef(evidence_version_id=e['id'], part_index=0, start=0, end=len(text), quote=text)
    fact = service.objects.save_fact(pid, FactInput(subject_id=scope['subject_id'], predicate='business_relation', text=text,
        basis='actual', subject_attribution='explicit', refs=(ref,), attributes={'kind': 'business_relation',
            'peer_id': scope['peer_id'], 'role': 'customer', 'stage': 'trial'}))
    result = project_relation(service, pid, fact)
    assert result['role'] == 'pilot_partner' and result['support'] == 'source_assertion'
    peer = service.objects.get(pid, scope['peer_id'])
    service.objects.save(pid, ObjectInput(kind='company', name='Different Beta'), object_id=peer['id'], expected_version=peer['version'], actor='user')
    assert project_relation(service, pid, fact)['support'] == 'pending'
