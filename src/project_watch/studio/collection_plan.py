"""Pure, versioned collection plans and receipt reconciliation.

The host supplies scoped queries and capability decisions. Model summaries never
serve as query receipts. This module performs no I/O and dispatches no requests.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

MODES = frozenset({'first', 'incremental', 'refresh', 'topic'})
CAPABILITIES = frozenset({'available', 'not_configured', 'not_integrated',
                          'not_applicable', 'disabled', 'needs_scope'})
OUTCOMES = frozenset({'running', 'success', 'failed', 'unknown', 'budget_deferred'})
EXCLUDED = frozenset({'not_applicable', 'disabled', 'not_due'})


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} required')
    return value


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def make_plan(*, round_id, project_id, binding, mode, candidates, at):
    """Freeze capability, schedule and query scope without changing saved cadence.

    Candidates are produced by source adapters and confirmed project scope, not
    free-form model finish text. Manual refresh bypasses cadence, not consent.
    Each query carries a version and window; pagination is reconciled by its
    executor before a logical query can report success.
    """
    for name, value in [('round_id', round_id), ('project_id', project_id), ('binding', binding)]:
        _text(value, name)
    if mode not in MODES:
        raise ValueError('invalid collection mode')
    moment = _time(at)
    units = {}
    for candidate in candidates:
        unit = {key: _text(candidate.get(key), key) for key in
                ('source', 'target_id', 'category', 'query_version')}
        capability = candidate.get('capability')
        if capability not in CAPABILITIES:
            raise ValueError('invalid source capability')
        query = candidate.get('query', '')
        if not isinstance(query, str) or (capability == 'available' and not query.strip()):
            raise ValueError('available source requires scoped query')
        reason = candidate.get('reason', '')
        if not isinstance(reason, str) or (capability != 'available' and not reason.strip()):
            raise ValueError('capability decision requires reason')
        start, end = candidate.get('window_start'), candidate.get('window_end')
        if end is None or _time(end) > moment or (start is not None and _time(start) > _time(end)):
            raise ValueError('invalid collection window')
        due_at = candidate.get('due_at')
        due = due_at is None or _time(due_at) <= moment or mode in ('first', 'refresh')
        if 'schedule_due' in candidate:
            if type(candidate['schedule_due']) is not bool:
                raise ValueError('invalid schedule decision')
            due = candidate['schedule_due']
        basis = candidate.get('applicability_evidence_ids', [])
        if not isinstance(basis, list) or any(not isinstance(v, str) or not v for v in basis):
            raise ValueError('invalid applicability evidence')
        priority = candidate.get('priority', 1)
        if type(priority) is not int or priority < 0:
            raise ValueError('invalid priority')
        unit.update(query=query, window_start=start, window_end=end)
        # A logical query identity is independent of queue order and round id.
        # Scope/config binding and project keep reuse within its valid boundary.
        query_id = _digest({'project_id': project_id, 'binding': binding, **unit})
        unit.update(id=query_id, capability=capability, due_at=due_at,
                    schedule_due=due, applicability_evidence_ids=list(basis),
                    required=capability not in ('disabled', 'not_applicable') and due,
                    disposition=capability if capability != 'available' else 'pending' if due else 'not_due',
                    reason=reason, priority=priority)
        if query_id in units and units[query_id] != unit:
            raise ValueError('conflicting duplicate query')
        units[query_id] = unit
    return {'schema_version': 1, 'round_id': round_id, 'project_id': project_id,
            'binding': binding, 'mode': mode, 'created_at': at,
            'units': sorted(units.values(), key=lambda u: (
                u['priority'], _time(u['due_at']).timestamp() if u['due_at'] else float('-inf'), u['id']))}


def reconcile(plan, receipts):
    """Return execution coverage, not a claim of complete real-world information.

    Empty plans cannot certify coverage. Blocked capabilities remain visible even
    when a schedule is not due. Success requires retrieval AND durable ingestion;
    truncated pages and pending saves cannot certify a completed query.
    """
    if plan.get('schema_version') != 1:
        raise ValueError('unsupported collection plan')
    expected = {u['id']: u for u in plan['units']}
    if len(expected) != len(plan['units']):
        raise ValueError('duplicate collection plan unit')
    actual = {}
    for receipt in receipts:
        key = receipt.get('unit_id')
        if receipt.get('round_id') != plan['round_id'] or key not in expected:
            raise ValueError('receipt outside collection round')
        if key in actual:
            raise ValueError('duplicate logical query receipt')
        if receipt.get('outcome') not in OUTCOMES:
            raise ValueError('invalid collection outcome')
        if expected[key]['disposition'] != 'pending':
            raise ValueError('receipt for undispatched query')
        actual[key] = receipt
    rows = []
    for key, unit in expected.items():
        receipt = actual.get(key)
        outcome = receipt['outcome'] if receipt else unit['disposition']
        count = saved = None
        if outcome == 'success' or (receipt and ('result_count' in receipt or 'saved_count' in receipt)):
            count, saved = receipt.get('result_count'), receipt.get('saved_count')
            if (type(count) is not int or count < 0 or type(saved) is not int
                    or not 0 <= saved <= count):
                raise ValueError('success requires valid result and saved counts')
            request_ids = receipt.get('request_ids')
            if not isinstance(request_ids, list) or not request_ids or any(
                    not isinstance(i, str) or not i for i in request_ids):
                raise ValueError('success requires request provenance')
            if outcome == 'success' and (receipt.get('truncated') is not False
                                         or receipt.get('ingestion_complete') is not True):
                outcome = 'partial'
        rows.append({'unit_id': key, 'source': unit['source'], 'target_id': unit['target_id'],
                     'category': unit['category'], 'schedule_due': unit['schedule_due'],
                     'window_start': unit['window_start'], 'window_end': unit['window_end'],
                     'capability': unit['capability'], 'required': unit['required'],
                     'outcome': outcome, 'result_count': count, 'saved_count': saved,
                     'reason': receipt.get('reason', '') if receipt else unit['reason']})
    unresolved = [r['unit_id'] for r in rows if r['outcome'] not in EXCLUDED | {'success'}]
    return {'status': 'complete' if rows and not unresolved else 'partial',
            'rows': rows, 'unresolved_ids': unresolved,
            'required_count': sum(r['required'] for r in rows),
            'successful_count': sum(r['outcome'] == 'success' for r in rows),
            'empty_plan': not rows}


def channel_key(row):
    """Source identity takes precedence over its discovery/news direction."""
    return {'site': 'site', 'cninfo': 'filing', 'openalex_affiliation': 'paper',
            'openalex_author': 'paper', 'openalex_topic': 'paper', 'patent_search': 'patent',
            'clinical_sponsor': 'trial', 'clinical_term': 'trial', 'cn_regulatory': 'cn_trial'}.get(
                row['source'], row['category'])


def channel_status(rows):
    """Keep capability, schedule and execution independent; no content inference."""
    active = [r for r in rows if r['outcome'] not in EXCLUDED | {'scope_resolved'}]
    outcomes = {r['outcome'] for r in active or rows if r['outcome'] != 'scope_resolved'}
    execution = next(iter(outcomes)) if len(outcomes) == 1 else 'partial' if outcomes else 'not_in_round'
    if 'unknown' in outcomes:
        execution = 'unknown'
    elif 'running' in outcomes:
        execution = 'running'
    if execution == 'success' and all(r['result_count'] == 0 for r in active):
        execution = 'empty'
    return dict(execution=execution, capabilities=sorted({r['capability'] for r in rows}),
                due_count=sum(r['schedule_due'] for r in rows), query_count=len(rows),
                successful_count=sum(r['outcome'] == 'success' for r in rows))


def resolved_scopes(base, supplements):
    """Link initially unscoped requirements to evidence-bound concrete queries.

    A sponsor-less clinical discovery requirement may be scoped by an explicit
    intervention query. This mirrors build_collection_plan's sponsor/term
    alternatives; it does not claim the sponsor endpoint was queried.
    Capability errors and real failed queries can never be removed this way.
    """
    result = {}
    for parent in base['units']:
        if parent['capability'] != 'needs_scope' or parent['query'] or parent['target_id'] != base['project_id']:
            continue
        adapters = {'clinical_sponsor', 'clinical_term'} if parent['source'] in (
            'clinical_sponsor', 'clinical_term') else {parent['source']}
        children = [unit['id'] for plan in supplements
                    if plan['project_id'] == base['project_id'] and plan['round_id'] == base['round_id']
                    and plan['binding'] == base['binding']
                    for unit in plan['units'] if unit['source'] in adapters and unit['query'].strip()
                    and unit['category'] == parent['category'] and unit['disposition'] == 'pending'
                    and unit['target_id'].startswith('scope:') and unit['applicability_evidence_ids']
                    and unit['capability'] == 'available']
        if children:
            result[parent['id']] = list(dict.fromkeys(children))
    return result
