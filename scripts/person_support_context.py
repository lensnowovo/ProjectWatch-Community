"""Offline experimental person contract; NOT wired to runtime or model calls.

Typed declarations and exact source/date presence are structural checks, not
entailment. Callers must keep rejected raw output and never silently repair it.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, date, datetime

from project_watch.studio.public_profile import _supported_occurrence_date

SCHEMA = 'person-support-context-v1'
RELATIONS = {'role', 'appointment', 'departure', 'listed', 'first_role', 'precedence'}


def _fail(code):
    raise ValueError(code)


def _string(value, maximum, code):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(code)
    return value


def _publication(value, today):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return None
    return parsed.isoformat() if parsed <= today else None


def freeze_context(subject: str, sources: list[dict], *, today: date | None = None) -> dict:
    """Whitelist fields, preserve text, fail on scope/bounds; never read providers."""
    subject = _string(subject, 200, 'context_bound')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 24:
        _fail('context_bound')
    today = today or datetime.now(UTC).date()
    frozen, seen = [], set()
    for source in sources:
        if (not isinstance(source, dict) or source.get('visibility') != 'public'
                or source.get('commercial') is not False):
            _fail('context_scope')
        marker = source.get('ref')
        if not isinstance(marker, str) or not re.fullmatch(r'P[1-9][0-9]?', marker) or marker in seen:
            _fail('context_refs')
        seen.add(marker)
        frozen.append({'ref': marker,
            'title': {'text': _string(source.get('title'), 400, 'context_bound'), 'basis': 'source_title'},
            'body': {'text': _string(source.get('body'), 4000, 'context_bound'), 'basis': 'source_excerpt'},
            'publication': {'date': _publication(source.get('published_at'), today),
                            'basis': 'source_publication', 'is_occurrence_date': False},
            'visibility': 'public', 'commercial': False})
    return {'schema_version': SCHEMA, 'subject': subject, 'sources': frozen}


def _check_context(context):
    if not isinstance(context, dict) or context.get('schema_version') != SCHEMA:
        _fail('context_shape')
    if set(context) != {'schema_version', 'subject', 'sources'}:
        _fail('context_shape')
    _string(context.get('subject'), 200, 'context_shape')
    sources = context.get('sources')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 24:
        _fail('context_bound')
    seen = set()
    for source in sources:
        if (not isinstance(source, dict) or source.get('visibility') != 'public'
                or source.get('commercial') is not False):
            _fail('context_scope')
        if set(source) != {'ref', 'title', 'body', 'publication', 'visibility', 'commercial'}:
            _fail('context_shape')
        marker = source.get('ref')
        if not isinstance(marker, str) or not re.fullmatch(r'P[1-9][0-9]?', marker) or marker in seen:
            _fail('context_refs')
        seen.add(marker)
        for field, basis, maximum in [('title', 'source_title', 400), ('body', 'source_excerpt', 4000)]:
            part = source.get(field)
            if not isinstance(part, dict) or part.get('basis') != basis or set(part) != {'text', 'basis'}:
                _fail('context_shape')
            _string(part.get('text'), maximum, 'context_bound')
        publication = source.get('publication')
        if (not isinstance(publication, dict) or publication.get('basis') != 'source_publication'
                or publication.get('is_occurrence_date') is not False
                or set(publication) != {'date', 'basis', 'is_occurrence_date'}):
            _fail('context_shape')
        value = publication.get('date')
        if value is not None and (_publication(value, datetime.now(UTC).date()) != value):
            _fail('context_shape')


def context_digest(context: dict) -> str:
    _check_context(context)
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def extraction_payload(context: dict) -> dict:
    return {'task': 'extract_person_relations', 'context_sha256': context_digest(context),
            'context': deepcopy(context)}


def _raw_facts(proposal):
    if not isinstance(proposal, dict) or not isinstance(proposal.get('facts'), list):
        _fail('person_shape')
    facts = proposal['facts']
    if len(facts) > 30 or any(not isinstance(f, dict) for f in facts):
        _fail('person_shape')
    return facts


def review_payload(context: dict, proposal: dict, expected_context_sha256: str) -> dict:
    """Freeze raw extraction without claiming it is correct or filling answers."""
    digest = context_digest(context)
    if digest != expected_context_sha256:
        _fail('context_changed')
    return {'task': 'review_person_relations', 'context_sha256': digest, 'context': deepcopy(context),
            'claims': [{'id': f'F{i:02d}', 'fact': deepcopy(fact)}
                       for i, fact in enumerate(_raw_facts(proposal), 1)]}


def validate_person_proposal(context: dict, proposal: dict) -> dict:
    """Check declared scope/one relation/ref/time, NOT the prose's actual meaning."""
    context_digest(context)
    sources = {s['ref']: s for s in context['sources']}
    facts = []
    for index, raw in enumerate(_raw_facts(proposal), 1):
        if raw.get('subject') != context['subject']:
            _fail('person_subject')
        relation = raw.get('relation')
        if not isinstance(relation, str) or relation not in RELATIONS:
            _fail('person_relation')
        if (raw.get('kind') != 'person' or raw.get('polarity') not in ('positive', 'negative')
                or raw.get('time_scope') not in ('historical', 'as_reported')):
            _fail('person_shape')
        for name, maximum in [('label', 100), ('value', 200), ('text', 600)]:
            _string(raw.get(name), maximum, 'person_shape')
        refs = raw.get('refs')
        if not isinstance(refs, list) or not 1 <= len(refs) <= 3:
            _fail('person_refs')
        cited = []
        for ref in refs:
            if (not isinstance(ref, dict) or not isinstance(ref.get('ref'), str)
                    or ref['ref'] not in sources or ref.get('field', 'body') != 'body'):
                _fail('person_refs')
            source = sources[ref['ref']]
            quote = ref.get('quote')
            if (not isinstance(quote, str) or len(quote) > 1000 or len(re.sub(r'\s+', '', quote)) < 6
                    or quote not in source['body']['text']):
                _fail('person_refs')
            cited.append(source)
        occurred, reported = raw.get('date'), raw.get('reported_on')
        if (not isinstance(occurred, str) or len(occurred) > 40
                or not isinstance(reported, str) or len(reported) > 10):
            _fail('person_shape')
        # The existing date guard checks presence/precision, never event semantics.
        if occurred and not _supported_occurrence_date(occurred, [s['body']['text'] for s in cited]):
            _fail('person_occurrence')
        if reported and reported not in {s['publication']['date'] for s in cited}:
            _fail('person_reporting')
        fact = {key: deepcopy(raw[key]) for key in ('subject', 'kind', 'label', 'relation', 'value',
                'polarity', 'time_scope', 'text', 'date', 'reported_on', 'refs')}
        facts.append({'id': f'F{index:02d}', **fact})
    return {'facts': facts, 'context_sha256': context_digest(context), 'semantic_verification': 'not_proven'}


