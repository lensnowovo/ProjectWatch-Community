"""Source money facts with explicit measures/scopes; never a cash-gap engine."""

import json
import re
from decimal import Decimal, localcontext

from project_watch.studio.business_relations import current_fact
from project_watch.studio.normalize import normalize_amount, period_key


def project_funding(service, project_id, fact, *, include_internal=True):
    attrs = fact['attributes']
    subject = service.objects.get(project_id, fact['subject_id'], include_internal=include_internal)
    missing = []
    if not current_fact(service, project_id, fact):
        missing.append('stale_or_outside_validity')
    if fact['subject_attribution'] != 'explicit' or fact['basis'] == 'unknown':
        missing.append('subject_or_basis')
    if attrs['stage'] == 'received' and fact['basis'] != 'actual':
        missing.append('actual_receipt')
    terms = [subject['name'], attrs['stage_text']]
    for key in ('value', 'unit', 'period', 'occurred_at'):
        if not fact.get(key):
            missing.append(key)
        else:
            terms.append(fact[key])
    if fact['consolidation_scope'] == 'unknown':
        missing.append('consolidation_scope')
    else:
        terms.append(fact['consolidation_scope'])
    if attrs.get('milestone_name'):
        terms.append(attrs['milestone_name'])
    if not any(all(t in r['quote'] for t in terms) for r in fact['refs']):
        missing.append('located_money_scope')
    if any(service.evidence.get(project_id, r['evidence_version_id'], include_internal=include_internal)['read_scope'] == 'summary'
           for r in fact['refs']):
        missing.append('original_passage')
    raw, normalized, norm_unit, inferred_currency = fact.get('value'), None, fact['unit'], ''
    if raw is not None and re.fullmatch(r'[-+]?\d+(?:\.\d+)?', raw):
        with localcontext() as context:
            context.prec = 120
            normalized, norm_unit, inferred_currency = normalize_amount(Decimal(raw), fact['unit'], fact['text'])
        normalized = format(normalized, 'f')
    else:
        missing.append('numeric_value')
    currency = fact['currency']
    if not currency or not inferred_currency or currency != inferred_currency:
        missing.append('explicit_consistent_currency')
    current = not missing
    return {'fact_id': fact['id'], 'subject_id': subject['id'], 'measure': attrs['measure'], 'stage': attrs['stage'],
        'raw_value': raw, 'raw_unit': fact['unit'], 'currency': currency, 'period': fact['period'],
        'normalized_value': normalized, 'normalized_unit': norm_unit, 'period_key': period_key(fact['period']),
        'consolidation_scope': fact['consolidation_scope'], 'basis': fact['basis'], 'occurred_at': fact['occurred_at'],
        'milestone_name': attrs.get('milestone_name'), 'support': 'source_assertion' if current else 'pending',
        'is_funding_received': current and attrs['measure'] == 'funding' and attrs['stage'] == 'received' and Decimal(raw) > 0,
        'is_cash_balance': current and attrs['measure'] == 'cash_balance' and fact['basis'] == 'actual',
        'cash_gap': None, 'missing': list(dict.fromkeys(missing)), 'refs': fact['refs'],
        'series_key': json.dumps([subject['id'], attrs['measure'], currency, period_key(fact['period']),
                                 fact['consolidation_scope'], fact['basis']], ensure_ascii=False),
        'assertion_basis': 'source_assertion_not_independent_verification'}
