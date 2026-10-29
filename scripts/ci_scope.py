"""Bounded, read-only CI evidence selection. No product data or provider calls."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAINS = ('python', 'frontend', 'native', 'installed')
WORKFLOW = '.github/workflows/ci.yml'
FORMAT = 'projectwatch-ci-domain-v1'
TTL = timedelta(days=7)
FRONTEND_TESTS = (
    'tests/test_web_preview.py', 'tests/test_static_asset_mime.py',
    'tests/test_windows_icon.py', 'tests/test_frontend_notices.py',
    'tests/test_package_boundary.py', 'tests/test_layer_boundaries.py',
    'tests/test_core_probe.py', 'tests/test_studio_runtime_boundary.py',
    'tests/test_studio.py', 'tests/test_studio_taskboard.py', 'tests/test_postgres_migrations.py',
)
CONTRACT_TESTS = (
    'tests/test_ci_scope.py', 'tests/test_release_ci.py',
    'tests/test_release_security_dependencies.py', 'tests/test_release_document_links.py',
    'tests/test_release_feedback.py', 'tests/test_developer_distribution.py',
)
CI_ONLY = {WORKFLOW, '.github/workflows/release-candidate.yml', 'scripts/ci_scope.py',
           *CONTRACT_TESTS}
LEGACY = {
    'run': 40793763735, 'job': 123185078901, 'attempt': 1,
    'sha': '43e44bea88ff67915bbff8c2c95fd8c6a994dc16',
    'tree': 'cd238c00c66fb9ea5a61766512627dbb3e4a788d',
    'workflow_blob': '823213db0124a598650a0ff628eb99ac3b626451',
}
LEGACY_COMMON = ('Checkout without saved credentials', 'Python', 'Node',
                 'Confirm native runner', 'Install isolated test dependencies')
LEGACY_STEPS = {
    'python': (*LEGACY_COMMON, 'Lint', 'Architecture boundaries', 'Offline Python suite'),
    'frontend': (*LEGACY_COMMON, 'Frontend dependencies', 'Frontend tests', 'Frontend types',
                 'Frontend build', 'Node regression', 'Offline Python suite'),
    'native': (*LEGACY_COMMON, 'Prepare pinned native DSH (no install scripts)',
               'Native DSH with synthetic model gateway'),
    'installed': (*LEGACY_COMMON, 'Frontend dependencies', 'Frontend build', 'Build full wheel',
                  'Fresh installed-wheel smoke', 'Full installed synthetic journey'),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def sha256(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, timeout=30).decode('utf-8')


def tracked_inputs():
    rows = []
    for item in git('ls-tree', '-rz', 'HEAD').split('\0'):
        if item:
            identity, path = item.split('\t', 1)
            mode, kind, blob = identity.split()
            if kind != 'blob':
                raise ValueError('Unsupported tracked input type')
            rows.append({'path': path, 'mode': mode, 'blob': blob})
    return rows


def domains_for(path):
    if path in CI_ONLY:
        return set()
    if path.startswith('docs/') or path in {'README.md', 'CHANGELOG.md', 'AGENTS.md', 'SUPPORT.md'}:
        return set()
    if path.startswith('.github/ISSUE_TEMPLATE/'):
        return set()
    if path == 'tests/test_studio.py':
        return set(DOMAINS)  # _item is also imported by the main Python suite.
    if path in FRONTEND_TESTS:
        return {'frontend'}
    if path.startswith(('frontend/', 'src/project_watch/web/', 'src/project_watch/static/')):
        return {'frontend', 'installed'}
    if path.startswith('tests/') and path.endswith('.test.mjs'):
        return {'frontend'}
    if path.startswith('src/'):
        # Public Python fixtures exercise shared backend behavior. Avoid a speculative import graph.
        return set(DOMAINS)
    if path.startswith('tests/'):
        # Native/installed harnesses import test helpers; all non-UI fixtures are shared.
        return set(DOMAINS)
    if path.startswith('packaging/dsh/') or path == 'scripts/prepare_dsh_runtime.py':
        return {'python', 'native', 'installed'}
    if path == 'scripts/run_offline_tests.py':
        return set(DOMAINS)
    # Licenses, package rules, scripts, dependency declarations, and unknown paths fail full.
    return set(DOMAINS)


def execution_recipe(flow, domain):
    job = flow['jobs'][domain]
    steps = []
    for step in job['steps']:
        if step.get('id') in {'ci_identity', 'ci_proof', 'ci_upload', 'ci_proof_upload'}:
            continue
        value = {key: step[key] for key in ('uses', 'with', 'run', 'env', 'shell', 'working-directory',
                                           'if', 'continue-on-error') if key in step}
        if value.get('uses', '').startswith('actions/checkout@'):
            value['with'] = {key: item for key, item in value.get('with', {}).items() if key != 'ref'}
        steps.append(value)
    return {'workflow_env': flow.get('env', {}), 'workflow_defaults': flow.get('defaults', {}),
            'runs-on': job['runs-on'], 'defaults': job.get('defaults', {}),
            'env': job.get('env', {}), 'container': job.get('container'),
            'services': job.get('services'), 'steps': steps}


def fingerprint(rows, flow, domain):
    inputs = sorted((row['path'], row['mode'], row['blob']) for row in rows
                    if domain in domains_for(row['path']))
    return sha256({'format': FORMAT, 'domain': domain, 'inputs': inputs,
                   'execution': execution_recipe(flow, domain)})


def fresh(timestamp, now):
    if not isinstance(timestamp, str):
        return False
    try:
        age = now - datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        return timedelta() <= age <= TTL
    except (TypeError, ValueError):
        return False


def proof_zip(raw):
    if len(raw) > 65536:
        raise ValueError('Oversized proof ZIP')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != 'proof.json':
            raise ValueError('Unexpected proof ZIP entry')
        entry = entries[0]
        if entry.file_size > 8192 or entry.flag_bits & 1 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError('Unsafe proof ZIP entry')
        value = json.loads(archive.read(entry))
        if not isinstance(value, dict):
            raise ValueError('Proof must be an object')
        return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    """Only GitHub API gets the token; signed downloads use a new unauthenticated request."""
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid repository')
        self.repository, self.token = repository, token
        self.opener = urllib.request.build_opener(NoRedirect())
        self.requests = 0
        self.deadline = time.monotonic() + 90

    def _read(self, url, *, authenticated, limit):
        self.requests += 1
        if self.requests > 60 or time.monotonic() >= self.deadline:
            raise ValueError('Bounded evidence lookup exhausted')
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError('Unsafe evidence URL')
        host = parsed.hostname or ''
        if authenticated:
            if host != 'api.github.com':
                raise ValueError('Token only allowed at GitHub API')
        elif not (host.endswith('.blob.core.windows.net') or host.endswith('.actions.githubusercontent.com')):
            raise ValueError('Unapproved artifact download host')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(url, headers=headers)
        try:
            response = self.opener.open(request, timeout=min(10, max(1, self.deadline-time.monotonic())))
        except urllib.error.HTTPError as exc:
            if authenticated and exc.code == 302:
                return self._read(exc.headers['Location'], authenticated=False, limit=limit)
            raise ValueError('Evidence request unavailable') from None
        with response:
            raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Oversized evidence response')
        return raw

    def data(self, path):
        return self._read('https://api.github.com/repos/' + self.repository + '/' + path,
                          authenticated=True, limit=2_000_000)

    def get(self, path):
        value = json.loads(self.data(path))
        if not isinstance(value, dict):
            raise ValueError('API response must be an object')
        return value


def eligible_run(run, *, repository, head_repository, pr_number, base_branch, workflow_id, now,
                 merged_prs=None):
    if not isinstance(run, dict) or not isinstance(run.get('repository'), dict) or not isinstance(run.get('head_repository'), dict):
        return False
    if run.get('status') != 'completed' or run.get('conclusion') not in {'success', 'failure', 'timed_out'}:
        return False
    if run.get('workflow_id') != workflow_id or run.get('repository', {}).get('full_name') != repository:
        return False
    if not fresh(run.get('updated_at'), now):
        return False
    if run.get('event') == 'push':
        return (run.get('head_branch') == base_branch
                and run.get('head_repository', {}).get('full_name') == repository)
    if run.get('event') != 'pull_request' or not isinstance(run.get('pull_requests'), list):
        return False
    source = run['head_repository']['full_name']
    if not isinstance(source, str) or not source:
        return False
    linked = merged_prs or {}
    return any(isinstance(row, dict) and type(row.get('number')) is int and row['number'] > 0 and (
        (isinstance(pr_number, int) and pr_number > 0 and row.get('number') == pr_number and source == head_repository)
        or (row['number'] in linked and linked[row['number']] == source)) for row in run['pull_requests'])


def merged_pr_sources(client, run, context, cache):
    """Only the PR GitHub says produced this exact main push; no ancestor inference."""
    if context.get('event') != 'push' or run.get('event') != 'pull_request':
        return {}
    rows = run.get('pull_requests')
    if not isinstance(rows, list):
        return {}
    result = {}
    for row in rows[:1]:
        number = row.get('number') if isinstance(row, dict) else None
        if not isinstance(number, int) or number <= 0:
            continue
        if number not in cache:
            cache[number] = client.get(f'pulls/{number}')
        pull = cache[number]
        if (pull.get('merged') is True and pull.get('merge_commit_sha') == context['sha']
                and pull.get('base', {}).get('ref') == context['base_branch']
                and pull.get('base', {}).get('repo', {}).get('full_name') == client.repository):
            result[number] = pull['head']['repo']['full_name']
    return result


def validate_proof(proof, artifact, run, jobs, *, repository, domain, expected, now):
    if not all(isinstance(value, dict) for value in (proof, artifact, run)) or not isinstance(jobs, list):
        raise ValueError('Malformed proof metadata')
    origin = proof.get('origin', {})
    if not isinstance(origin, dict) or not isinstance(artifact.get('workflow_run'), dict):
        raise ValueError('Malformed proof origin')
    if (proof.get('format') != FORMAT or proof.get('state') != 'executed'
            or proof.get('domain') != domain or proof.get('fingerprint') != expected
            or origin.get('repository') != repository or origin.get('workflow') != WORKFLOW
            or origin.get('run') != run['id'] or origin.get('attempt') != run['run_attempt']):
        raise ValueError('Proof identity mismatch')
    if (artifact.get('expired') or artifact.get('name') != f"ci-proof-{domain}-attempt-{run['run_attempt']}"
            or artifact.get('workflow_run', {}).get('id') != run['id']
            or artifact.get('workflow_run', {}).get('head_sha') != run['head_sha']):
        raise ValueError('Artifact owner mismatch')
    matches = [job for job in jobs if isinstance(job, dict) and job.get('name') == origin.get('job_name')]
    if len(matches) != 1:
        raise ValueError('Ambiguous proof job')
    job = matches[0]
    if (job.get('run_id') != run['id'] or job.get('run_attempt') != run['run_attempt']
            or job.get('status') != 'completed' or job.get('conclusion') != 'success'
            or not fresh(job.get('completed_at'), now)):
        raise ValueError('Domain was not executed successfully')
    if not isinstance(job.get('steps'), list):
        raise ValueError('Malformed job steps')
    markers = [step for step in job['steps'] if isinstance(step, dict) and step.get('name') == 'Emit executed domain proof']
    if len(markers) != 1 or markers[0].get('conclusion') != 'success':
        raise ValueError('Missing executed proof step')
    if not re.fullmatch(r'[0-9a-f]{40}', proof.get('sha', '')) or not re.fullmatch(r'[0-9a-f]{40}', proof.get('tree', '')):
        raise ValueError('Invalid Git identity')
    return {**origin, 'job_id': job['id'], 'sha': proof['sha'], 'tree': proof['tree'],
            'kind': 'executed', 'completed_at': job['completed_at']}


def verify_checkout(client, origin, domain, expected, jobs):
    commit = client.get('git/commits/' + origin['sha'])
    if not isinstance(commit.get('tree'), dict) or commit['tree'].get('sha') != origin['tree']:
        raise ValueError('Tested tree mismatch')
    log = client.data(f"actions/jobs/{origin['job_id']}/logs").decode('utf-8', 'replace')
    if 'Testing exact source ' + origin['sha'] not in log:
        raise ValueError('Checkout identity absent from executed job')
    remote = client.get(f"contents/{WORKFLOW}?ref={origin['sha']}")
    flow = json.loads(base64.b64decode(remote['content']))
    domain_job = flow['jobs'][domain]
    if domain_job['name'] != origin['job_name']:
        raise ValueError('Proof job does not match the tested workflow')
    observed = next(job['steps'] for job in jobs if job['id'] == origin['job_id'])
    for step in domain_job['steps']:
        if step.get('id') in {'ci_upload', 'ci_proof_upload'}:
            continue
        matches = [item for item in observed if isinstance(item, dict) and item.get('name') == step['name']]
        if len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('conclusion') != 'success':
            raise ValueError('A required execution step did not succeed')
    tree = client.get('git/trees/' + origin['tree'] + '?recursive=1')
    if tree.get('truncated'):
        raise ValueError('Truncated input tree')
    rows = [{'path': row['path'], 'mode': row['mode'], 'blob': row['sha']}
            for row in tree['tree'] if row['type'] == 'blob']
    if fingerprint(rows, flow, domain) != expected:
        raise ValueError('Remote input/recipe does not prove the claimed fingerprint')


def legacy_origin(client, run, flow, expected, now):
    """One explicitly reviewed old run; never promote arbitrary historical workflows."""
    if run['id'] != LEGACY['run'] or run.get('run_attempt') != LEGACY['attempt']:
        return {}
    job = client.get(f"actions/jobs/{LEGACY['job']}")
    if (job.get('run_id') != run['id'] or job.get('run_attempt') != 1
            or job.get('status') != 'completed'):
        return {}
    source = client.get(f"contents/{WORKFLOW}?ref={LEGACY['sha']}")
    if source.get('sha') != LEGACY['workflow_blob']:
        return {}
    previous = json.loads(base64.b64decode(source['content']))
    commit = client.get('git/commits/' + LEGACY['sha'])
    if not isinstance(commit.get('tree'), dict) or commit['tree'].get('sha') != LEGACY['tree']:
        return {}
    log = client.data(f"actions/jobs/{LEGACY['job']}/logs").decode('utf-8', 'replace')
    if not re.search(r'log -1 --format=[\'\"]?%H[^\n]*\n[^\n]*' + LEGACY['sha'], log):
        return {}
    tree = client.get('git/trees/' + LEGACY['tree'] + '?recursive=1')
    if tree.get('truncated'):
        return {}
    rows = [{'path': row['path'], 'mode': row['mode'], 'blob': row['sha']}
            for row in tree['tree'] if row['type'] == 'blob']
    old_steps = {step['name']: step for step in previous['jobs']['offline']['steps']}
    expanded = json.loads(json.dumps(previous).replace('${{ matrix.os }}', 'windows-2022')
                          .replace('${{ matrix.python }}', '3.13').replace('${{ matrix.arch }}', 'x64')
                          .replace('${{ matrix.dsh }}', 'win32-x64'))
    old_environment = {key: value for key, value in execution_recipe(expanded, 'offline').items() if key != 'steps'}
    observed = job.get('steps', [])
    result = {}
    for domain in DOMAINS:
        required = LEGACY_STEPS[domain]
        evidence = [step for step in observed if step.get('name') in required]
        if (len(evidence) != len(required) or len({s['name'] for s in evidence}) != len(required)
                or any(s.get('status') != 'completed' or s.get('conclusion') != 'success'
                       or not fresh(s.get('completed_at'), now) for s in evidence)):
            continue
        # The new Python/Frontend commands only partition the old unfiltered tests collection.
        actual = {step['name']: step for step in flow['jobs'][domain]['steps']
                  if step.get('id') not in {'ci_identity', 'ci_proof', 'ci_upload', 'ci_proof_upload'}}
        compatible = ({key: value for key, value in execution_recipe(flow, domain).items() if key != 'steps'}
                      == old_environment)
        for name, step in actual.items():
            if name in {'Python domain tests', 'Frontend Python contracts'}:
                target = python_command(domain)
                compatible &= step == {'name': name, 'run': target}
            elif name in old_steps:
                old = json.loads(json.dumps(old_steps[name]).replace('${{ matrix.python }}', '3.13')
                                 .replace('${{ matrix.arch }}', 'x64').replace('${{ matrix.dsh }}', 'win32-x64'))
                new = dict(step)
                if name == 'Checkout without saved credentials':
                    new['with'] = {k: v for k, v in new['with'].items() if k != 'ref'}
                compatible &= old == new
            else:
                compatible = False
        if compatible and fingerprint(rows, flow, domain) == expected[domain]:
            result[domain] = {'kind': 'legacy_verified_steps', 'repository': client.repository,
                'workflow': WORKFLOW, 'run': run['id'], 'attempt': 1, 'job_id': job['id'],
                'sha': LEGACY['sha'], 'tree': LEGACY['tree'], 'workflow_blob': LEGACY['workflow_blob'],
                'steps': list(required), 'completed_at': max(s['completed_at'] for s in evidence)}
    return result


def python_command(domain):
    prefix = 'python -I -X utf8 scripts/run_offline_tests.py --output "$env:RUNNER_TEMP/'
    if domain == 'python':
        return prefix + 'offline" -- tests ' + ' '.join('--ignore=' + path for path in (*FRONTEND_TESTS, *CONTRACT_TESTS))
    return prefix + 'frontend-python" ' + ' '.join(FRONTEND_TESTS)


def select(rows, flow, *, client=None, context=None, force_full=False, now=None):
    now = now or datetime.now(UTC)
    expected = {domain: fingerprint(rows, flow, domain) for domain in DOMAINS}
    plan = {'format': FORMAT, 'domains': {domain: {'run': True, 'fingerprint': expected[domain],
            'reason': 'no_matching_success', 'origin': None} for domain in DOMAINS}}
    if (force_full or not context or context.get('event') not in {'pull_request', 'push'}
            or (context.get('event') == 'push' and
                (context.get('ref') != 'refs/heads/main' or context.get('base_branch') != 'main'))):
        for item in plan['domains'].values():
            item['reason'] = 'forced_full'
        return plan
    if client is None:
        return plan
    try:
        workflow = client.get('actions/workflows/ci.yml')
        runs = client.get(f"actions/workflows/{workflow['id']}/runs?per_page=12")['workflow_runs']
        with suppress(ValueError, KeyError, TypeError, AttributeError, OSError):
            base = urllib.parse.quote(context['base_branch'], safe='')
            base_runs = client.get(f"actions/workflows/{workflow['id']}/runs?branch={base}&event=push&per_page=4")['workflow_runs']
            known = {run['id'] for run in runs if isinstance(run, dict)}
            runs.extend(run for run in base_runs if isinstance(run, dict) and run['id'] not in known)
        # The fixed bridge may have fallen outside the newest bounded page.
        if not any(isinstance(run, dict) and run.get('id') == LEGACY['run'] for run in runs):
            with suppress(ValueError, KeyError, TypeError, AttributeError, OSError, NotImplementedError):
                runs.append(client.get(f"actions/runs/{LEGACY['run']}"))
        pulls = {}
        for run in runs:
            linked = {}
            with suppress(ValueError, KeyError, TypeError, AttributeError, OSError):
                linked = merged_pr_sources(client, run, context, pulls)
            if not eligible_run(run, repository=client.repository, head_repository=context['head_repository'],
                                pr_number=context['pr_number'], base_branch=context['base_branch'],
                                workflow_id=workflow['id'], now=now, merged_prs=linked):
                continue
            try:
                if run['id'] == LEGACY['run']:
                    for domain, origin in legacy_origin(client, run, flow, expected, now).items():
                        if plan['domains'][domain]['run']:
                            plan['domains'][domain].update(run=False, reason='matching_success', origin=origin)
                    continue
                artifacts = client.get(f"actions/runs/{run['id']}/artifacts?per_page=20")['artifacts']
                jobs = client.get(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=30")['jobs']
                for domain in DOMAINS:
                    if not plan['domains'][domain]['run']:
                        continue
                    matches = [a for a in artifacts if isinstance(a, dict) and a.get('name') == f"ci-proof-{domain}-attempt-{run['run_attempt']}" and not a.get('expired')]
                    if len(matches) != 1:
                        continue
                    artifact = matches[0]
                    try:
                        proof = proof_zip(client.data(f"actions/artifacts/{artifact['id']}/zip"))
                        origin = validate_proof(proof, artifact, run, jobs, repository=client.repository,
                                                domain=domain, expected=expected[domain], now=now)
                        verify_checkout(client, origin, domain, expected[domain], jobs)
                        plan['domains'][domain].update(run=False, reason='matching_success', origin=origin)
                    except (ValueError, KeyError, TypeError, AttributeError, OSError, zipfile.BadZipFile, NotImplementedError):
                        continue
            except (ValueError, KeyError, TypeError, AttributeError, OSError, NotImplementedError):
                continue
            if all(not item['run'] for item in plan['domains'].values()):
                break
    except (ValueError, KeyError, TypeError, AttributeError, OSError, NotImplementedError):
        pass  # Unavailable evidence never becomes permission to skip a required domain.
    return plan


def summarize(plan, results, planner_result):
    if planner_result != 'success' or plan.get('format') != FORMAT or set(plan.get('domains', {})) != set(DOMAINS):
        raise ValueError('CI planning/contracts did not succeed')
    lines = []
    for domain in DOMAINS:
        item = plan['domains'][domain]
        if item['run'] is True and results.get(domain) == 'success':
            lines.append(f'{domain}: executed successfully')
        elif item['run'] is False and results.get(domain) == 'skipped' and item.get('origin'):
            origin = item['origin']
            if origin.get('kind') not in {'executed', 'legacy_verified_steps'} or not origin.get('job_id'):
                raise ValueError('Missing original successful execution')
            lines.append(f"{domain}: reused run {origin['run']} / job {origin['job_id']} / attempt {origin['attempt']} ({origin['kind']})")
        else:
            raise ValueError('Required domain did not succeed: ' + domain)
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'proof', 'summary'))
    parser.add_argument('--domain', choices=DOMAINS)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--force-full', default='false')
    args = parser.parse_args()
    flow = json.loads((ROOT / WORKFLOW).read_text(encoding='utf-8'))
    if args.action == 'summary':
        message = summarize(json.loads(os.environ['CI_PLAN']), json.loads(os.environ['CI_RESULTS']),
                            os.environ['CI_PLANNER_RESULT'])
        print(message)
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
            stream.write(message + '\n\nNo publication or deployment is authorized.\n')
        return
    rows = tracked_inputs()
    sha, tree = git('rev-parse', 'HEAD').strip(), git('rev-parse', 'HEAD^{tree}').strip()
    if sha != os.environ['GITHUB_SHA']:
        raise ValueError('Checkout differs from workflow SHA')
    if args.action == 'proof':
        actual = fingerprint(rows, flow, args.domain)
        if actual != os.environ['CI_EXPECTED_FINGERPRINT']:
            raise ValueError('Execution inputs differ from selected inputs')
        proof = {'format': FORMAT, 'state': 'executed', 'sha': sha, 'tree': tree,
                 'domain': args.domain, 'fingerprint': actual, 'origin': {
                     'repository': os.environ['GITHUB_REPOSITORY'], 'workflow': WORKFLOW,
                     'run': int(os.environ['GITHUB_RUN_ID']), 'attempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
                     'job_name': flow['jobs'][args.domain]['name']}}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
        return
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    pr = event.get('pull_request', {})
    context = {'event': os.environ['GITHUB_EVENT_NAME'], 'pr_number': event.get('number'),
               'head_repository': pr.get('head', {}).get('repo', {}).get('full_name'),
               'base_branch': pr.get('base', {}).get('ref'), 'sha': sha, 'ref': os.environ.get('GITHUB_REF')}
    if context['event'] == 'push':
        context.update(head_repository=os.environ['GITHUB_REPOSITORY'], pr_number=None,
                       base_branch=context['ref'].removeprefix('refs/heads/'))
    token = os.environ.get('GITHUB_TOKEN')
    client = GitHub(os.environ['GITHUB_REPOSITORY'], token) if token else None
    plan = select(rows, flow, client=client, context=context, force_full=args.force_full.lower() == 'true')
    plan.update(sha=sha, tree=tree)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
        stream.write('plan=' + json.dumps(plan, separators=(',', ':')) + '\n')
        for domain, item in plan['domains'].items():
            stream.write(f"{domain}={str(item['run']).lower()}\n{domain}_hash={item['fingerprint']}\n")
    print(json.dumps({domain: item['reason'] for domain, item in plan['domains'].items()}))


if __name__ == '__main__':
    main()
