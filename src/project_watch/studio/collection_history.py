"""Per-query successful windows, independent of job/manifest identities.

This index cannot authorize recovery or dispatch. Immutable collection receipts
remain authoritative; an index write interrupted after receipt persistence is
replayed when that same ledger opens again.
"""
import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

from project_watch.studio.collection_plan import make_plan, reconcile

SCHEMA = '''CREATE TABLE IF NOT EXISTS studio_collection_successes (
    project_id TEXT NOT NULL REFERENCES projects(id), query_key TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES studio_jobs(id), unit_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL, window_end TEXT, full_scan INTEGER NOT NULL,
    PRIMARY KEY(project_id,query_key,job_id,unit_id))'''


def moment(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('collection history requires timezone')
    return result.astimezone(UTC)


def query_key(project_id, binding, unit):
    # Credentials/configuration are represented only by the existing binding
    # digest. A changed subject, query, target or adapter version gets a baseline.
    value = dict(project_id=project_id, binding=binding,
                 **{key: unit[key] for key in ('source', 'target_id', 'category', 'query', 'query_version')})
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read(store, project_id):
    with closing(store._connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_collection_successes'").fetchone():
            return {}
        return {r['query_key']: dict(r) for r in db.execute('''SELECT query_key,
            MAX(fetched_at) last_success_at, MAX(window_end) window_end,
            MAX(CASE WHEN full_scan=1 THEN fetched_at END) baseline_at
            FROM studio_collection_successes WHERE project_id=? GROUP BY query_key''', (project_id,))}


def schedule(plan, binding, history, *, overlap_days=7, baseline_days=30, continuations=None):
    """Freeze windows and fair order without changing the user's source cadence.

    Only adapters whose prior receipt explicitly applied a window can narrow it.
    Publication-date APIs use an overlap plus periodic original-scope scans;
    this is bounded lookback, not a guarantee of exhaustive update detection.
    """
    if type(overlap_days) is not int or not 0 <= overlap_days <= 90:
        raise ValueError('invalid collection overlap')
    if type(baseline_days) is not int or not 1 <= baseline_days <= 365:
        raise ValueError('invalid collection baseline interval')
    at = moment(plan['created_at'])
    from project_watch.studio.collection_continuation import select

    candidates, metadata = [], {}
    for unit in plan['units']:
        key = query_key(plan['project_id'], binding, unit)
        previous = history.get(key, {})
        full_scan = True
        start = unit['window_start']
        if (start is not None and previous.get('window_end') and previous.get('baseline_at')
                and moment(previous['baseline_at']) + timedelta(days=baseline_days) > at):
            # Keep gaps even if they exceed the usual first-round lookback.
            start = min(at, moment(previous['window_end']) - timedelta(days=overlap_days)).isoformat()
            full_scan = False
        continuation = select(unit, (continuations or {}).get(key, {}))
        candidate = {**unit, 'window_start': start}
        if continuation:
            candidate.update(window_start=continuation['window_start'], window_end=continuation['window_end'])
            full_scan = continuation['full_scan']
        candidates.append(candidate)
        metadata[(unit['source'], unit['target_id'], unit['category'], unit['query'], unit['query_version'])] = dict(
            history_key=key, last_success_at=previous.get('last_success_at'), full_scan=full_scan,
            **({'continuation': continuation} if continuation else {}))
    result = make_plan(round_id=plan['round_id'], project_id=plan['project_id'], binding=plan['binding'],
                       mode=plan['mode'], candidates=candidates, at=plan['created_at'])
    result.update(history_binding=binding, history_policy=dict(overlap_days=overlap_days, baseline_days=baseline_days))
    for unit in result['units']:
        unit.update(metadata[tuple(unit[k] for k in ('source', 'target_id', 'category', 'query', 'query_version'))])
    # Unsuccessful/never-run queries go first. Tier only breaks equal waiting
    # times: repeated successful company searches cannot starve other units.
    result['units'].sort(key=lambda u: (u['disposition'] != 'pending',
                                      moment(u['last_success_at']).timestamp() if u['last_success_at'] else float('-inf'),
                                      u['priority'], u['id']))
    return result


def record(execution, plan, receipts):
    if not plan.get('history_binding'):
        return  # Older ledgers keep their original execution semantics.
    rows = {r['unit_id']: r for r in receipts}
    successful = {r['unit_id'] for r in reconcile(plan, receipts)['rows'] if r['outcome'] == 'success'}
    entries = []
    for unit in plan['units']:
        if unit['id'] not in successful:
            continue
        key = query_key(plan['project_id'], plan['history_binding'], unit)
        if unit.get('history_key') != key:
            raise ValueError('collection history identity changed')
        receipt = rows[unit['id']]
        fetched_at = moment((unit.get('continuation') or {}).get('origin_at', plan['created_at']))
        cached = receipt.get('cache_provenance', [])
        # An undated cache can be reused as evidence but cannot certify new
        # freshness. Never replace its original fetch time with this job's time.
        if any(not row.get('completed_at') for row in cached):
            continue
        if cached:
            fetched_at = min(fetched_at, *(moment(row['completed_at']) for row in cached))
        end = None
        if receipt.get('source_scope', {}).get('window_applied') is True:
            end = min(fetched_at, moment(unit['window_end'])).isoformat()
        entries.append((plan['project_id'], key, plan['round_id'], unit['id'],
                        fetched_at.isoformat(), end, int(unit['full_scan'])))
    if not entries:
        return
    with execution.store.write_scope(execution._guard), closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        db.execute(SCHEMA)
        db.executemany('INSERT OR IGNORE INTO studio_collection_successes VALUES (?,?,?,?,?,?,?)', entries)
        _advance_relations(db, plan)
        from project_watch.studio.exploration import Explorations

        Explorations.record_collection(db, plan, successful,
            cached_units={unit_id for unit_id, receipt in rows.items() if receipt.get('cache_provenance')})


def _advance_relations(db, plan):
    """Project successful query times onto the same-version public relation.

    Recompute from durable per-query successes instead of applying callbacks:
    reopening a ledger cannot count the same query twice or move time to now.
    Question lifecycle transitions remain owned by the exploration engine.
    """
    from project_watch.studio.collection_scope import watch_direction

    watch_ids = {u['target_id'][6:] for u in plan['units']
                 if u['source'] == 'search' and u['target_id'].startswith('watch:')}
    for wid in watch_ids:
        row = db.execute('SELECT * FROM studio_watchpoints WHERE id=? AND project_id=?',
                         (wid, plan['project_id'])).fetchone()
        if (not row or row['visibility'] != 'public' or row['status'] == 'retired'
                or row['scope_kind'] != 'relation'):
            continue
        watch = dict(row)
        version = 'watch:' + str(watch['lifecycle_version'])
        if not any(u['target_id'] == 'watch:' + wid and u['query_version'] == version for u in plan['units']):
            continue
        queries = set(json.loads(watch['queries']))
        if not queries:
            continue
        times = {}
        for query in queries:
            key = query_key(plan['project_id'], plan['history_binding'], dict(
                source='search', target_id='watch:' + wid, category=watch_direction(watch),
                query=query, query_version=version))
            stamp = db.execute('SELECT MAX(fetched_at) FROM studio_collection_successes '
                               'WHERE project_id=? AND query_key=?', (plan['project_id'], key)).fetchone()[0]
            if stamp:
                times[query] = moment(stamp)
        if not times:
            continue
        last = moment(watch['last_run_at']) if watch['last_run_at'] else None
        candidate = min(times.values()) if times.keys() == queries else None
        advance = candidate is not None and (last is None or candidate > last)
        completed = [] if advance else sorted(q for q, stamp in times.items() if last is None or stamp > last)
        attempt = max(times.values())
        if watch['last_attempt_at']:
            attempt = max(attempt, moment(watch['last_attempt_at']))
        db.execute('UPDATE studio_watchpoints SET last_run_at=?, last_attempt_at=?, completed_queries=? WHERE id=?',
                   (candidate.isoformat() if advance else watch['last_run_at'], attempt.isoformat(),
                    json.dumps(completed, ensure_ascii=False), wid))
