"""Generic stored human records must not require old feature entry points."""
import importlib
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

PAIRS = (
    ('holding_snapshots', 'holding_input', ('validated_holding_input',)),
    ('identity_review', 'identity_input', ('VerifiedIdentityInput', 'review_note',
                                         '_public_https_reference', 'verified_identity_input')),
    ('watch_items', 'followup_records', ('watch_item_input', 'watch_review_input',
                                       'watch_change_input', 'watch_change_review_input',
                                       'watch_item_snapshot')),
)


@pytest.mark.parametrize('old,new,names', PAIRS)
def test_record_legacy_aliases(old, new, names):
    previous = importlib.import_module('project_watch.' + old)
    current = importlib.import_module('project_watch.' + new)
    for name in names:
        assert getattr(previous, name) is getattr(current, name)


def test_repository_works_with_old_record_modules_blocked(tmp_path):
    import project_watch
    package_parent = str(Path(project_watch.__file__).resolve().parent.parent)
    code = '''
import importlib.abc
import sys
from pathlib import Path
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'project_watch.holding_snapshots', 'project_watch.identity_review',
                        'project_watch.watch_items', 'project_watch.tasks', 'project_watch.candidate_metrics'}:
            raise AssertionError('legacy import: ' + fullname)
sys.meta_path.insert(0, Block())
from project_watch.repository import ProjectRepository
from project_watch.holding_input import validated_holding_input
from project_watch.identity_input import verified_identity_input
from project_watch.followup_records import watch_item_input, watch_change_input
repo = ProjectRepository(Path(sys.argv[1]) / 'synthetic.sqlite3')
project = repo.create_project('Synthetic Boundary')
review = repo.verify_project_identity(project.id, legal_name='Synthetic Boundary Limited',
    registration_code=None, evidence_source_id=None, evidence_locator=None,
    evidence_url='https://example.org/company', note='Synthetic manual verification',
    expected_version=0, actor_user_id='synthetic')
assert review.version == 1
assert ProjectRepository(repo.db_path).get_project(project.id).identity_status == 'verified'
assert watch_item_input(' Name ', 'Question', None, None) == ('Name', 'name', 'Question', None, None)
assert watch_change_input('resolved', 'Question', None, None, 'Synthetic done')[-1] == 'Synthetic done'
'''
    code = 'import sys\nsys.path.insert(0, ' + repr(package_parent) + ')\n' + code
    result = subprocess.run([sys.executable, '-I', '-c', code, str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('url', ['http://example.org', 'https://localhost',
                               'https://127.0.0.1', 'https://u:p@example.org'])
def test_core_identity_reference_remains_bounded(url):
    from project_watch.identity_input import verified_identity_input
    with pytest.raises(ValueError, match='HTTPS'):
        verified_identity_input(legal_name='Synthetic Company', registration_code=None,
                                evidence_source_id=None, evidence_locator=None,
                                evidence_url=url, note='Synthetic review note')


@pytest.mark.parametrize('function,args', [
    ('watch_review_input', ('invented', 'reviewer', None)),
    ('watch_change_input', ('invented', 'question', None, None, 'reason')),
    ('watch_change_review_input', ('invented', 'reviewer', None)),
])
def test_core_followup_rejects_invalid_actions(function, args):
    from project_watch import followup_records
    with pytest.raises(ValueError):
        getattr(followup_records, function)(*args)


def test_core_holding_keeps_literal_units_and_exact_locator():
    from project_watch.holding_input import validated_holding_input
    args = dict(fund_name='Synthetic Fund', as_of=date(2020, 1, 1),
                source_locator='line 1', fragment_locator='line 1',
                fragment_text='Synthetic Fund 1,100 万元 8.5% 1430 万元',
                invested_cost_wan='1100', ownership_pct='8.5', current_value_wan='1430')
    assert validated_holding_input(**args)[1:] == (1100, 8.5, 1430)
    for change in ({'current_value_wan': '9999'}, {'source_locator': 'invented'},
                   {'fragment_text': 'Synthetic Fund 1100 元 8.5% 1430 元'},
                   {'ownership_pct': True}, {'ownership_pct': '101'},
                   {'fund_name': 'Unrelated Fund'}):
        with pytest.raises(ValueError):
            validated_holding_input(**(args | change))
