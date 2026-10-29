"""Synthetic evidence only: never calls GitHub, providers, or an existing instance."""
import base64
import copy
import io
import json
import urllib.error
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import ci_scope as ci

FLOW = json.loads((Path(__file__).parents[1] / ci.WORKFLOW).read_text())
NOW = datetime(2026, 10, 29, 4, tzinfo=UTC)
STAMP = (NOW - timedelta(hours=1)).isoformat()
REPO = 'synthetic/project'
ROWS = [{'path': p, 'mode': '100644', 'blob': str(i) * 40} for i, p in enumerate((
    'src/project_watch/studio/pipeline.py', 'frontend/src/App.tsx', 'docs/support.md',
    'tests/test_studio.py', 'tests/test_web_preview.py', ci.WORKFLOW), 1)]
CONTEXT = {'event': 'pull_request', 'pr_number': 5, 'head_repository': REPO, 'base_branch': 'main'}


def fingerprints(rows=ROWS, flow=FLOW):
    return {d: ci.fingerprint(rows, flow, d) for d in ci.DOMAINS}


@pytest.mark.parametrize(('path', 'changed'), [
    ('frontend/src/App.tsx', {'frontend', 'installed'}),
    ('tests/test_web_preview.py', {'frontend'}),
    ('tests/test_studio.py', set(ci.DOMAINS)),
    ('docs/support.md', set()), (ci.WORKFLOW, set()),
    ('src/project_watch/studio/pipeline.py', set(ci.DOMAINS)),
    ('unknown/new-input', set(ci.DOMAINS)),
])
def test_actual_input_domains_and_shared_test_helpers(path, changed):
    rows = copy.deepcopy(ROWS)
    matching = next((row for row in rows if row['path'] == path), None)
    if matching:
        matching['blob'] = 'f' * 40
    else:
        rows.append({'path': path, 'mode': '100644', 'blob': 'f' * 40})
    assert {d for d in ci.DOMAINS if fingerprints()[d] != fingerprints(rows)[d]} == changed


@pytest.mark.parametrize('operation', ['delete', 'rename', 'mode', 'merged_base_content'])
def test_complete_tree_detects_changes_without_pr_base_diff(operation):
    rows = copy.deepcopy(ROWS)
    if operation == 'delete':
        rows.pop(0)
    elif operation == 'rename':
        rows[0]['path'] += '.renamed'
    elif operation == 'mode':
        rows[0]['mode'] = '100755'
    else:
        rows[0]['blob'] = 'b' * 40
    assert all(fingerprints(rows)[d] != fingerprints()[d] for d in ci.DOMAINS)


def test_ci_grouping_names_and_time_budgets_do_not_change_execution_inputs():
    flow = copy.deepcopy(FLOW)
    for domain in ci.DOMAINS:
        flow['jobs'][domain]['name'] += ' renamed'
        flow['jobs'][domain]['timeout-minutes'] -= 1
        for step in flow['jobs'][domain]['steps']:
            step['name'] += ' display only'
    assert fingerprints(flow=flow) == fingerprints()


@pytest.mark.parametrize('mutation', ['command', 'env', 'if', 'continue-on-error', 'runner', 'python', 'node'])
def test_execution_semantics_invalidate_the_affected_domain(mutation):
    flow = copy.deepcopy(FLOW)
    job = flow['jobs']['native']
    step = next(s for s in job['steps'] if s['name'] == 'Native DSH with synthetic model gateway')
    if mutation == 'command':
        step['run'] += ' tests/test_new_contract.py'
    elif mutation in {'env', 'if', 'continue-on-error'}:
        step[mutation] = {'PYTHONOPTIMIZE': '1'} if mutation == 'env' else 'false'
    elif mutation == 'runner':
        job['runs-on'] = 'windows-2025'
    else:
        setup = next(s for s in job['steps'] if s['name'].lower() == mutation)
        setup['with'][mutation + '-version'] = 'different'
    assert fingerprints(flow=flow)['native'] != fingerprints()['native']
    assert all(fingerprints(flow=flow)[d] == fingerprints()[d] for d in ci.DOMAINS if d != 'native')


@pytest.mark.parametrize('key', ['env', 'defaults'])
def test_workflow_execution_environment_is_shared_input(key):
    flow = copy.deepcopy(FLOW)
    flow[key] = {'PYTHONOPTIMIZE': '1'} if key == 'env' else {'run': {'shell': 'different'}}
    assert all(fingerprints(flow=flow)[d] != fingerprints()[d] for d in ci.DOMAINS)


