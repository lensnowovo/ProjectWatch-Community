"""Content revisions for UI receipts. No clock, provider, scheduling or writes."""
from __future__ import annotations

import hashlib
import json


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _revision(values):
    text = json.dumps(_canonical(values), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'count': len(values), 'revision': hashlib.sha256(text.encode('utf8')).hexdigest()}


def saved_revisions(watchpoints: list[dict], record: dict | None) -> dict:
    """Only persisted relation definitions and judgment content, not hit counters.

    A revision means saved content changed; it does not assert correctness, task
    completion, or that a company made progress. No private source text is returned.
    """
    fields = ('id', 'relation_target', 'relation_type', 'relation_aliases', 'reason',
              'text', 'status', 'parent_id', 'scope_kind')
    relations = [{key: row.get(key) for key in fields} for row in watchpoints
                 if row.get('scope_kind') == 'relation' and row.get('status') != 'retired']
    analysis = (record or {}).get('analysis') or {}
    return {'version': 1, 'relations': _revision(relations),
            'judgment': _revision([analysis] if analysis else [])}
