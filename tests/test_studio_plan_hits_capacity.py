"""Synthetic SQL capacity regression: sparse watch hits must not scan every item."""
import sqlite3

import pytest

from project_watch.studio.dependencies import SCHEMA as DEPENDENCIES_SCHEMA
from project_watch.studio.evidence_store import SCHEMA as EVIDENCE_SCHEMA
from project_watch.studio.monitor import SCHEMA as MONITOR_SCHEMA
from project_watch.studio.monitoring_plan import MonitoringPlan
from project_watch.studio.store import SCHEMA as STORE_SCHEMA


@pytest.fixture
def db():
    with sqlite3.connect(':memory:') as connection:
        connection.row_factory = sqlite3.Row
        # Use production definitions/indexes. Foreign keys are disabled only for
        # this query fixture, which deliberately omits unrelated parent records.
        connection.executescript(STORE_SCHEMA + MONITOR_SCHEMA + EVIDENCE_SCHEMA + DEPENDENCIES_SCHEMA)
        connection.execute('ALTER TABLE studio_items ADD COLUMN event_id TEXT')
        connection.executemany(
            'INSERT INTO studio_items(id,project_id,key,category,title,collected_at) VALUES (?,?,?,?,?,?)',
            [(f'noise-{n}', 'other-project', f'noise-{n}', 'company', 'Synthetic unrelated',
              '2026-10-23T00:00:00Z') for n in range(10000)],
        )
        yield connection


def _item(db, key, date='2026-10-20', relevant=1, event=None):
    db.execute('INSERT INTO studio_items(id,project_id,key,category,title,collected_at,published_at,relevant,event_id) '
               'VALUES (?,?,?,?,?,?,?,?,?)', (key, 'project', key, 'company', 'Synthetic '+key,
                                              '2026-10-23T00:00:00Z', date, relevant, event))
    db.execute('INSERT INTO studio_watch_hits(watch_id,item_id,created_at) VALUES (?,?,?)', ('watch', key, '2026-10-23'))


@pytest.mark.parametrize('sparse', [False, True])
def test_hit_read_work_is_bounded_by_watch_matches_not_global_items(db, sparse):
    if sparse:
        _item(db, 'match')
    steps = 0

    def budget():
        nonlocal steps
        steps += 1000
        return int(steps > 10000)

    # Deterministic SQLite VM work budget, not a machine-speed assertion.
    db.set_progress_handler(budget, 1000)
    try:
        rows = MonitoringPlan._hits(None, db, 'watch')
    finally:
        db.set_progress_handler(None, 0)
    assert [row['id'] for row in rows] == (['match'] if sparse else [])


def test_hit_read_keeps_order_limit_relevance_and_lifecycle_filters(db):
    _item(db, 'older')
    _item(db, 'newer', '2026-10-21')
    _item(db, 'fallback', None)
    _item(db, 'irrelevant', relevant=0)
    _item(db, 'dismissed', event='dismissed-event')
    _item(db, 'withdrawn')
    db.execute('INSERT INTO studio_events(id,project_id,title,category,status,created_at,updated_at) '
               'VALUES (?,?,?,?,?,?,?)', ('dismissed-event', 'project', 'Synthetic', 'company',
                                         'dismissed', '2026-10-23', '2026-10-23'))
    db.execute('INSERT INTO studio_item_evidence VALUES (?,?,?,?,?,?)',
               ('withdrawn', 'project', 'version', 'public', 'full', '2026-10-23'))
    db.execute('INSERT INTO studio_evidence_lifecycle VALUES (?,?,?,?,?)',
               ('project', 'version', 'withdrawn', 'Synthetic test', 'human'))
    # A withdrawal for another project must not hide this project's item.
    db.execute('INSERT INTO studio_item_evidence VALUES (?,?,?,?,?,?)',
               ('newer', 'project', 'other-version', 'public', 'full', '2026-10-23'))
    db.execute('INSERT INTO studio_evidence_lifecycle VALUES (?,?,?,?,?)',
               ('other-project', 'other-version', 'withdrawn', 'Synthetic test', 'human'))
    rows = MonitoringPlan._hits(None, db, 'watch', limit=2)
    assert [row['id'] for row in rows] == ['fallback', 'newer']
    assert [row['date'] for row in rows] == ['2026-10-23', '2026-10-21']
    assert [row['id'] for row in MonitoringPlan._hits(None, db, 'watch', limit=30)] == ['fallback', 'newer', 'older']
    assert MonitoringPlan._hits(None, db, 'other-watch') == []
