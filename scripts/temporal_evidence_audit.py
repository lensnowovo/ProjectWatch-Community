"""Offline public-evidence slice audit; not wired to production or paid services.

Availability is supplied by a trusted local snapshot adapter, not inferred from
publication date. Hashes detect content mismatch; they do not authenticate the
adapter, prove historical web availability, authorize exports or score meaning.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

SCHEMA = 'temporal-evidence-slice-v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError('invalid_timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('invalid_timestamp') from error
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed.astimezone(UTC)


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError('invalid_identifier')
    return value


def parts_digest(parts):
    """Canonical frozen-part digest, separate from product content_hash format."""
    if not isinstance(parts, list) or not 1 <= len(parts) <= 16:
        raise ValueError('parts_bound')
    for part in parts:
        if (not isinstance(part, dict) or set(part) != {'locator_kind', 'locator', 'text'}
                or any(not isinstance(part[key], str) for key in part)
                or not part['text'] or len(part['text']) > 100_000
                or len(part['locator']) > 1000 or len(part['locator_kind']) > 100):
            raise ValueError('parts_shape')
    return _digest(parts)


def freeze_slice(project_id, cutoff, records):
    """Keep the complete denominator, exclude late/unknown acquisition receipts.

    This tool accepts public-only inputs. Internal and commercial records must
    be withheld by the existing permission-aware caller before invoking it.
    No implication that a role/date appearing in these parts is true or current.
    """
    project_id, limit = _identifier(project_id), _timestamp(cutoff)
    if not isinstance(records, list) or len(records) > 512:
        raise ValueError('records_bound')
    selected, excluded, seen, characters = [], [], set(), 0
    for row in records:
        if not isinstance(row, dict):
            raise ValueError('record_shape')
        version = _identifier(row.get('evidence_version_id'))
        if version in seen:
            raise ValueError('duplicate_version')
        seen.add(version)
        if (row.get('project_id') != project_id or row.get('visibility') != 'public'
                or row.get('commercial') is not False):
            raise ValueError('record_scope')
        if row.get('available_basis') != 'evidence_version_created_at':
            raise ValueError('availability_basis')
        parts = row.get('parts')
        digest = parts_digest(parts)
        if digest != row.get('parts_sha256'):
            raise ValueError('parts_changed')
        characters += sum(len(p['text']) for p in parts)
        if characters > 4_000_000:
            raise ValueError('records_text_bound')
        availability = row.get('available_at')
        if availability is None or availability == '':
            excluded.append({'evidence_version_id': version, 'reason': 'availability_unknown'})
            continue
        known_at = _timestamp(availability)
        if known_at > limit:
            excluded.append({'evidence_version_id': version, 'reason': 'not_yet_available'})
            continue
        selected.append({'project_id': project_id, 'evidence_version_id': version,
            'available_at': known_at.isoformat(), 'available_basis': 'evidence_version_created_at',
            'visibility': 'public', 'commercial': False, 'parts': deepcopy(parts), 'parts_sha256': digest})
    pack = {'schema_version': SCHEMA, 'project_id': project_id, 'cutoff': limit.isoformat(),
            'input_count': len(records), 'sources': selected, 'excluded': excluded,
            'ready': bool(selected), 'availability_basis': 'local_saved_version',
            'historical_web_availability': 'not_proven'}
    return {**pack, 'slice_sha256': _digest(pack)}


def audit_refs(pack, refs):
    """Resolve only this frozen project/version/slice; never score fact truth."""
    if not isinstance(pack, dict) or pack.get('schema_version') != SCHEMA:
        raise ValueError('pack_shape')
    checked = freeze_slice(pack.get('project_id'), pack.get('cutoff'), pack.get('sources'))
    if checked['excluded'] or checked['sources'] != pack['sources']:
        raise ValueError('pack_changed')
    if pack.get('slice_sha256') != _digest({k: v for k, v in pack.items() if k != 'slice_sha256'}):
        raise ValueError('slice_changed')
    if not isinstance(refs, list) or len(refs) > 4096:
        raise ValueError('refs_bound')
    versions = {r['evidence_version_id']: r for r in checked['sources']}
    rejected, resolved = [], 0
    for index, ref in enumerate(refs, 1):
        code = 'invalid_reference'
        row = versions.get(ref.get('evidence_version_id')) if isinstance(ref, dict) and isinstance(
            ref.get('evidence_version_id'), str) else None
        if isinstance(ref, dict) and row is None:
            code = 'outside_slice'
        if row is not None:
            part, start, end = (ref.get(key) for key in ('part_index', 'start', 'end'))
            quote = ref.get('quote')
            if (all(type(value) is int for value in (part, start, end))
                    and 0 <= part < len(row['parts'])
                    and 0 <= start < end <= len(row['parts'][part]['text'])
                    and isinstance(quote, str) and bool(quote)
                    and row['parts'][part]['text'][start:end] == quote):
                resolved += 1
                continue
        rejected.append({'id': f'R{index:02d}', 'reason': code})
    status = ('no_references' if not refs else 'partial' if resolved and rejected else
              'rejected' if rejected else 'resolved')
    return {'status': status, 'input_count': len(refs), 'resolved_count': resolved,
            'rejected': rejected, 'semantic_verification': 'not_proven',
            'business_change_detection': 'not_measured'}
