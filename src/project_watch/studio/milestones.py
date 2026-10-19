"""Original, adjusted, actual and delay assertions remain separate and traceable."""

import json
from contextlib import closing
from datetime import UTC, datetime

from project_watch.studio.business_relations import current_fact


def project_milestone(service, project_id, fact, *, include_internal=True):
    attrs = fact['attributes']
    subject = service.objects.get(project_id, fact['subject_id'], include_internal=include_internal)
    missing = []
    if not current_fact(service, project_id, fact):
        missing.append('stale_or_outside_validity')
    if fact['subject_attribution'] != 'explicit' or fact['basis'] == 'unknown':
        missing.append('subject_or_basis')
    if attrs['phase'] == 'actual' and fact['basis'] != 'actual':
        missing.append('actual_completion')
    if attrs['phase'] == 'actual' and attrs['target_date'] and fact['occurred_at'] and attrs['target_date'] > fact['occurred_at']:
        missing.append('actual_date_after_assertion')
    if attrs['phase'] in ('original', 'adjusted', 'actual') and not attrs['target_date']:
        missing.append('target_date')
    if not fact['occurred_at']:
        missing.append('assertion_date')
    terms = [subject['name'], attrs['name'], attrs['phase_text']]
    terms += [v for v in (attrs['target_date'], fact['occurred_at']) if v]
    if not any(all(t in r['quote'] for t in terms) for r in fact['refs']):
        missing.append('located_plan_phase_and_dates')
    if any(service.evidence.get(project_id, r['evidence_version_id'], include_internal=include_internal)['read_scope'] == 'summary'
           for r in fact['refs']):
        missing.append('original_passage')
    return {'fact_id': fact['id'], 'subject_id': subject['id'], **attrs, 'basis': fact['basis'],
        'occurred_at': fact['occurred_at'], 'discovered_at': fact['discovered_at'],
        'support': 'pending' if missing else 'source_assertion', 'missing': missing, 'refs': fact['refs']}


def read(service, project_id, *, include_internal=False, cutoff=None):
    cutoff = cutoff or datetime.now(UTC)
    if cutoff.tzinfo is None:
        raise ValueError('cutoff requires timezone')
    service.project(project_id) if hasattr(service, 'project') else service.store.get_project(project_id)
    with closing(service.store._connect()) as db:
        rows = db.execute("SELECT record FROM studio_fact_records WHERE project_id=? AND (? OR visibility='public') "
                          "AND json_extract(record,'$.attributes.kind')='milestone' ORDER BY id LIMIT 501",
                          (project_id, include_internal)).fetchall()
    groups = {}
    for row in rows[:500]:
        fact = json.loads(row[0])
        if datetime.fromisoformat(fact['discovered_at']) > cutoff or (fact['occurred_at'] and fact['occurred_at'] > cutoff.date().isoformat()):
            continue
        try:
            item = project_milestone(service, project_id, fact, include_internal=include_internal)
        except KeyError:
            continue
        groups.setdefault((item['subject_id'], item['name']), []).append(item)
    output = []
    for (subject_id, name), history in sorted(groups.items()):
        supported = [h for h in history if h['support'] == 'source_assertion']
        latest = max((h['occurred_at'] for h in supported), default=None)
        phases = {h['phase'] for h in supported if h['occurred_at'] == latest}
        output.append({'subject_id': subject_id, 'name': name,
            'phase': next(iter(phases)) if len(phases) == 1 and len(rows) <= 500 else None,
            'support': 'incomplete' if len(rows) > 500 else 'conflict' if len(phases) > 1 else 'source_assertion' if supported else 'pending',
            'original_dates': sorted({h['target_date'] for h in history if h['phase'] == 'original' and h['target_date']
                                     and not (set(h['missing']) - {'stale_or_outside_validity'})}),
            'adjusted_dates': sorted({h['target_date'] for h in supported if h['phase'] == 'adjusted' and h['target_date']}),
            'actual_dates': sorted({h['target_date'] for h in supported if h['phase'] == 'actual' and h['target_date'] <= cutoff.date().isoformat()}),
            'history': [{**h, 'refs': [{k: v for k, v in r.items() if k != 'quote'} for r in h['refs']]}
                        for h in sorted(history, key=lambda h: (h['occurred_at'] or '', h['discovered_at'], h['fact_id']))[-20:]],
            'history_truncated': len(history) > 20})
    return {'project_id': project_id, 'schema_version': 1, 'items': output[:50], 'truncated': len(rows) > 500 or len(output) > 50,
            'cutoff': cutoff.isoformat(), 'dependency_scope': 'current_dependencies_not_historical_reconstruction', 'investment_judgment': False}