def zipped(value, name='proof.json'):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(name, json.dumps(value))
    return stream.getvalue()


class Evidence:
    repository = REPO

    def __init__(self):
        self.run = {'id': 100, 'run_attempt': 1, 'workflow_id': 10, 'status': 'completed',
                    'conclusion': 'success', 'repository': {'full_name': REPO},
                    'head_repository': {'full_name': REPO}, 'event': 'pull_request',
                    'pull_requests': [{'number': 5}], 'head_branch': 'feature',
                    'head_sha': 'a' * 40, 'updated_at': STAMP}
        self.flow = copy.deepcopy(FLOW)
        self.rows = copy.deepcopy(ROWS)
        self.jobs, self.artifacts, self.proofs = [], [], {}
        for i, domain in enumerate(ci.DOMAINS, 1):
            name = FLOW['jobs'][domain]['name']
            self.jobs.append({'id': i, 'run_id': 100, 'run_attempt': 1, 'name': name,
                              'status': 'completed', 'conclusion': 'success', 'completed_at': STAMP,
                              'steps': [{'name': s['name'], 'status': 'completed', 'conclusion': 'success',
                                         'completed_at': STAMP} for s in FLOW['jobs'][domain]['steps']]})
            self.artifacts.append({'id': i, 'name': f'ci-proof-{domain}-attempt-1', 'expired': False,
                                   'workflow_run': {'id': 100, 'head_sha': 'a' * 40}})
            self.proofs[i] = {'format': ci.FORMAT, 'state': 'executed', 'sha': 'a' * 40,
                             'tree': 'b' * 40, 'domain': domain, 'fingerprint': fingerprints()[domain],
                             'origin': {'repository': REPO, 'workflow': ci.WORKFLOW, 'run': 100,
                                        'attempt': 1, 'job_name': name}}

    def get(self, path):
        if path == 'actions/workflows/ci.yml':
            return {'id': 10}
        if path == 'actions/workflows/10/runs?per_page=12':
            return {'workflow_runs': [self.run]}
        if path == 'actions/runs/100/artifacts?per_page=20':
            return {'artifacts': self.artifacts}
        if path == 'actions/runs/100/attempts/1/jobs?per_page=30':
            return {'jobs': self.jobs}
        if path == 'git/commits/' + 'a' * 40:
            return {'tree': {'sha': 'b' * 40}}
        if path == 'contents/' + ci.WORKFLOW + '?ref=' + 'a' * 40:
            return {'content': base64.b64encode(json.dumps(self.flow).encode()).decode()}
        if path == 'git/trees/' + 'b' * 40 + '?recursive=1':
            return {'truncated': False, 'tree': [{'path': r['path'], 'mode': r['mode'], 'sha': r['blob'], 'type': 'blob'} for r in self.rows]}
        raise ValueError('Synthetic unavailable endpoint')

    def data(self, path):
        if path.startswith('actions/artifacts/'):
            return zipped(self.proofs[int(path.split('/')[2])])
        if path.startswith('actions/jobs/'):
            return ('Testing exact source ' + 'a' * 40).encode()
        raise ValueError('Synthetic unavailable download')


def selected(evidence, **kwargs):
    return ci.select(ROWS, FLOW, client=evidence, context=CONTEXT, now=NOW, **kwargs)


def test_successful_execution_reused_without_creating_a_skipped_proof_chain():
    plan = selected(Evidence())
    assert all(not v['run'] and v['origin']['job_id'] for v in plan['domains'].values())
    assert 'reused run 100' in ci.summarize(plan, dict.fromkeys(ci.DOMAINS, 'skipped'), 'success')


@pytest.mark.parametrize('fault', ['failure', 'cancelled', 'skipped', 'neutral', 'missing_proof',
                                   'wrong_attempt', 'expired', 'wrong_artifact_run', 'reused_proof',
                                   'missing_timestamp', 'bad_origin', 'bad_json_shape', 'duplicate_job',
                                   'required_step_skipped', 'forged_hash', 'forged_recipe'])
