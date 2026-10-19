"""Storage and saved-reading views work without legacy generation or job imports."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def run_isolated(program, *args):
    import project_watch

    result = subprocess.run([sys.executable, '-I', '-c', program,
                             str(Path(project_watch.__file__).parent.parent), *map(str, args)],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_profile_storage_does_not_import_generation_or_create_reading_jobs(tmp_path):
    run_isolated('''
import importlib.abc
import sys
from contextlib import closing
sys.path.insert(0, sys.argv[1])
blocked = {'project_watch.monitor_profile', 'project_watch.company_state',
           'project_watch.reading_jobs', 'project_watch.reading_worker', 'project_watch.dsh_executor'}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from project_watch.repository import ProjectRepository, ReviewConflictError
from project_watch.company_reading_view import CompanyStateView
repo = ProjectRepository(sys.argv[2])
p = repo.create_project('Synthetic isolated profile')
assert repo.edit_monitor_profile(p.id, expected_version=0, fields={'business':'My description'}, focus=['My focus']) == 1
try:
    repo.edit_monitor_profile(p.id, expected_version=0, fields={'business':'Conflict'})
except ReviewConflictError:
    pass
else:
    raise AssertionError('optimistic version check lost')
repo.edit_monitor_profile(p.id, expected_version=1, restore_fields=['business'], restore_focus=True)
assert repo.monitor_profile_history(p.id)[0]['fields']['business'] == 'My description'
assert repo.monitor_profile_history(p.id)[1]['fields'] == {}
assert repo.monitor_profile_history(p.id)[1]['focus'] is None
with closing(repo._connect()) as db:
    tables = db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
assert CompanyStateView(repo).read(p.id) == {'project_id':p.id,'batches':[],'stale_batches':0}
with closing(repo._connect()) as db:
    assert tables == db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='reading_jobs'").fetchone()
    try:
        db.execute("DELETE FROM monitor_profile_revisions")
    except Exception as exc:
        assert 'append only' in str(exc)
    else:
        raise AssertionError('history became mutable')
assert not blocked.intersection(sys.modules)
''', tmp_path / 'synthetic.sqlite3')


def test_full_saved_view_and_profile_read_without_legacy_jobs(tmp_path):
    from test_company_state import publish, sample

    from project_watch.company_reading_view import CompanyStateView
    from project_watch.monitor_profile import project_profile

    repo, project, source, packet, output = sample(tmp_path)
    _, old_state, _ = publish(repo, source, packet, output)
    repo.edit_monitor_profile(project.id, expected_version=0, fields={'business': 'Human description'})
    expected = {'state': old_state.read(project.id), 'profile': project_profile(repo, project.id)}
    assert CompanyStateView(repo).read(project.id) == expected['state']
    baseline = hashlib.sha256(Path(repo.db_path).read_bytes()).hexdigest()
    run_isolated('''
import importlib.abc
import json
import sys
sys.path.insert(0, sys.argv[1])
blocked = {'project_watch.company_state', 'project_watch.reading_jobs',
           'project_watch.reading_worker', 'project_watch.dsh_executor'}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from project_watch.repository import ProjectRepository
from project_watch.company_reading_view import CompanyStateView
from project_watch.monitor_profile import project_profile
class ReadOnly(ProjectRepository):
    def __init__(self, path):
        from pathlib import Path
        self.db_path = Path(path)
    def _connect(self):
        db = super()._connect()
        db.execute('PRAGMA query_only=ON')
        return db
repo = ReadOnly(sys.argv[2])
assert {'state':CompanyStateView(repo).read(sys.argv[3]),
        'profile':project_profile(repo, sys.argv[3])} == json.loads(sys.argv[4])
assert not blocked.intersection(sys.modules)
''', repo.db_path, project.id, json.dumps(expected))
    assert hashlib.sha256(Path(repo.db_path).read_bytes()).hexdigest() == baseline


def test_full_compatibility_aliases_and_read_identity():
    from project_watch import (
        company_reading_view,
        company_state,
        monitor_profile,
        monitor_profile_store,
    )

    for name in ('PROFILE_FIELDS', 'ensure_profile_schema', 'validate_edit'):
        assert getattr(monitor_profile, name) is getattr(monitor_profile_store, name)
    assert company_state._current_packet is company_reading_view._current_packet
    assert company_state.CompanyState.read is company_reading_view.CompanyStateView.read
