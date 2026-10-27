"""Review retained assertions before projecting them into public product views."""
from __future__ import annotations

import json

from project_watch.studio import dsh_judgment_review as review
from project_watch.studio.dsh_bootstrap import digest
from project_watch.studio.http import HttpError

VERSION = 'public-record-admission-v2'
CONTENT = ('label', 'text', 'date', 'reason', 'relation_type', 'relation_target', 'relation_aliases')
GUIDANCE = ('\n每个 field 是一条候选记录，包含标题、正文及关系属性；逐项断言全部支持才能判 supported。'
            '正面支持必须引用该 field 的 bound_evidence；其余 evidence 可用于反证。'
            '引文选择最短充分的连续原文片段，不改写。')


def payload_for(research, rows):
    fields, numbered, bindings = {}, {}, {}
    for key, row in sorted(rows.items()):
        fields[key] = json.dumps({k: row[k] for k in CONTENT if row.get(k)}, ensure_ascii=False, sort_keys=True)
        bindings[key] = []
        for ref in row['refs']:
            version = research.store.evidence.get(research.project_id, ref['evidence_version_id'], include_internal=False)
            name = 'E' + digest([version['id'], ref['part_index']])[:24]
            numbered[name] = {'context': version['parts'][ref['part_index']]['text'],
                'title': ref.get('title', ''), 'read_scope': version['read_scope'],
                'source_type': version['source_type'], 'evidence_version_id': version['id']}
            bindings[key].append(name)
    return {'purpose': 'public_record_admission', 'analysis': fields, 'fields': fields,
            'evidence': numbered, 'bound_evidence': bindings}


def validate_records(value, payload):
    """Quarantine inexact quotes only after checking batch and source identity."""
    if not isinstance(value, dict) or not isinstance(value.get('checks'), list):
        raise ValueError('review_checks_required')
    seen = set()
    for row in value['checks']:
        if not isinstance(row, dict):
            raise ValueError('review_check_invalid')
        path = row.get('field')
        if not isinstance(path, str) or path not in payload['fields'] or path in seen:
            raise ValueError('review_field_coverage_invalid')
        seen.add(path)
    if seen != set(payload['fields']):
        raise ValueError('review_field_coverage_invalid')
    issues = []
    for row in value['checks']:
        path = row['field']
        try:
            issues.extend(review.validate_review({'checks': [row]},
                {**payload, 'fields': {path: payload['fields'][path]}}))
        except ValueError as exc:
            if str(exc) != 'review_evidence_invalid' or not all(
                isinstance(w, dict) and isinstance(w.get('ref'), str)
                and w['ref'] in payload['evidence'] and isinstance(w.get('quote'), str)
                and w['quote'].strip() for w in row['evidence']
            ):
                raise
            issues.append({'field': path, 'code': 'evidence_quote_invalid'})
    return issues


def admit(research, records, old_facts):
    """Filter projections, not stored evidence; never regenerate rejected records."""
    pool = {'record:' + r['key']: r for r in records}
    pool.update({'previous:' + digest(f): f for f in old_facts})
    if not pool:
        return records, old_facts, {'version': VERSION, 'accepted': 0, 'rejected': []}
    # Validate each provenance independently. The profile's text deduplication
    # must not discard a refreshed reference before admission has reviewed it.
    rows = {k: r for k, r in pool.items() if research.partition_public_facts([r])[0]}
    rejected = {k: 'references_unavailable' for k in pool if k not in rows}
    generation = research.pipeline.evidence.dependencies.generation(research.project_id)
    payload = payload_for(research, rows)
    binding = digest([VERSION, GUIDANCE, review.VERSION, review.SYSTEM, review.CONSISTENCY_SYSTEM,
                      review.MAX_FIELDS, review.MAX_FIELD_CHARS, review.MAX_BATCHES,
                      review.OUTPUT_TOKENS, research.binding, generation, payload])
    prefix = 'dsh_record_review_' + binding[:40]

    def guard():
        research.guard()
        if research.pipeline.evidence.dependencies.generation(research.project_id) != generation:
            raise ValueError('public inputs changed during record review')
        if payload_for(research, rows) != payload:
            raise ValueError('public inputs changed during record review')

    # An empty eligible set has no assertions to send; all raw records remain.
    batches = review.review_batches(payload) if rows else []
    checked = set()
    for index, batch in enumerate(batches):
        guard()
        key = prefix + ':' + str(index)
        saved = research.execution.read_checkpoint(key)
        if saved is None:
            reviewer = getattr(research, 'record_reviewer', None)
            if reviewer is None:
                research.hold('judgment_review_unavailable')
                raise HttpError('judgment_review_unavailable')
            research.execution.checkpoint('dsh_record_review_progress', {
                'batch': index + 1, 'total': len(batches), 'phase': batch['phase'], 'status': 'running'})
            response = reviewer(batch)
            try:
                issues = (review.validate_consistency(response, batch) if batch['phase'] == 'consistency'
                          else validate_records(response, batch))
                if batch['phase'] == 'fields':
                    for check in response['checks']:
                        if check['status'] == 'not_factual':
                            issues.append({'field': check['field'], 'code': 'not_a_public_fact'})
                        if check['status'] == 'supported' and not any(
                                e['ref'] in payload['bound_evidence'][check['field']] for e in check['evidence']):
                            issues.append({'field': check['field'], 'code': 'evidence_not_bound'})
            except (ValueError, TypeError) as exc:
                research.hold('judgment_review_invalid')
                raise HttpError('judgment_review_invalid') from exc
            saved = {'binding': binding, 'issues': issues, 'checked': list(batch['fields']), 'response': response}
            research.execution.checkpoint_chunked(key, saved)
        if saved.get('binding') != binding:
            raise ValueError('record_review_binding_mismatch')
        if batch['phase'] == 'fields':
            checked.update(saved['checked'])
        for issue in saved['issues']:
            for field in issue.get('fields', [issue['field']]):
                rejected[field] = issue['code']
    guard()
    if checked != set(rows):
        raise ValueError('record_review_coverage_invalid')
    accepted = set(rows) - set(rejected)
    receipt = {'version': VERSION, 'binding': binding, 'accepted': len(accepted),
               'accepted_keys': sorted(accepted),
               'rejected': [{'key': k, 'code': v} for k, v in sorted(rejected.items())]}
    research.execution.checkpoint_chunked('dsh_record_admission', receipt)
    research.execution.checkpoint('dsh_record_review_progress', {
        'total': len(batches), 'status': 'partial' if rejected else 'passed'})
    return ([r for r in records if 'record:' + r['key'] in accepted],
            [f for f in old_facts if 'previous:' + digest(f) in accepted], receipt)