def test_bad_domain_evidence_runs_that_domain_instead_of_turning_green(fault):
    e = Evidence()
    if fault in {'failure', 'cancelled', 'skipped', 'neutral'}:
        e.jobs[0]['conclusion'] = fault
    elif fault == 'missing_proof':
        e.artifacts.pop(0)
    elif fault == 'wrong_attempt':
        e.proofs[1]['origin']['attempt'] = 2
    elif fault == 'expired':
        e.jobs[0]['completed_at'] = (NOW - timedelta(days=8)).isoformat()
    elif fault == 'wrong_artifact_run':
        e.artifacts[0]['workflow_run']['id'] = 101
    elif fault == 'reused_proof':
        e.proofs[1]['state'] = 'reused'
    elif fault == 'missing_timestamp':
        e.jobs[0]['completed_at'] = None
    elif fault == 'bad_origin':
        e.proofs[1]['origin'] = None
    elif fault == 'bad_json_shape':
        e.proofs[1] = []
    elif fault == 'duplicate_job':
        e.jobs.append(copy.deepcopy(e.jobs[0]))
    elif fault == 'required_step_skipped':
        next(s for s in e.jobs[0]['steps'] if s['name'] == 'Python domain tests')['conclusion'] = 'skipped'
    elif fault == 'forged_hash':
        e.rows[0]['blob'] = 'c' * 40  # Self-reported fingerprint is unchanged but the tested tree differs.
    else:
        next(s for s in e.flow['jobs']['python']['steps'] if s['name'] == 'Python domain tests')['if'] = 'false'
    plan = selected(e)
    assert plan['domains']['python']['run'] is True


@pytest.mark.parametrize('fault', ['running', 'cancelled', 'other_repo', 'other_workflow', 'other_pr',
                                   'other_fork', 'missing_timestamp'])
def test_unrelated_or_unfinished_run_never_supplies_proof(fault):
    e = Evidence()
    if fault == 'running':
        e.run['status'] = 'in_progress'
    elif fault == 'cancelled':
        e.run['conclusion'] = 'cancelled'
    elif fault == 'other_repo':
        e.run['repository']['full_name'] = 'other/repo'
    elif fault == 'other_workflow':
        e.run['workflow_id'] = 11
    elif fault == 'other_pr':
        e.run['pull_requests'] = [{'number': 6}]
    elif fault == 'other_fork':
        e.run['head_repository']['full_name'] = 'other/fork'
    else:
        e.run['updated_at'] = None
    assert all(v['run'] for v in selected(e)['domains'].values())


def test_completed_run_with_other_domain_failure_preserves_actual_success():
    e = Evidence()
    e.run['conclusion'] = 'failure'
    e.jobs[0]['conclusion'] = 'failure'
    plan = selected(e)
    assert plan['domains']['python']['run']
    assert all(not plan['domains'][d]['run'] for d in ci.DOMAINS[1:])


def test_trusted_main_proof_can_seed_a_new_docs_only_pr():
    e = Evidence()
    e.run.update(event='push', head_branch='main', pull_requests=[])
    assert all(not v['run'] for v in selected(e)['domains'].values())
    e.run['head_branch'] = 'unrelated'
    assert all(v['run'] for v in selected(e)['domains'].values())


def test_first_or_full_or_unavailable_evidence_runs_all():
    assert all(v['run'] for v in selected(Evidence(), force_full=True)['domains'].values())
    assert all(v['run'] for v in ci.select(ROWS, FLOW, context=CONTEXT)['domains'].values())
    assert all(v['run'] for v in ci.select(ROWS, FLOW, client=Evidence(), context={'event': 'workflow_dispatch'})['domains'].values())


@pytest.mark.parametrize('status', ['failure', 'cancelled', 'skipped', 'neutral'])
def test_summary_rejects_any_selected_domain_that_did_not_succeed(status):
    plan = selected(Evidence(), force_full=True)
    results = dict.fromkeys(ci.DOMAINS, 'success')
    results['native'] = status
    with pytest.raises(ValueError):
        ci.summarize(plan, results, 'success')


def test_summary_requires_planner_and_original_proof_for_every_reuse():
    plan = selected(Evidence())
    results = dict.fromkeys(ci.DOMAINS, 'skipped')
    with pytest.raises(ValueError):
        ci.summarize(plan, results, 'failure')
    plan['domains']['native']['origin'] = None
    with pytest.raises(ValueError):
        ci.summarize(plan, results, 'success')


@pytest.mark.parametrize('name', ['../proof.json', '/proof.json', 'sub/proof.json'])
def test_zip_paths_are_never_extracted(name):
    with pytest.raises(ValueError):
        ci.proof_zip(zipped({}, name))


