"""Offline old-full seed -> installed full/Studio read-only classic history replay."""
import argparse
import hashlib
import importlib.abc
import importlib.util
import json
import runpy
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import project_watch
from project_watch.repository import ProjectRepository


def seed(root):
    root.mkdir(parents=True, exist_ok=False)
    helpers = runpy.run_path(str(Path(__file__).with_name('test_company_state.py')))
    repo, project, source, packet, output = helpers['sample'](root)
    _ledger, state, job = helpers['publish'](repo, source, packet, output)
    # A second success with the identical digest must not duplicate the view.
    with closing(repo._connect()) as db, db:
        db.execute('''INSERT INTO reading_jobs SELECT 'synthetic-duplicate',project_id,authorization_id,
            'synthetic-duplicate',sources_json,input_digest,state,attempts,lease_token,lease_until,created_at,updated_at
            FROM reading_jobs WHERE id=?''', (job['id'],))
        db.execute('''INSERT INTO company_reading_batches SELECT 'synthetic-duplicate',project_id,source_id,
            input_digest,packet_json,output_json,rule_version,created_at FROM company_reading_batches WHERE job_id=?''', (job['id'],))
    for mode, sha in [('stale', 'c'), ('uncertain', 'd')]:
        src = helpers['material'](repo, project, packet['fragments'][0]['text'], sha * 64)
        pkt = helpers['input_packets'](repo, src)[0]
        _, _, saved = helpers['publish'](repo, src, pkt, output)
        with closing(repo._connect()) as db, db:
            if mode == 'stale':
                db.execute("UPDATE document_fragments SET text='Synthetic correction' WHERE source_id=?", (src.id,))
            else:
                db.execute("UPDATE reading_jobs SET state='uncertain' WHERE id=?", (saved['id'],))
    other = repo.create_project('Other synthetic company')
    repo.edit_monitor_profile(project.id, expected_version=0, fields={'business': 'Human override'}, focus=[])
    from project_watch.monitor_profile import project_profile
    expected = {'state': state.read(project.id), 'profile': project_profile(repo, project.id),
                'project_id': project.id, 'other_id': other.id, 'history': repo.monitor_profile_history(project.id)}
    assert len(expected['state']['batches']) == 1 and expected['state']['stale_batches'] == 1
    expected['db_sha256'] = hashlib.sha256(Path(repo.db_path).read_bytes()).hexdigest()
    (root / 'expected.json').write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'seed': True, 'batches': 4, 'visible': 1, 'stale': 1, 'uncertain': 1, 'live_calls': 0}


def read(root):
    expected = json.loads((root / 'expected.json').read_text(encoding='utf-8'))
    path = root / 'watch.sqlite3'
    blocked = {'project_watch.company_state', 'project_watch.reading_jobs',
               'project_watch.reading_worker', 'project_watch.dsh_executor'}

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in blocked:
                raise AssertionError('Read attempted legacy import: ' + fullname)

    sys.meta_path.insert(0, Block())

    class ReadOnly(ProjectRepository):
        def __init__(self):
            self.db_path = path

        def _connect(self):
            db = super()._connect()
            db.execute('PRAGMA query_only=ON')
            return db

    from project_watch.company_reading_view import CompanyStateView
    repo = ReadOnly()
    assert CompanyStateView(repo).read(expected['project_id']) == expected['state']
    assert CompanyStateView(repo).read(expected['other_id'])['batches'] == []
    assert repo.monitor_profile_history(expected['project_id']) == expected['history']
    has_profile = importlib.util.find_spec('project_watch.monitor_profile') is not None
    if has_profile:
        from project_watch.monitor_profile import project_profile
        assert project_profile(repo, expected['project_id']) == expected['profile']
    assert not blocked.intersection(sys.modules)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected['db_sha256']
    return {'history_read': True, 'profile_projection': 'unchanged' if has_profile else 'classic_not_packaged',
            'database_unchanged': True, 'legacy_imports': 0, 'live_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--phase', choices=('seed', 'read'), required=True)
    args = parser.parse_args()
    assert args.root.is_absolute()
    assert Path(project_watch.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())

    def forbidden(*_args, **_kwargs):
        raise AssertionError('No network permitted')

    with patch('socket.create_connection', forbidden), patch('socket.getaddrinfo', forbidden):
        result = seed(args.root) if args.phase == 'seed' else read(args.root)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
