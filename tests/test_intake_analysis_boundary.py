"""Synthetic intake remains usable with the entire old analysis package absent."""
import os
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest


def test_storage_imports_and_reopens_without_analysis_package(tmp_path):
    import project_watch

    package_root = str(Path(project_watch.__file__).parent.parent)
    program = '''
import importlib.abc
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
class RejectAnalysis(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'project_watch.analysis' or fullname.startswith('project_watch.analysis.'):
            raise AssertionError('base intake attempted old analysis import: ' + fullname)
sys.meta_path.insert(0, RejectAnalysis())
from project_watch.repository import ProjectRepository
from project_watch.intake_worker import IntakeWorker
from project_watch.financial_intake import map_financial_fragments
from project_watch.operating_intake import map_operating_fragments
from project_watch.source_topics import impact_topic, review_topics
repo = ProjectRepository(Path(sys.argv[2]) / 'synthetic.sqlite3')
project = repo.create_project('Synthetic boundary')
assert ProjectRepository(repo.db_path).get_project(project.id).name == 'Synthetic boundary'
assert map_financial_fragments('empty.txt', []).candidates == ()
assert map_operating_fragments('empty.txt', []).candidates == ()
assert review_topics(None, ()) == ()
assert impact_topic('unknown', '财务表现') == '财务表现'
assert not any(n.startswith('project_watch.analysis') for n in sys.modules)
print('isolated intake imports and reopen passed')
'''
    result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', program, package_root, str(tmp_path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=45,
                            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert result.returncode == 0, result.stderr


def test_legacy_aliases_keep_class_and_function_identity():
    from project_watch import financial_intake, operating_intake, source_topics
    from project_watch.analysis import (
        financial_mapping,
        financial_projection,
        operating_mapping,
        source_update,
    )
    from project_watch.financial_vocabulary import FINANCIAL_METRIC_NAMES

    for old, new in ((financial_mapping, financial_intake), (operating_mapping, operating_intake),
                     (source_update, source_topics)):
        for name in old.__all__:
            assert getattr(old, name) is getattr(new, name)
    assert financial_projection.FINANCIAL_METRIC_NAMES is FINANCIAL_METRIC_NAMES
    assert financial_mapping._number is financial_intake._number


def test_base_finance_validation_retains_exact_numbers_and_source_binding():
    from project_watch.domain import FragmentInput
    from project_watch.financial_intake import (
        map_financial_fragments,
        validate_financial_suggestions,
    )

    rows = [FragmentInput(1, 'sheet 合并利润表 row 1', 'A1=单位：万元'),
            FragmentInput(2, 'sheet 合并利润表 row 2',
                          'A2=净利润 | B2[header:2025年实际]=(1,234.50) | C2[header:2026年预测]=2000')]
    mapped = map_financial_fragments('synthetic.xlsx', rows)
    assert [(c.number, c.basis) for c in mapped.candidates] == [(Decimal('-1234.50'), 'actual'),
                                                              (Decimal('2000'), 'forecast')]
    validate_financial_suggestions('synthetic.xlsx', rows, mapped.candidates)
    with pytest.raises(ValueError, match='located spreadsheet'):
        validate_financial_suggestions('synthetic.xlsx', rows,
                                       (replace(mapped.candidates[0], number=Decimal('999')),))


def test_base_operating_validation_does_not_infer_utilization():
    from project_watch.domain import FragmentInput
    from project_watch.operating_intake import (
        map_operating_fragments,
        validate_operating_suggestions,
    )

    rows = [FragmentInput(1, 'sheet 产能 row 1', 'A1=产品 | B1=指标 | C1=单位 | D1=2025年实际'),
            FragmentInput(2, 'sheet 产能 row 2', 'A2=合成产品 | B2=产能利用率 | C2=% | D2[header:2025年实际]=0.7')]
    result = map_operating_fragments('synthetic.xlsx', rows)
    assert result.candidates == () and result.issues[0].reason == 'percent_scale_ambiguous'
    rows[1] = replace(rows[1], text=rows[1].text.replace(']=0.7', '][format:percent]=0.7'))
    result = map_operating_fragments('synthetic.xlsx', rows)
    assert result.candidates[0].number == Decimal('70')
    validate_operating_suggestions('synthetic.xlsx', rows, result.candidates)
