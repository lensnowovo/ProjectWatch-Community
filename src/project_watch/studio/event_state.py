"""Attributed event chronology. No assumed forward stage order or investment impact."""

import json
from contextlib import closing
from datetime import UTC, date, datetime

from project_watch.studio.business_relations import current_fact
from project_watch.studio.public_dates import date_in_quote


def project_event(service, project_id, fact, *, include_internal=True):
    from project_watch.studio.commitments import source_links
    attrs = fact['attributes']
    subject = service.objects.get(project_id, fact['subject_id'], include_internal=include_internal)
    reasons = []
    if not current_fact(service, project_id, fact):
        reasons.append('stale_or_outside_validity')
    if fact['subject_attribution'] != 'explicit' or fact['basis'] != 'actual':
        reasons.append('explicit_actual_attribution')
    if attrs['role'] in ('mentioned', 'unknown') or (attrs['channel'] == 'funding' and attrs['role'] != 'recipient'):
        reasons.append('subject_role')
    if not fact.get('occurred_at'):
        reasons.append('occurrence_date')
    terms = [subject['name'], attrs['name'], attrs['state_text']]
    dates = [date.fromisoformat(fact[k]) for k in ('occurred_at', 'disclosed_at') if fact.get(k)]
    if not any(all(t in r['quote'] for t in terms) and all(date_in_quote(d, r['quote']) for d in dates) for r in fact['refs']):
        reasons.append('located_event_stage_and_dates')
    if attrs['state'] == 'unknown':
        reasons.append('stage')
    if any(service.evidence.get(project_id, r['evidence_version_id'], include_internal=include_internal)['read_scope'] == 'summary'
           for r in fact['refs']):
        reasons.append('original_passage')
    return {'fact_id': fact['id'], 'subject_id': subject['id'], 'subject_name': subject['name'], **attrs,
            'support': 'pending' if reasons else 'source_assertion', 'missing': list(dict.fromkeys(reasons)),
            **{k: fact.get(k) for k in ('occurred_at', 'disclosed_at', 'discovered_at', 'valid_from', 'valid_to')},
            'refs': fact['refs'], 'sources': source_links(service, project_id, fact['refs']),
            'assertion_basis': 'source_assertion_not_independent_verification'}


def timeline(service, project_id, *, include_internal=False, cutoff=None):
    cutoff = cutoff or datetime.now(UTC)
    if cutoff.tzinfo is None:
        raise ValueError('cutoff requires an explicit timezone')
    service.project(project_id) if hasattr(service, 'project') else service.store.get_project(project_id)
    with closing(service.store._connect()) as db:
        rows = db.execute("SELECT record FROM studio_fact_records WHERE project_id=? AND (? OR visibility='public') "
                          "AND json_extract(record,'$.attributes.kind')='event' ORDER BY id LIMIT 501",
                          (project_id, include_internal)).fetchall()
    truncated = len(rows) > 500
    groups = {}
    for row in rows[:500]:
        fact = json.loads(row[0])
        if datetime.fromisoformat(fact['discovered_at']) > cutoff or (
                fact.get('occurred_at') and fact['occurred_at'] > cutoff.date().isoformat()) or (
                fact.get('disclosed_at') and fact['disclosed_at'] > cutoff.date().isoformat()):
            continue
        try:
            projected = project_event(service, project_id, fact, include_internal=include_internal)
        except KeyError:
            continue
        key = (fact['subject_id'], projected['channel'], projected['name'], fact['period'])
        groups.setdefault(key, []).append(projected)
    output = []
    for (subject_id, channel, name, period), history in sorted(groups.items()):
        current = [h for h in history if h['support'] == 'source_assertion']
        latest = max((h['occurred_at'] for h in current), default=None)
        supporting = [h for h in current if h['occurred_at'] == latest]
        states = {h['state'] for h in supporting}
        support = 'incomplete' if truncated else 'conflict' if len(states) > 1 else 'source_assertion' if states else 'pending'
        output.append({'subject_id': subject_id, 'channel': channel, 'name': name, 'period': period,
            'state': next(iter(states)) if len(states) == 1 and not truncated else None, 'support': support,
            'effective_at': latest, 'supporting_fact_ids': [h['fact_id'] for h in supporting],
            'sources': [s for h in supporting for s in h.get('sources', [])],
            'history': [{**h, 'refs': [{k: v for k, v in r.items() if k != 'quote'} for r in h['refs']]}
                for h in sorted(history, key=lambda h: (h['occurred_at'] or '', h['discovered_at'], h['fact_id']))[-20:]],
            'history_truncated': len(history) > 20})
    if len(output) > 50:
        truncated = True
        output = output[:50]
    return {'project_id': project_id, 'schema_version': 1, 'items': output, 'truncated': truncated,
            'cutoff': cutoff.isoformat(), 'evaluated_at': datetime.now(UTC).isoformat(),
            'dependency_scope': 'current_dependencies_not_historical_reconstruction', 'investment_judgment': False}