def test_duplicate_large_or_symlink_zip_is_rejected():
    with pytest.raises(ValueError):
        ci.proof_zip(zipped({'padding': 'x' * 9000}))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        entry = zipfile.ZipInfo('proof.json')
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, '{}')
    with pytest.raises(ValueError):
        ci.proof_zip(stream.getvalue())


def test_signed_download_never_receives_github_token():
    client = ci.GitHub(REPO, 'synthetic-token')
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append((request.full_url, dict(request.header_items())))
            assert 0 < timeout <= 10
            if len(seen) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, 'redirect',
                                             {'Location': 'https://synthetic.blob.core.windows.net/proof?signature=synthetic'}, None)
            return io.BytesIO(b'{}')
    client.opener = Opener()
    assert client.data('actions/artifacts/1/zip') == b'{}'
    assert seen[0][1]['Authorization'] == 'Bearer synthetic-token'
    assert 'Authorization' not in seen[1][1]


@pytest.mark.parametrize('url', ['http://synthetic.blob.core.windows.net/a', 'https://evil.test/a',
                                'https://user@synthetic.blob.core.windows.net/a'])
def test_unapproved_redirect_is_rejected_before_download(url):
    client = ci.GitHub(REPO, 'synthetic-token')
    with pytest.raises(ValueError):
        client._read(url, authenticated=False, limit=100)


class LegacyEvidence(Evidence):
    def __init__(self):
        super().__init__()
        self.run.update(id=ci.LEGACY['run'])
        names = {}
        for domain in ci.DOMAINS:
            for step in FLOW['jobs'][domain]['steps']:
                if not step.get('id') and step['name'] not in {'Python domain tests', 'Frontend Python contracts'}:
                    names[step['name']] = copy.deepcopy(step)
        names['Offline Python suite'] = {'name': 'Offline Python suite', 'run': 'python -I -X utf8 scripts/run_offline_tests.py --output "$env:RUNNER_TEMP/offline"'}
        names['Checkout without saved credentials']['with'].pop('ref')
        names['Python']['with'] = {'python-version': '${{ matrix.python }}', 'architecture': '${{ matrix.arch }}'}
        names['Confirm native runner']['env'] = {'EXPECTED_ARCH': '${{ matrix.arch }}'}
        names['Prepare pinned native DSH (no install scripts)']['env'] = {'DSH_TARGET': '${{ matrix.dsh }}'}
        self.old = {'jobs': {'offline': {'runs-on': '${{ matrix.os }}', 'defaults': {'run': {'shell': 'pwsh'}}, 'steps': list(names.values())}}}
        self.job = {'id': ci.LEGACY['job'], 'run_id': ci.LEGACY['run'], 'run_attempt': 1,
                    'status': 'completed', 'conclusion': 'success', 'steps': [
                        {'name': name, 'status': 'completed', 'conclusion': 'success', 'completed_at': STAMP}
                        for name in names]}

    def get(self, path):
        if path == f"actions/jobs/{ci.LEGACY['job']}":
            return self.job
        if path == f"contents/{ci.WORKFLOW}?ref={ci.LEGACY['sha']}":
            return {'sha': ci.LEGACY['workflow_blob'], 'content': base64.b64encode(json.dumps(self.old).encode()).decode()}
        if path == 'git/commits/' + ci.LEGACY['sha']:
            return {'tree': {'sha': ci.LEGACY['tree']}}
        if path == 'git/trees/' + ci.LEGACY['tree'] + '?recursive=1':
            return super().get('git/trees/' + 'b' * 40 + '?recursive=1')
        return super().get(path)

    def data(self, path):
        if path == f"actions/jobs/{ci.LEGACY['job']}/logs":
            return ('git log -1 --format=%H\n2026-10-29T00:00:00Z ' + ci.LEGACY['sha']).encode()
        return super().data(path)


def test_fixed_legacy_bridge_expands_exact_old_matrix_and_reuses_all_finished_domains():
    e = LegacyEvidence()
    origins = ci.legacy_origin(e, e.run, FLOW, fingerprints(), NOW)
    assert set(origins) == set(ci.DOMAINS)
    assert all(v['kind'] == 'legacy_verified_steps' and v['job_id'] == ci.LEGACY['job'] for v in origins.values())


