"""Connect existing local Studio projects to Claude Code over MCP, without model/search calls.

Each project gets its own write grant (public material only) and a private connection file outside Git;
Claude Code is told only that file's path. Reconnecting replaces the server entry and revokes the old grant.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--project', help='Connect one project ID; default connects existing projects')
    parser.add_argument('--directory', type=Path, required=True, help='Private connection files, outside Git')
    parser.add_argument('--scope', choices=('user', 'local'), default='user',
                        help='Claude Code configuration scope (never the shared project .mcp.json)')
    parser.add_argument('--hours', type=int, default=720)
    args = parser.parse_args()
    if args.directory.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error('Connection secrets must be stored outside the repository')
    parsed = urlsplit(args.url)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        parser.error('An explicit loopback HTTP origin is required')
    if not 1 <= args.hours <= 720:
        parser.error('--hours must be between 1 and 720')
    claude = shutil.which('claude')
    if not claude:
        parser.error('Claude Code CLI is not installed')
    opener = build_opener(ProxyHandler({}))

    def request(path, payload=None, method=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request(args.url.rstrip('/') + path, data=body, headers={'Content-Type': 'application/json'}, method=method)
        with opener.open(req, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}

    projects = request('/api/studio/projects')
    if args.project:
        projects = [p for p in projects if p['id'] == args.project]
        if not projects:
            parser.error('Project not found')
    args.directory.mkdir(parents=True, exist_ok=True)
    results = []
    for project in projects:
        pid = project['id']
        connection = args.directory / f'{pid}.json'
        previous = None
        if connection.exists():
            try:
                previous = json.loads(connection.read_text(encoding='utf-8')).get('grant_id')
            except (OSError, ValueError):
                previous = None
        grant = request(f'/api/studio/projects/{pid}/agent-grants',
                        {'permission': 'write', 'label': 'Claude Code local project research', 'hours': args.hours,
                         'allow_internal': False})
        temporary = connection.with_suffix('.tmp')
        temporary.write_text(json.dumps({'url': args.url, 'instance_id': grant['instance_id'],
                             'project_id': pid, 'token': grant['token'], 'grant_id': grant['id'],
                             'project_name': project['name']}, ensure_ascii=False), encoding='utf-8')
        os.chmod(temporary, 0o600)
        os.replace(temporary, connection)
        name = 'projectwatch_' + pid[:8]
        # Replace an earlier entry of the same name; absence is not an error.
        subprocess.run([claude, 'mcp', 'remove', '--scope', args.scope, name], capture_output=True, text=True, check=False)
        # Only a filename is passed to the CLI; never put the grant on a command line.
        completed = subprocess.run([claude, 'mcp', 'add', '--scope', args.scope, name,
                                    '-e', f'PROJECT_WATCH_AGENT_CONFIG={connection.resolve()}', '--',
                                    sys.executable, '-I', '-m', 'project_watch.studio.agent_bridge'],
                                   capture_output=True, text=True, check=False)
        if completed.returncode:
            raise RuntimeError('Claude Code MCP registration failed; private connection preserved, no automatic retry')
        revoked = False
        if previous and previous != grant['id']:
            try:
                request(f'/api/studio/projects/{pid}/agent-grants/{previous}', method='DELETE')
                revoked = True
            except HTTPError:
                revoked = False  # already expired or revoked
        results.append({'server': name, 'project': project['name'], 'project_id': pid, 'scope': args.scope,
                        'permission': 'write', 'internal': False, 'expires': grant.get('expires_at'),
                        'previous_grant_revoked': revoked})
    print(json.dumps({'connected': results, 'provider_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
