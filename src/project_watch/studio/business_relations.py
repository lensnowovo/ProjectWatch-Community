"""Source-backed commercial roles; cooperation is not an order or cash receipt."""

from datetime import date


def current_fact(service, project_id, fact):
    dependency = fact.get('dependency') or service.evidence.dependencies.get(project_id, 'fact:' + fact['id'])
    day = date.today().isoformat()
    return bool(dependency and dependency['status'] == 'current' and
                (not fact.get('valid_from') or fact['valid_from'] <= day) and
                (not fact.get('valid_to') or fact['valid_to'] >= day))


def project_relation(service, project_id, fact, *, include_internal=True):
    from project_watch.studio.commitments import source_links

    item = fact['attributes']
    role, stage = item['role'], item['stage']
    if role == 'customer' and stage == 'trial':
        role = 'pilot_partner'
    subject = service.objects.get(project_id, fact['subject_id'], include_internal=include_internal)
    if subject['kind'] == 'product' and subject['parent_id']:
        subject = service.objects.get(project_id, subject['parent_id'], include_internal=include_internal)
    peer = service.objects.get(project_id, item['peer_id'], include_internal=include_internal)
    reasons = []
    if not current_fact(service, project_id, fact):
        reasons.append('stale_or_outside_validity')
    if fact['subject_attribution'] != 'explicit' or fact['basis'] != 'actual':
        reasons.append('explicit_actual_attribution')
    if not any(subject['name'] in r['quote'] and peer['name'] in r['quote'] for r in fact['refs']):
        reasons.append('located_both_parties')
    if role == 'customer' and stage not in ('order', 'delivery', 'acceptance', 'revenue', 'payment'):
        reasons.append('commercial_stage')
    if role == 'unknown':
        reasons.append('role')
    for ref in fact['refs']:
        evidence = service.evidence.get(project_id, ref['evidence_version_id'], include_internal=include_internal)
        if evidence['read_scope'] == 'summary':
            reasons.append('original_passage')
    return {'fact_id': fact['id'], 'subject_id': subject['id'], 'subject_name': subject['name'],
            'subject_kind': subject['kind'], 'peer_id': peer['id'], 'peer_name': peer['name'],
            'role': role, 'stage': stage, 'support': 'pending' if reasons else 'source_assertion',
            'missing': list(dict.fromkeys(reasons)), 'implies_revenue': False, 'implies_funding_received': False,
            'visibility': fact['visibility'], 'refs': fact['refs'], 'sources': source_links(service, project_id, fact['refs']),
            'assertion_basis': 'source_assertion_not_independent_verification'}
