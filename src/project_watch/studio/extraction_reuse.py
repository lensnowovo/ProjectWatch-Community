"""Reuse the model's answer to an extraction batch that is unchanged since an earlier round.

Sources are batched in the order they were first stored, and numbered within each batch,
so a batch of old sources reads exactly as it did last round. Its stored answer is reused
instead of asking the model again. The caller still checks every reused fact against the
current source text, exactly as it checks a fresh answer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import closing
from datetime import UTC, datetime, timedelta

LAYOUT = 2
MAX_PARTS = 12
MAX_CHARS = 48000
KEEP_DAYS = 45
SCHEMA = ('CREATE TABLE IF NOT EXISTS studio_extraction_reuse (project_id TEXT NOT NULL, key TEXT NOT NULL, '
          'response TEXT NOT NULL, created_at TEXT NOT NULL, used_at TEXT NOT NULL, PRIMARY KEY(project_id, key))')
# Fields of an answer that name a source of the batch: "B3", or a passage of it, "B3:P2".
MARKER_FIELDS = ('source', 'ownership_passage')
_MARKER = re.compile(r'^(B[1-9]\d*)(:P[1-9]\d*)?$')


def _now():
    return datetime.now(UTC).isoformat()


def _stored(store):
    """Stand-in stores without a database keep the given order and reuse nothing."""
    return callable(getattr(store, '_connect', None))


def batches(store, project_id, parts):
    """Oldest stored evidence first, so new sources only fill the last batch or add new ones."""
    seq = {}
    if _stored(store):
        with closing(store._connect()) as db:
            seq = dict(db.execute('SELECT id, seq FROM studio_evidence_versions WHERE project_id=?', (project_id,)))
    position = {id(p): n for n, p in enumerate(parts)}
    ordered = sorted(parts, key=lambda p: (seq.get(p.get('evidence_version_id'), float('inf')),
                                           p.get('part_index', 0), position[id(p)]))
    result, batch, size = [], [], 0
    for part in ordered:
        if batch and (len(batch) >= MAX_PARTS or size + len(part['text']) > MAX_CHARS):
            result.append(batch)
            batch, size = [], 0
        batch.append(part)
        size += len(part['text'])
    if batch:
        result.append(batch)
    return result


def localize(parts):
    """The batch numbered B1..Bn, and the way back to each part's own marker."""
    if not all(isinstance(p.get('marker'), str) for p in parts):
        return parts, {}  # nothing to renumber
    local = [{**p, 'marker': f'B{n}'} for n, p in enumerate(parts, 1)]
    return local, {f'B{n}': p['marker'] for n, p in enumerate(parts, 1)}


def restore(response, back):
    """The answer with batch markers turned back into the parts' own markers."""
    result = copy.deepcopy(response)

    def walk(node):
        if isinstance(node, dict):
            for name, value in node.items():
                if name in MARKER_FIELDS and isinstance(value, str):
                    match = _MARKER.match(value)
                    if match and match.group(1) in back:
                        node[name] = back[match.group(1)] + (match.group(2) or '')
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(result)
    return result


def _stable(body):
    """Object versions change every round without changing what the model is asked to read."""
    if isinstance(body, dict):
        return {k: _stable(v) for k, v in body.items() if not (k == 'version' and isinstance(v, int))}
    if isinstance(body, list):
        return [_stable(v) for v in body]
    return body


def key(prefix, system, binding, body):
    return hashlib.sha256(json.dumps({'layout': LAYOUT, 'prefix': prefix, 'binding': binding,
                                      'system': hashlib.sha256(system.encode()).hexdigest(), 'body': _stable(body)},
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def lookup(store, project_id, reuse_key):
    if not _stored(store):
        return None
    with closing(store._connect()) as db, db:
        db.execute(SCHEMA)
        row = db.execute('SELECT response FROM studio_extraction_reuse WHERE project_id=? AND key=?',
                         (project_id, reuse_key)).fetchone()
        if row is None:
            return None
        db.execute('UPDATE studio_extraction_reuse SET used_at=? WHERE project_id=? AND key=?',
                   (_now(), project_id, reuse_key))
    return json.loads(row[0])


def remember(store, project_id, reuse_key, response):
    if not _stored(store):
        return
    stamp = _now()
    with closing(store._connect()) as db, db:
        db.execute(SCHEMA)
        db.execute('INSERT INTO studio_extraction_reuse VALUES (?,?,?,?,?) ON CONFLICT(project_id, key) '
                   'DO UPDATE SET response=excluded.response, used_at=excluded.used_at',
                   (project_id, reuse_key, json.dumps(response, ensure_ascii=False), stamp, stamp))
        db.execute('DELETE FROM studio_extraction_reuse WHERE project_id=? AND used_at<?',
                   (project_id, (datetime.now(UTC) - timedelta(days=KEEP_DAYS)).isoformat()))
