"""Publish only the reviewed Mac artifact; preserve the existing Windows release."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

REPO = 'lensnowovo/ProjectWatch-Community'
TAG = 'v0.2.0rc2-preview.1'
ROOT = Path(__file__).resolve().parents[1]


def gh(*args):
    return subprocess.check_output(['gh', *map(str, args)], text=True)


def api(path):
    return json.loads(gh('api', f'repos/{REPO}/{path}'))


def normalize(value):
    return value.replace('\r\n', '\n').strip()


def main():
    config = json.loads((ROOT / 'scripts/macos-r11b-publication.json').read_text())
    assert os.environ['GITHUB_REPOSITORY'] == REPO
    run = api(f"actions/runs/{config['run_id']}")
    assert run['head_sha'] == config['build_commit'] and run['conclusion'] == 'success'
    release = api('releases/tags/' + TAG)
    assert release['id'] == 426477075 and release['prerelease'] and not release['draft']
    assert api('git/ref/tags/' + TAG)['object']['sha'] == '6055415a8cc198a24972c0f97b28acd00861116d'
    before = {a['name']: a['digest'] for a in release['assets']}
    for name, digest in config['windows_assets'].items():
        assert before[name] == 'sha256:' + digest
    if normalize(release['body']).endswith(normalize(config['notes_append'])):
        actual = {a['name']: a for a in release['assets']}
        for asset in config['assets']:
            got = actual[asset['name']]
            assert got['state'] == 'uploaded' and got['size'] == asset['bytes']
            assert got['digest'] == 'sha256:' + asset['sha256']
        print(json.dumps({'already_published_verified': True, 'uploads': 0,
                          'release': release['html_url']}))
        return
    assert hashlib.sha256(release['body'].encode()).hexdigest() == config['previous_body_sha256']
    with tempfile.TemporaryDirectory(prefix='macos-publication-') as folder:
        folder = Path(folder)
        gh('run', 'download', config['run_id'], '--repo', REPO,
           '--name', 'macos-r11b-arm64', '--dir', folder)
        receipt = json.loads((folder / 'receipts/verification.json').read_text())
        assert receipt['build_commit'] == config['build_commit']
        assert receipt['product_commit'] == 'aa53cf1f08e1fe48cacec8b93c36571892216bd5'
        assert receipt['arch'] == 'arm64' and receipt['real_provider_calls'] == 0
        assert receipt['native_installed_journey'] and receipt['native_dsh_synthetic']
        assert not receipt['tests']['failures'] and not receipt['tests']['errors']
        audit = json.loads((folder / 'receipts/dependency-audit.json').read_text())
        assert audit['dependencies'] and not any(d.get('vulns') for d in audit['dependencies'])
        paths = []
        for asset in config['assets']:
            assert Path(asset['name']).name == asset['name']
            path = folder / 'delivery' / asset['name']
            assert path.stat().st_size == asset['bytes']
            assert hashlib.sha256(path.read_bytes()).hexdigest() == asset['sha256']
            if asset['name'] in before:
                assert before[asset['name']] == 'sha256:' + asset['sha256']
            else:
                paths.append(path)
        if paths:
            gh('release', 'upload', TAG, *paths, '--repo', REPO)
        after = api('releases/tags/' + TAG)
        actual = {a['name']: a for a in after['assets']}
        for name, digest in before.items():
            assert actual[name]['digest'] == digest
        for asset in config['assets']:
            got = actual[asset['name']]
            assert got['state'] == 'uploaded' and got['size'] == asset['bytes']
            assert got['digest'] == 'sha256:' + asset['sha256']
        assert after['body'] == release['body']
        notes = folder / 'notes.md'
        notes.write_text(release['body'] + '\n\n' + config['notes_append'], encoding='utf-8')
        gh('release', 'edit', TAG, '--repo', REPO, '--notes-file', notes,
           '--title', 'ProjectWatch 0.2.0rc2 · Windows / Apple Silicon 公开预览版 R11b')
        final = api('releases/tags/' + TAG)
        assert normalize(final['body']) == normalize(notes.read_text(encoding='utf-8'))
        print(json.dumps({'release': final['html_url'], 'mac_assets_verified': len(config['assets']),
                          'existing_assets_unchanged': True, 'tag_unchanged': True}))


if __name__ == '__main__':
    main()
