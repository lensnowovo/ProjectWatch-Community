import sqlite3

import pytest
from test_studio_agent_instance import make_service


def test_expired_lease_blocks_legacy_plan_and_site_writes(tmp_path):
    from project_watch.studio.app import create_studio_app
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project('Synthetic fence')
    pipeline.store.upsert_project(project.id, {}, consent=True)
    # Initialise monitor before the run so this tests mutations, not schema creation.
    monitor = pipeline.monitor
    def stale(job):
        with sqlite3.connect(pipeline.store.db_path) as db:
            db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (job['id'],))
        for write in (lambda: pipeline.store.put_plan(project.id, {'late': True}),
                      lambda: monitor.add_page(project.id, 'https://synthetic.example', 'late', 'home', 'auto'),
                      lambda: pipeline.health.record(project.id, {'company': {'checked': 1, 'added': 0, 'errors': []}})):
            with pytest.raises(RuntimeError, match='lease'):
                write()
    pipeline.run_external = stale
    pipeline.store.enqueue(project.id, 'external')
    with pytest.raises(RuntimeError, match='lease'):
        pipeline.run(pipeline.store.claim())
    assert pipeline.store.get_plan(project.id) is None and monitor.pages(project.id) == []
    assert pipeline.health.read(project.id)['sources'] == []
    # User actions after a failed dispatch must not inherit its expired guard.
    pipeline.store.put_plan(project.id, {'user': True})
    assert pipeline.store.get_plan(project.id)['user']


def test_scope_fences_cursor_script_commit_and_preserves_ordinary_reads(tmp_path):
    from project_watch.studio.execution import JobExecution
    service, pid = make_service(tmp_path / 'fences.sqlite3')
    service.store.enqueue(pid, 'external')
    execution = JobExecution(service.store, service.store.claim())
    with service.store.write_scope(execution._guard):
        db = service.store._connect()
        assert db.execute('SELECT count(*) FROM studio_projects').fetchone()[0] == 1
        assert not db.in_transaction
        with sqlite3.connect(service.store.db_path) as control:
            control.execute("UPDATE studio_jobs SET lease_until='2000-01-01'")
        for write in (lambda: db.execute("UPDATE studio_projects SET note='late'"),
                      lambda: db.cursor().execute("UPDATE studio_projects SET note='late'"),
                      lambda: db.executemany('UPDATE studio_projects SET note=?', [('late',)]),
                      lambda: db.executescript("CREATE TABLE late(value TEXT); INSERT INTO late VALUES ('late');")):
            with pytest.raises(RuntimeError, match='lease'):
                write()
            db.rollback()
    with pytest.raises(RuntimeError, match='lease'):
        db.execute("UPDATE studio_projects SET note='escaped connection'")
    db.rollback()
    db.close()
    assert service.store.get_project(pid)['note'] == ''


def test_commit_rechecks_lease_and_rolls_back_complete_script(tmp_path):
    service, pid = make_service(tmp_path / 'commit.sqlite3')
    active = True

    def guard(db):
        if not active:
            raise RuntimeError('job lease expired')

    with service.store.write_scope(guard):
        db = service.store._connect()
        with pytest.raises(RuntimeError, match='lease'), db:
            db.executescript("""
                    CREATE TABLE scripted(value TEXT);
                    CREATE TRIGGER scripted_change AFTER INSERT ON scripted
                    BEGIN UPDATE studio_projects SET note=NEW.value; END;
                    INSERT INTO scripted VALUES ('a;b');
                """)
            assert db.execute('SELECT note FROM studio_projects').fetchone()[0] == 'a;b'
            active = False
        assert not db.in_transaction
        assert db.execute("SELECT name FROM sqlite_master WHERE name='scripted'").fetchone() is None
        db.close()
    assert service.store.get_project(pid)['note'] == ''


def test_nested_scope_restores_guard_and_lazy_schema_is_guarded(tmp_path):
    from project_watch.studio.monitor import MonitorStore
    service, pid = make_service(tmp_path / 'nested.sqlite3')
    checks = []
    with service.store.write_scope(lambda db: checks.append('outer')):
        with service.store.write_scope(lambda db: checks.append('inner')):
            MonitorStore(service.store)
            service.store.put_plan(pid, {'inner': True})
        service.store.put_plan(pid, {'outer': True})
    assert 'inner' in checks and checks[-1] == 'outer'
    count = len(checks)
    service.store.put_plan(pid, {'user': True})
    assert len(checks) == count


def test_site_reads_replay_and_unknown_robots_never_dispatch_page(tmp_path):
    from test_studio_monitor import HOME, Site

    from project_watch.feeds import FeedError
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.http import HttpError
    from project_watch.studio.monitor import MonitorStore, check_sites

    service, pid = make_service(tmp_path / 'site-replay.sqlite3')
    monitor = MonitorStore(service.store)
    service.store.enqueue(pid, 'external')
    execution = JobExecution(service.store, service.store.claim())
    transport = Site({'https://synthetic.example/': HOME})
    with service.store.write_scope(execution._guard):
        check_sites(monitor, transport, pid, 'https://synthetic.example/', execution=execution)
    initial_calls = list(transport.requests)
    # Reset local check timestamps only to simulate a lost caller checkpoint.
    with service.store._connect() as db:
        db.execute('UPDATE studio_site_pages SET last_checked=NULL')
    with service.store.write_scope(execution._guard):
        check_sites(monitor, transport, pid, 'https://synthetic.example/', execution=execution)
    assert transport.requests == initial_calls

    service2, pid2 = make_service(tmp_path / 'site-unknown.sqlite3')
    monitor2 = MonitorStore(service2.store)
    service2.store.enqueue(pid2, 'external')
    execution2 = JobExecution(service2.store, service2.store.claim())
    calls = []
    class Failed:
        def get_response(self, url):
            calls.append(url)
            raise FeedError('timeout')
    for _ in range(2):
        with pytest.raises(HttpError, match='request_outcome_unknown'), service2.store.write_scope(execution2._guard):
            check_sites(monitor2, Failed(), pid2, 'https://synthetic.example/', execution=execution2)
    assert calls == ['https://synthetic.example/robots.txt']
