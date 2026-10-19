"""Connect existing local Studio projects to Codex, without model/search calls."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--project', help='Connect one project ID; default connects existing projects')
    parser.add_argument('--directory', type=Path, required=True, help='Private connection files, outside Git')
    args = parser.parse_args()
    if args.directory.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error('Connection secrets must be stored outside the repository')
    parsed = urlsplit(args.url)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        parser.error('An explicit loopback HTTP origin is required')
    codex = shutil.which('codex')
    if not codex:
        parser.error('Codex CLI is not installed')
    opener = build_opener(ProxyHandler({}))

    def request(path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request(args.url.rstrip('/') + path, data=body, headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=15) as response:
            return json.load(response)

    projects = request('/api/studio/projects')
    if args.project:
        projects = [p for p in projects if p['id'] == args.project]
        if not projects:
            parser.error('Project not found')
    args.directory.mkdir(parents=True, exist_ok=True)
    results = []
    for project in projects:
        pid = project['id']
        grant = request(f'/api/studio/projects/{pid}/agent-grants',
                        {'permission': 'write', 'label': 'Codex local project maintenance', 'hours': 720,
                         'allow_internal': False})
        connection = args.directory / f'{pid}.json'
        connection.write_text(json.dumps({'url': args.url, 'instance_id': grant['instance_id'],
                              'project_id': pid, 'token': grant['token'], 'grant_id': grant['id'],
                              'project_name': project['name']}, ensure_ascii=False), encoding='utf-8')
        os.chmod(connection, 0o600)
        name = 'projectwatch_' + pid[:8]
        # Only a filename is passed to the CLI; never put the grant on a command line.
        completed = subprocess.run([codex, 'mcp', 'add', name, '--env',
                                   f'PROJECT_WATCH_AGENT_CONFIG={connection.resolve()}', '--',
                                   sys.executable, '-I', '-m', 'project_watch.studio.agent_bridge'],
                                  capture_output=True, text=True, check=False)
        if completed.returncode:
            raise RuntimeError('Codex MCP registration failed; private connection preserved, no automatic retry')
        config_path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
        text = config_path.read_text(encoding='utf-8')
        pattern = re.compile(r'(^\[mcp_servers\.' + re.escape(name) + r'\]\n)(.*?)(?=^\[|\Z)', re.M | re.S)

        def approval_setting(match):
            body = re.sub(r'^default_tools_approval_mode\s*=.*\n?', '', match[2], flags=re.M)
            return match[1] + 'default_tools_approval_mode = "writes"\n' + body

        updated, replacements = pattern.subn(approval_setting, text)
        if replacements != 1:
            raise RuntimeError('Cannot locate registered MCP configuration; inspect it before reconnecting')
        tomllib.loads(updated)
        temporary = config_path.with_suffix('.projectwatch.tmp')
        temporary.write_text(updated, encoding='utf-8')
        os.replace(temporary, config_path)
        results.append({'server': name, 'project': project['name'], 'project_id': pid,
                        'permission': 'write', 'internal': False, 'expires': grant.get('expires_at')})
    print(json.dumps({'connected': results, 'provider_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
