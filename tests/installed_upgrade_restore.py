"""Offline cross-version installed-wheel drill. All inputs synthetic; no listeners.

Run old wheel with --phase seed, new wheel with --phase upgrade --label full/studio,
then old wheel with --phase rollback --label full/studio, using the same new --root.
Requires physically installed packages, never inserts source paths. Every restore
targets a new directory; the seed and both backup generations stay unchanged.
"""
import argparse
import hashlib
import json
import runpy
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import project_watch
from project_watch.studio.app import create_studio_app
from project_watch.studio.host import restore_snapshot, snapshot, verify_snapshot


def digest(value):
    return hashlib.sha256(value).hexdigest()


def state(home):
    with closing(sqlite3.connect(f'{(home / "projectwatch.sqlite3").as_uri()}?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        tables = {}
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            safe = '"' + name.replace('"', '""') + '"'
            rows = sorted(json.dumps(tuple(row), ensure_ascii=True, default=lambda v: v.hex())
                          for row in db.execute('SELECT * FROM ' + safe))
            tables[name] = {'count': len(rows), 'sha256': digest('\n'.join(rows).encode())}
        pauses = dict(db.execute('SELECT project_id,status FROM studio_projects'))
        unknown = db.execute("SELECT count(*) FROM studio_job_calls WHERE status IN ('unknown','started')").fetchone()[0]
        identity = dict(db.execute('SELECT * FROM studio_instance').fetchone())
        grants = [dict(row) for row in db.execute('SELECT * FROM studio_agent_grants ORDER BY id')]
    files = {p.relative_to(home).as_posix(): digest(p.read_bytes()) for p in home.rglob('*')
             if p.is_file() and p.name not in ('.studio.lock', '.supervisor.lock', 'studio-manifest.json')
             and p.suffix != '.sqlite3' and not p.name.endswith(('-wal', '-shm'))}
    return {'tables': tables, 'files': files, 'pauses': pauses, 'unknown_calls': unknown,
            'identity': identity, 'grants': grants}


def compare(before, after, *, relocated=False):
    assert before['files'] == after['files'], 'Non-database files changed'
    assert before['pauses'] == after['pauses'], 'Pause states changed'
    assert before['unknown_calls'] == after['unknown_calls'] > 0, 'Unknown request history changed'
    if relocated:
        # Existing security contract: copied homes get a new instance binding and
        # inherited Agent credentials are revoked, never silently reusable.
        assert before['identity']['instance_id'] != after['identity']['instance_id']
        assert before['identity']['binding_hash'] != after['identity']['binding_hash']
        assert len(before['grants']) == len(after['grants'])
        for old, new in zip(before['grants'], after['grants'], strict=True):
            assert {k: v for k, v in old.items() if k != 'revoked_at'} == {
                k: v for k, v in new.items() if k != 'revoked_at'}
            assert new['revoked_at'] and (old['revoked_at'] is None or new['revoked_at'] == old['revoked_at'])
    for name, values in before['tables'].items():
        if relocated and name in ('studio_instance', 'studio_agent_grants'):
            assert after['tables'][name]['count'] == values['count']
            continue
        assert after['tables'].get(name) == values, 'Table rows changed: ' + name
    extra = sorted(set(after['tables']) - set(before['tables']))
    assert all(after['tables'][n]['count'] == 0 for n in extra), 'Unexpected new records'
    return extra


def seed(root):
    assert not root.exists(), 'Seed requires a new directory'
    root.mkdir(parents=True)
    journey = runpy.run_path(str(Path(__file__).with_name('distribution_journey_support.py')))
    core = runpy.run_path(str(Path(__file__).with_name('core_without_insight_support.py')))
    home = root / 'old-home'
    app, transport = journey['make_app'](home)
    with TestClient(app) as client:
        journey['configure'](client)
        pid, _ = journey['run_name_journey'](client, app)
        receipt = core['bp_incremental_history'](client, app, transport)
        assert client.patch('/api/studio/projects/' + receipt['project_id'], json={'status': 'paused'}).status_code == 200
        entity = journey['SyntheticTransport'].model_result('准确工商主体', '')['candidates'][0]
        # Name/BP deliberately resolve to the same project. The unknown-call
        # fixture must be a second company, not re-create that paused project.
        entity = {**entity, 'legal_name': '合成未知请求科技有限公司', 'short_name': '合成未知请求'}
        failure = core['unknown_outcome_not_retried'](client, app, transport, entity)
        from project_watch.external_scopes import ExternalFocusStore
        from project_watch.monitoring import FeedItem, ManualSnapshotAdapter, MonitoringRunner

        repo = app.state.intake_worker.repository
        scope = ExternalFocusStore(repo).create(pid, category='company', term=journey['COMPANY'], provider='gdelt')
        repo.set_monitoring_plan_enabled(scope.id, True)
        runner = MonitoringRunner(repo, {'rss': ManualSnapshotAdapter({scope.target: (
            FeedItem('synthetic-legacy', journey['COMPANY'] + ' 合成公告', 'https://synthetic.example/legacy', '', datetime.now(UTC)),)})})
        assert runner.run(scope.id, idempotency_key='synthetic-legacy').new_count == 1
        repo.set_monitoring_plan_enabled(scope.id, False)
    baseline = state(home)
    manifest = snapshot(home, root / 'before-backup')
    compare(baseline, state(root / 'before-backup'))
    record = {'baseline': baseline, 'bp': receipt, 'failure': failure, 'name_project': pid,
              'legacy_scope': scope.id, 'synthetic_http_calls': len(transport.calls),
              'live_calls': 0, 'backup_files': len(manifest['files']), 'installed_at': project_watch.__file__}
    (root / 'seed.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return {k: record[k] for k in ('synthetic_http_calls', 'live_calls', 'backup_files', 'installed_at')}


def upgrade_or_rollback(root, phase, label):
    record = json.loads((root / 'seed.json').read_text(encoding='utf-8'))
    source = root / ('before-backup' if phase == 'upgrade' else label + '-after-backup')
    home = root / (label + '-' + phase + '-verified')
    verify_snapshot(source)
    restore_snapshot(source, home)
    baseline = state(home)
    compare(record['baseline'], baseline, relocated=(phase == 'rollback'))
    app = create_studio_app(home, start_worker=False)
    with TestClient(app) as client:
        bp = record['bp']
        route = '/api/studio/projects/' + bp['project_id']
        assert client.get(route).status_code == 200
        history = client.get(route + '/questions/' + bp['watch_id'] + '/history').json()
        assert sorted(row['id'] for row in history) == bp['history_ids']
        for source_id in bp['source_ids']:
            assert client.get('/api/sources/' + source_id + '/fragments').json()
        failure = record['failure']
        recovery = client.get('/api/studio/projects/' + failure['project_id'] + '/jobs/' + failure['job_id'] + '/recovery')
        assert recovery.status_code == 200 and recovery.json()['can_recover']
        # No recovery authorization. An ordinary idle worker step cannot retry.
        app.state.studio_worker._last_schedule = datetime.now(UTC)
        assert not app.state.studio_worker.step()
        assert client.get('/api/studio/health').status_code == 200
    after = state(home)
    extra = compare(baseline, after, relocated=True)
    # Reopening the same home must not churn the new identity or revoke more data.
    with TestClient(create_studio_app(home, start_worker=False)) as client:
        assert client.get('/api/studio/health').status_code == 200
    compare(after, state(home))
    if phase == 'upgrade':
        snapshot(home, root / (label + '-after-backup'))
        compare(after, state(root / (label + '-after-backup')))
    # The old home and the immutable pre-upgrade backup were never opened for writes.
    compare(record['baseline'], state(root / 'old-home'))
    compare(record['baseline'], state(root / 'before-backup'))
    result = {'phase': phase, 'label': label, 'installed_at': project_watch.__file__,
              'tables_verified': len(baseline['tables']), 'extra_empty_tables': extra,
              'projects': len(after['pauses']), 'unknown_calls_held': after['unknown_calls'],
              'files_preserved': len(after['files']), 'relocated_agent_grants_revoked': True,
              'same_path_reopen_unchanged': True, 'live_calls': 0}
    (root / (label + '-' + phase + '.json')).write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def in_place(root, phase, label):
    record = json.loads((root / 'seed.json').read_text(encoding='utf-8'))
    home = root / (label + '-same-path')
    if phase == 'bind-old':
        restore_snapshot(root / 'before-backup', home)
    before = state(home)
    with TestClient(create_studio_app(home, start_worker=False)) as client:
        assert client.get('/api/studio/projects/' + record['bp']['project_id']).status_code == 200
        assert client.get('/api/studio/health').status_code == 200
    after = state(home)
    compare(before, after, relocated=(phase == 'bind-old'))
    result = {'phase': phase, 'label': label, 'installed_at': project_watch.__file__,
              'all_table_rows_preserved': phase != 'bind-old', 'files_preserved': True,
              'unknown_calls_held': after['unknown_calls'], 'live_calls': 0}
    (root / (label + '-' + phase + '.json')).write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--phase', choices=('seed', 'upgrade', 'rollback', 'bind-old',
                                            'upgrade-in-place', 'rollback-in-place'), required=True)
    parser.add_argument('--label', choices=('full', 'studio'), default='studio')
    args = parser.parse_args()
    package = Path(project_watch.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), 'Must use an installed wheel'
    assert args.root.is_absolute(), 'Use an absolute new isolated root'

    def no_network(*args, **kwargs):
        raise AssertionError('No outbound network permitted in upgrade drill')

    with patch('socket.create_connection', no_network), patch('socket.getaddrinfo', no_network):
        if args.phase == 'seed':
            result = seed(args.root)
        elif args.phase in ('upgrade', 'rollback'):
            result = upgrade_or_rollback(args.root, args.phase, args.label)
        else:
            result = in_place(args.root, args.phase, args.label)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
