"""Storage migration is independent of legacy monitoring and preserves its history."""
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest


def test_repository_reopens_without_legacy_monitor_methods(tmp_path):
    import project_watch

    program = '''
import importlib.abc
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
blocked = {'project_watch.external_focus', 'project_watch.monitor_briefing',
           'project_watch.external_attention', 'project_watch.external_context',
           'project_watch.metric_series'}
class RejectMethods(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('storage attempted legacy method import: ' + fullname)
sys.meta_path.insert(0, RejectMethods())
from project_watch.repository import ProjectRepository
from project_watch.external_scopes import ExternalFocusStore
from project_watch.monitoring import MonitoringRunner, ManualSnapshotAdapter, FeedItem
from datetime import UTC, datetime
repo = ProjectRepository(Path(sys.argv[2]) / 'synthetic.sqlite3')
project = repo.create_project('Synthetic storage boundary')
scope = ExternalFocusStore(repo).create(project.id, category='company', term=project.name, provider='gdelt')
assert not scope.enabled
assert ExternalFocusStore(repo).get(scope.id)['term'] == project.name
runner = MonitoringRunner(repo, {'rss': ManualSnapshotAdapter({scope.target: (
    FeedItem('synthetic', project.name + ' update', 'https://synthetic.example/news', '', datetime.now(UTC)),)})})
assert runner.run(scope.id, idempotency_key='paused').status == 'not_run'
repo.set_monitoring_plan_enabled(scope.id, True)
assert runner.run(scope.id, idempotency_key='active').new_count == 1
assert ProjectRepository(repo.db_path).get_project(project.id).name == project.name
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, '-I', '-c', program,
                             str(Path(project_watch.__file__).parent.parent), str(tmp_path)],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_legacy_schema_alias_is_same_function():
    from project_watch.external_focus import ensure_external_focus_schema as old
    from project_watch.external_focus_schema import ensure_external_focus_schema

    assert old is ensure_external_focus_schema
    from project_watch import external_focus, external_scopes
    assert external_focus.ExternalFocusStore is external_scopes.ExternalFocusStore
    assert external_focus.public_query is external_scopes.public_query
    assert external_focus.CATEGORIES is external_scopes.CATEGORIES


def legacy_database(db, *, category='company'):
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE monitoring_plans(id TEXT PRIMARY KEY)')
    db.execute("INSERT INTO monitoring_plans VALUES ('plan')")
    db.execute("""CREATE TABLE external_focus(plan_id TEXT PRIMARY KEY REFERENCES monitoring_plans(id),
        category TEXT, term TEXT, provider TEXT CHECK(provider IN ('gdelt','brave')),
        consent_version TEXT, created_at TEXT, actor_ref TEXT)""")
    row = ('plan', category, 'Synthetic history', 'gdelt', 'public-focus-v1', '2026-01-01', 'local_user')
    db.execute('INSERT INTO external_focus VALUES(?,?,?,?,?,?,?)', row)
    for operation in ('UPDATE', 'DELETE'):
        db.execute(f"""CREATE TRIGGER external_focus_no_{operation.lower()}
            BEFORE {operation} ON external_focus
            BEGIN SELECT RAISE(ABORT, 'external focus is immutable'); END""")
    db.commit()
    return row


def test_schema_upgrade_reopen_preserves_rows_and_constraints(tmp_path):
    from project_watch.external_focus_schema import ensure_external_focus_schema

    path = tmp_path / 'old.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        row = legacy_database(db)
        ensure_external_focus_schema(db)
        db.commit()
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        ensure_external_focus_schema(db)
        assert db.execute('SELECT * FROM external_focus').fetchall() == [row]
        for statement in ('DELETE FROM external_focus', "UPDATE external_focus SET term='changed'"):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(statement)
        db.execute("INSERT INTO monitoring_plans VALUES ('bing')")
        db.execute('INSERT INTO external_focus VALUES(?,?,?,?,?,?,?)', ('bing', *row[1:3], 'bing', *row[4:]))
        with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
            db.execute('INSERT INTO external_focus VALUES(?,?,?,?,?,?,?)', ('bad', *row[1:3], 'invalid', *row[4:]))
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_schema_failed_migration_preserves_original_table_triggers_and_transaction():
    from project_watch.external_focus_schema import ensure_external_focus_schema

    with closing(sqlite3.connect(':memory:')) as db:
        row = legacy_database(db, category='old-invalid-category')
        schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        db.execute("INSERT INTO monitoring_plans VALUES ('outer-transaction')")
        with pytest.raises(sqlite3.IntegrityError):
            ensure_external_focus_schema(db)
        assert db.in_transaction
        assert db.execute('SELECT * FROM external_focus').fetchall() == [row]
        assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall() == schema
        assert db.execute("SELECT 1 FROM monitoring_plans WHERE id='outer-transaction'").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute('DELETE FROM external_focus')
