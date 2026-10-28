"""Derived pagination positions; never request recovery or success authority."""
import json
from contextlib import closing

from project_watch.studio.collection_history import moment, query_key

SOURCES = frozenset({'openalex_topic', 'openalex_author', 'openalex_affiliation',
                     'clinical_sponsor', 'clinical_term', 'cninfo'})
SCHEMA = '''CREATE TABLE IF NOT EXISTS studio_collection_continuations (
    project_id TEXT NOT NULL REFERENCES projects(id), query_key TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES studio_jobs(id), unit_id TEXT NOT NULL,
    created_at TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(project_id,query_key,job_id,unit_id))'''


def read(store, project_id):
    with closing(store._connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_collection_continuations'").fetchone():
            return {}
        result = {}
        for row in db.execute('SELECT query_key,data FROM studio_collection_continuations WHERE project_id=? '
                              'ORDER BY created_at DESC,rowid DESC', (project_id,)):
            result.setdefault(row['query_key'], json.loads(row['data']))
        return result


def select(unit, previous):
    """Only previously saved pages in the same query and evidence scope carry."""
    value = previous.get('continuation')
    if (unit['source'] not in SOURCES or unit['disposition'] != 'pending' or not value
            or previous.get('evidence_ids') != unit['applicability_evidence_ids']):
        return None
    return value


def record(execution, plan, receipts):
    if not plan.get('history_binding'):
        return
    units = {u['id']: u for u in plan['units']}
    entries = []
    for receipt in receipts:
        unit = units[receipt['unit_id']]
        if unit['source'] not in SOURCES or receipt['outcome'] == 'running':
            continue
        key = query_key(plan['project_id'], plan['history_binding'], unit)
        if key != unit.get('history_key'):
            raise ValueError('continuation query identity changed')
        continuation = None
        prior = unit.get('continuation') or {}
        # Only saved, known-success pages or an unissued budget remainder may
        # continue. Source errors/unknowns invalidate the previous position;
        # reopening old receipts cannot resurrect it over a newer round.
        if (receipt['outcome'] in ('success', 'budget_deferred') and receipt.get('truncated') is True
                and receipt.get('ingestion_complete') is True and receipt.get('pages_completed', 0) > 0
                and receipt.get('next_cursor') is not None):
            continuation = dict(cursor=receipt['next_cursor'], visited=receipt.get('visited_cursors', []),
                window_start=unit['window_start'], window_end=unit['window_end'],
                origin_at=prior.get('origin_at', plan['created_at']), full_scan=unit['full_scan'],
                previous_job_id=plan['round_id'], previous_unit_id=unit['id'],
                pages_completed=prior.get('pages_completed', 0) + receipt['pages_completed'])
        elif receipt['outcome'] == 'budget_deferred' and 'result_count' not in receipt and prior:
            continuation = prior  # Zero requests this round; no new progress.
        entries.append((plan['project_id'], key, plan['round_id'], unit['id'], moment(plan['created_at']).isoformat(),
                        json.dumps(dict(continuation=continuation, evidence_ids=unit['applicability_evidence_ids']),
                                   ensure_ascii=False)))
    if not entries:
        return
    with execution.store.write_scope(execution._guard), closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        db.execute(SCHEMA)
        db.executemany('INSERT OR IGNORE INTO studio_collection_continuations VALUES (?,?,?,?,?,?)', entries)
