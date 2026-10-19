"""Conservative condition matching, not a legal opinion or investment impact."""

from datetime import date

from project_watch.studio.business_relations import current_fact


def project_policy(service, project_id, fact, *, include_internal=True):
    policy = fact['attributes']
    subject = service.objects.get(project_id, fact['subject_id'], include_internal=include_internal)
    if subject['kind'] == 'product' and subject['parent_id']:
        subject = service.objects.get(project_id, subject['parent_id'], include_internal=include_internal)
    day, missing = date.today().isoformat(), []
    expired = policy['status'] == 'expired' or (policy.get('effective_to') and policy['effective_to'] < day)
    if not current_fact(service, project_id, fact):
        missing.append('current_evidence')
    if policy['status'] != 'in_force' or not policy.get('effective_from') or policy['effective_from'] > day:
        missing.append('effective_rule')
    if not subject['jurisdiction'] or subject['jurisdiction'] not in policy['jurisdictions']:
        missing.append('matching_jurisdiction')
    classifications = {f['value'] for f in service.objects.facts(project_id, subject['id'], include_internal=include_internal)
                       if f['predicate'] == 'business_classification' and f['subject_attribution'] == 'explicit'
                       and f['basis'] not in ('forecast', 'budget') and f['value']
                       and any(f['value'] in r['quote'] and subject['name'] in r['quote'] for r in f['refs'])
                       and current_fact(service, project_id, f)}
    if not classifications.intersection(policy['applicable_to']):
        missing.append('matching_applicable_subject')
    text = '\n'.join(r['quote'] for r in fact['refs'])
    terms = [policy['name'], *policy['jurisdictions'], *policy['applicable_to']]
    terms += [d for d in (policy.get('effective_from'), policy.get('effective_to')) if d]
    if any(s not in text for s in terms):
        missing.append('located_conditions')
    return {'fact_id': fact['id'], 'name': policy['name'], 'status': policy['status'],
            'applicability': 'not_current' if expired else 'pending' if missing else 'conditions_match_source_assertions',
            'missing': missing, 'investment_impact': 'not_evaluated', 'legal_compliance': 'not_evaluated',
            'visibility': fact['visibility'], 'refs': fact['refs']}