def validate_review(context: dict, original: dict, reviewed: dict) -> dict:
    digest = context_digest(context)
    if not isinstance(reviewed, dict) or reviewed.get('context_sha256') != digest:
        _fail('context_changed')
    expected = {f'F{i:02d}' for i in range(1, len(_raw_facts(original)) + 1)}
    judgments = reviewed.get('judgments')
    if not isinstance(judgments, list) or any(not isinstance(j, dict) for j in judgments):
        _fail('review_shape')
    ids = [j.get('id') for j in judgments]
    if any(not isinstance(i, str) for i in ids) or len(ids) != len(expected) or set(ids) != expected:
        _fail('review_coverage')
    retained, records = [], []
    for judgment in judgments:
        status = judgment.get('status')
        if status not in ('supported', 'contradicted', 'insufficient'):
            _fail('review_shape')
        _string(judgment.get('reason'), 1000, 'review_shape')
        output = validate_person_proposal(context, {'facts': judgment.get('facts')})
        if status == 'supported' and not output['facts']:
            _fail('review_shape')
        for index, fact in enumerate(output['facts'], 1):
            retained.append({**fact, 'id': f"{judgment['id']}-{index:02d}", 'parent_id': judgment['id'],
                             'model_review_status': status})
        records.append(deepcopy(judgment))
    if len(retained) > 30:
        _fail('review_shape')
    return {'facts': retained, 'judgments': records, 'context_sha256': digest,
            'semantic_verification': 'not_proven'}


def _payload_digest(payload):
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError):
        _fail('request_binding')
    return hashlib.sha256(encoded).hexdigest()


def bind_review_request(context: dict, original: dict, sent_payload: dict, *, request_id: str) -> dict:
    """Caller-owned receipt, NOT model output or a cryptographic authentication token.

    Callers must bind the actual dispatched payload and correlate the response in
    their trusted transport/audit path. No provider calls or automatic retries here.
    """
    _string(request_id, 200, 'request_binding')
    digest = context_digest(context)
    expected = review_payload(context, original, digest)
    if sent_payload != expected or _payload_digest(sent_payload) != _payload_digest(expected):
        _fail('request_binding')
    return {'request_id': request_id, 'context_sha256': digest,
            'payload_sha256': _payload_digest(sent_payload)}