def test_legacy_later_failure_does_not_erase_completed_python_steps():
    e = LegacyEvidence()
    e.run['conclusion'] = e.job['conclusion'] = 'failure'
    next(s for s in e.job['steps'] if s['name'] == 'Native DSH with synthetic model gateway')['conclusion'] = 'failure'
    next(s for s in e.job['steps'] if s['name'] == 'Build full wheel')['conclusion'] = 'skipped'
    assert set(ci.legacy_origin(e, e.run, FLOW, fingerprints(), NOW)) == {'python', 'frontend'}


@pytest.mark.parametrize('mutation', ['runner', 'env', 'defaults', 'command', 'step_env', 'step_if', 'checkout', 'attempt'])
def test_legacy_bridge_cannot_authorize_changed_execution_semantics(mutation):
    e = LegacyEvidence()
    flow = copy.deepcopy(FLOW)
    if mutation == 'runner':
        flow['jobs']['python']['runs-on'] = 'windows-2025'
    elif mutation == 'env':
        flow['env'] = {'PYTHONOPTIMIZE': '1'}
    elif mutation == 'defaults':
        flow['defaults'] = {'run': {'shell': 'other'}}
    elif mutation == 'command':
        next(s for s in flow['jobs']['python']['steps'] if s['name'] == 'Python domain tests')['run'] += ' extra.py'
    elif mutation in {'step_env', 'step_if'}:
        step = next(s for s in flow['jobs']['python']['steps'] if s['name'] == 'Python domain tests')
        step[mutation.removeprefix('step_')] = {'PYTHONOPTIMIZE': '1'} if mutation == 'step_env' else 'false'
    elif mutation == 'checkout':
        e.data = lambda _: b'log contains no tested SHA'
    else:
        e.run['run_attempt'] = 2
    assert 'python' not in ci.legacy_origin(e, e.run, flow, fingerprints(flow=flow), NOW)


MAIN = {'event': 'push', 'pr_number': None, 'head_repository': REPO,
        'base_branch': 'main', 'ref': 'refs/heads/main', 'sha': 'c' * 40}


def main_evidence(evidence, pull):
    original = evidence.get
    def get(path):
        if path == 'pulls/5':
            if pull is None:
                raise ValueError('Synthetic unavailable association')
            return pull
        return original(path)
    evidence.get = get
    return evidence


def merged_pull():
    return {'merged': True, 'merge_commit_sha': MAIN['sha'],
            'base': {'ref': 'main', 'repo': {'full_name': REPO}},
            'head': {'repo': {'full_name': REPO}}}


@pytest.mark.parametrize('source', ['main', 'just_merged_pr'])
def test_main_push_reuses_same_inputs_from_a_trusted_origin(source):
    e = main_evidence(Evidence(), merged_pull())
    if source == 'main':
        e.run.update(event='push', head_branch='main', pull_requests=[])
    plan = ci.select(ROWS, FLOW, client=e, context=MAIN, now=NOW)
    assert all(not value['run'] for value in plan['domains'].values())


@pytest.mark.parametrize('fault', ['unmerged', 'repo', 'ref', 'sha', 'unavailable',
                                   'missing_number', 'wrong_push_ref', 'bad_shape', 'null_source_unmerged'])
def test_main_push_rejects_missing_or_wrong_merge_association(fault):
    pull, context, evidence = merged_pull(), dict(MAIN), Evidence()
    if fault == 'unmerged':
        pull['merged'] = False
    elif fault == 'repo':
        pull['base']['repo']['full_name'] = 'other/repo'
    elif fault == 'ref':
        pull['base']['ref'] = 'other'
    elif fault == 'sha':
        pull['merge_commit_sha'] = 'd' * 40
    elif fault == 'unavailable':
        pull = None
    elif fault == 'missing_number':
        evidence.run['pull_requests'] = [{}]
    elif fault == 'wrong_push_ref':
        context['ref'] = 'refs/heads/other'
    elif fault == 'null_source_unmerged':
        evidence.run['head_repository']['full_name'] = None
        pull['merged'] = False
    else:
        pull['base'] = None
    plan = ci.select(ROWS, FLOW, client=main_evidence(evidence, pull), context=context, now=NOW)
    assert all(value['run'] for value in plan['domains'].values())


@pytest.mark.parametrize('merged', [True, False])
def test_main_push_legacy_bridge_obeys_the_same_verified_merge_association(merged):
    pull = merged_pull()
    pull['merged'] = merged
    e = main_evidence(LegacyEvidence(), pull)
    plan = ci.select(ROWS, FLOW, client=e, context=MAIN, now=NOW)
    assert all(value['run'] is not merged for value in plan['domains'].values())
