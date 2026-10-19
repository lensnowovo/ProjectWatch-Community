"""Base candidate intake must not load the old task/numeric entry points."""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from project_watch.domain import FragmentInput
from project_watch.repository import ProjectRepository


def test_base_imports_without_legacy_task_modules():
    code = '''
import importlib.abc
import sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'project_watch.tasks', 'project_watch.candidate_metrics'}:
            raise AssertionError('legacy import: ' + fullname)
sys.meta_path.insert(0, Block())
from project_watch.executors import ModelCandidateExecutor
from project_watch.model_configuration import ModelConfiguration
from project_watch.studio.local_base import create_local_base
from project_watch.evidence_proposals import TaskService, EvidenceConflictError
from project_watch.literal_metrics import validate_candidate_metric
assert not {'project_watch.tasks', 'project_watch.candidate_metrics'} & sys.modules.keys()
'''
    # Installed runs use -I in the parent; propagate its actual package, never src.
    import project_watch
    package_parent = str(Path(project_watch.__file__).resolve().parent.parent)
    code = 'import sys\nsys.path.insert(0, ' + repr(package_parent) + ')\n' + textwrap.dedent(code)
    result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_legacy_aliases_preserve_identity():
    from project_watch import candidate_metrics, evidence_proposals, literal_metrics, tasks
    assert candidate_metrics.validate_candidate_metric is literal_metrics.validate_candidate_metric
    assert candidate_metrics._literal_numbers is literal_metrics._literal_numbers
    assert tasks.TaskService is evidence_proposals.TaskService
    assert tasks.EvidenceConflictError is evidence_proposals.EvidenceConflictError
    assert tasks.coverage_outcome is evidence_proposals.coverage_outcome


@pytest.fixture
def prepared(tmp_path):
    repo = ProjectRepository(tmp_path / 'synthetic.sqlite3')
    project = repo.create_project('Synthetic Candidate Boundary')
    source = repo.add_source(project.id, kind='document', reference='synthetic.txt',
                             content_sha256='a' * 64)
    repo.enqueue_intake(project.id, source.id)
    job = repo.claim_next_intake()
    repo.complete_intake(job.id, [FragmentInput(ordinal=1, locator='line 1',
                                              text='2025 revenue 1,200.50 万元')])
    return repo, project, source


def proposal(source):
    return dict(source_id=source.id, expected_source_sha256=source.content_sha256,
                source_locator='line 1', subject='财务表现', statement='营业收入',
                number='1200.50', unit='万元', period='2025')


def test_core_submission_and_coverage_survive_reopen(prepared):
    from project_watch.evidence_proposals import TaskService
    repo, project, source = prepared
    service = TaskService(repo)
    item = service.propose_candidate(project.id, **proposal(source))
    assert item.status == 'candidate'
    assert str(item.number) == '1200.50'
    assert not repo.list_observations(project.id, status='confirmed')
    task = service.run_coverage(project.id, source_ids=(source.id,), idempotency_key='coverage')
    assert task.outcome['candidate_count'] == 1 and task.outcome['confirmed_count'] == 0
    assert service.run_coverage(project.id, source_ids=(source.id,), idempotency_key='coverage') == task
    assert ProjectRepository(repo.db_path).get_execution_task(project.id, task.id) == task


@pytest.mark.parametrize('change,exception', [
    ({'expected_source_sha256': 'b' * 64}, 'conflict'),
    ({'source_locator': 'invented'}, 'value'),
    ({'number': '9999'}, 'value'),
    ({'number': 'NaN'}, 'value'),
    ({'period': '2025-Q5'}, 'value'),
    ({'number': True}, 'value'),
    ({'number': 1200.5}, 'value'),
])
def test_invalid_candidate_does_not_write(prepared, change, exception):
    from project_watch.evidence_proposals import EvidenceConflictError, TaskService
    repo, project, source = prepared
    args = proposal(source) | change
    with pytest.raises(EvidenceConflictError if exception == 'conflict' else ValueError):
        TaskService(repo).propose_candidate(project.id, **args)
    assert repo.list_observations(project.id) == []


def test_foreign_project_cannot_submit(prepared):
    from project_watch.evidence_proposals import TaskService
    repo, project, source = prepared
    foreign = repo.create_project('Synthetic Other')
    with pytest.raises(ValueError, match='another project'):
        TaskService(repo).propose_candidate(foreign.id, **proposal(source))
    assert repo.list_observations(project.id) == []
    assert repo.list_observations(foreign.id) == []