def _partial_rows(proposal):
    if not isinstance(proposal, dict) or not isinstance(proposal.get('facts'), list):
        _fail('person_shape')
    if len(proposal['facts']) > 30:
        _fail('person_shape')
    return proposal['facts']


def _partial_status(facts, rejections):
    if rejections:
        return 'partial' if facts else 'rejected'
    return 'complete' if facts else 'empty'


def validate_person_proposal_partial(context: dict, proposal: dict) -> dict:
    """Isolate invalid rows without changing them or renumbering surviving rows."""
    digest = context_digest(context)
    raw_rows = _partial_rows(proposal)
    retained, rejected = [], []
    for index, raw in enumerate(raw_rows, 1):
        identifier = f'F{index:02d}'
        try:
            fact = validate_person_proposal(context, {'facts': [raw]})['facts'][0]
        except ValueError as error:
            rejected.append({'id': identifier, 'code': str(error), 'raw': deepcopy(raw)})
        else:
            retained.append({**fact, 'id': identifier})
    return {'facts': retained, 'raw_facts': deepcopy(raw_rows), 'rejections': rejected,
            'status': _partial_status(retained, rejected),
            'coverage_status': 'provided' if raw_rows else 'needs_extraction',
            'context_sha256': digest, 'semantic_verification': 'not_proven'}


def validate_review_partial(context: dict, original: dict, reviewed: dict, *,
                            binding: dict, response_request_id: str) -> dict:
    """Use trusted request correlation, strict ID coverage and granular row checks.

    Model context echoes are diagnostics only. A valid local receipt never excuses
    missing judgments, and an empty claim-only review cannot recover omitted facts.
    """
    digest = context_digest(context)
    expected_payload = review_payload(context, original, digest)
    if (not isinstance(binding, dict)
            or set(binding) != {'request_id', 'context_sha256', 'payload_sha256'}):
        _fail('request_binding')
    _string(response_request_id, 200, 'request_binding')
    if binding != {'request_id': response_request_id, 'context_sha256': digest,
                   'payload_sha256': _payload_digest(expected_payload)}:
        _fail('request_binding')
    if not isinstance(reviewed, dict):
        _fail('review_shape')
    expected_ids = {claim['id'] for claim in expected_payload['claims']}
    judgments = reviewed.get('judgments')
    if not isinstance(judgments, list) or any(not isinstance(j, dict) for j in judgments):
        _fail('review_shape')
    ids = [j.get('id') for j in judgments]
    if (any(not isinstance(i, str) for i in ids)
            or len(ids) != len(expected_ids) or set(ids) != expected_ids):
        _fail('review_coverage')
    diagnostics = []
    if 'context_sha256' not in reviewed:
        diagnostics.append('model_context_echo_missing')
    elif reviewed['context_sha256'] != digest:
        diagnostics.append('model_context_echo_mismatch')
    retained, rejected = [], []
    for judgment in judgments:
        parent, status = judgment['id'], judgment.get('status')
        try:
            if status not in ('supported', 'contradicted', 'insufficient'):
                _fail('review_shape')
            _string(judgment.get('reason'), 1000, 'review_shape')
            output = validate_person_proposal_partial(context, {'facts': judgment.get('facts')})
            if status == 'supported' and not output['raw_facts']:
                _fail('review_shape')
        except ValueError as error:
            rejected.append({'id': parent, 'code': str(error), 'raw': deepcopy(judgment)})
            continue
        for fact in output['facts']:
            # Use the original row index, not its index after bad siblings were removed.
            suffix = fact['id'][1:]
            retained.append({**fact, 'id': f'{parent}-{suffix}', 'parent_id': parent,
                             'model_review_status': status})
        for error in output['rejections']:
            rejected.append({**error, 'id': f"{parent}-{error['id'][1:]}", 'parent_id': parent})
    if len(retained) > 30:
        _fail('review_shape')
    return {'facts': retained, 'judgments': deepcopy(judgments), 'rejections': rejected,
            'diagnostics': diagnostics, 'binding_status': 'matched',
            'status': _partial_status(retained, rejected),
            'coverage_status': 'reviewed' if expected_ids else 'needs_extraction',
            'context_sha256': digest, 'semantic_verification': 'not_proven'}
