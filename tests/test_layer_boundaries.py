"""Offline AST boundary regressions; no product imports or provider calls."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_layer_boundaries import audit, frozen_exceptions, load_map

ROOT = Path(__file__).resolve().parents[1]

# Frozen at Claude d2bd4b5. Removal is allowed; adding a pair needs an explicit
# reviewed change here as well as a reason/removal task in the inventory.
APPROVED_EXCEPTIONS = {
    ('project_watch.studio.business_identity', 'project_watch.studio.product_matching'),
    ('project_watch.studio.comparisons', 'project_watch.studio.race'),
    ('project_watch.studio.pipeline', 'project_watch.studio.competition_research'),
    ('project_watch.studio.pipeline', 'project_watch.studio.landscape'),
    ('project_watch.studio.relationships', 'project_watch.studio.competition'),
    ('project_watch.studio.structured_facts', 'project_watch.studio.race'),
}


def test_repository_exceptions_are_frozen():
    config = load_map(ROOT / 'scripts/layer_map.json')
    assert not frozen_exceptions(config['exceptions'], APPROVED_EXCEPTIONS)


def fixture_graph(tmp_path, source, layers, *, exceptions=()):
    source = {'project_watch.app': '', **source}
    layers = {'project_watch.app': 'App', **layers}
    for module, text in source.items():
        path = tmp_path / 'src' / Path(*module.split('.')).with_suffix('.py')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return {'composition_root': 'project_watch.app', 'modules': layers,
            'exceptions': list(exceptions), 'frontend': {}}


@pytest.mark.parametrize('statement', [
    'import project_watch.insight',
    'from .insight import analyze',
    'def later():\n    from project_watch import insight',
])
def test_core_cannot_import_insight(tmp_path, statement):
    config = fixture_graph(tmp_path, {'project_watch.core': statement, 'project_watch.insight': ''},
                           {'project_watch.core': 'Core', 'project_watch.insight': 'Insight'})
    result = audit(tmp_path, config)
    assert [e.rule for e in result.violations] == ['core_to_insight']
    assert result.violations[0].target == 'project_watch.insight'


def test_contract_cannot_import_implementation(tmp_path):
    config = fixture_graph(tmp_path, {'project_watch.contract': 'from .core import service',
                                     'project_watch.core': ''},
                           {'project_watch.contract': 'Contract', 'project_watch.core': 'Core'})
    assert audit(tmp_path, config).violations[0].rule == 'contract_to_implementation'


def test_only_composition_root_can_wire_insight(tmp_path):
    config = fixture_graph(tmp_path, {'project_watch.app': 'from .insight import service',
                                     'project_watch.other': 'from .insight import service',
                                     'project_watch.insight': ''},
                           {'project_watch.app': 'App', 'project_watch.other': 'Legacy',
                            'project_watch.insight': 'Insight'})
    result = audit(tmp_path, config)
    assert len(result.violations) == 1
    assert result.violations[0].source == 'project_watch.other'
    assert result.violations[0].rule == 'insight_wiring_outside_root'


def test_composition_root_cannot_be_removed_from_registration(tmp_path):
    config = fixture_graph(tmp_path, {}, {})
    config['modules']['project_watch.app'] = 'Core'
    config['composition_root'] = 'project_watch.missing'
    assert not audit(tmp_path, config).ok


def test_nested_relative_import_and_internal_insight_are_resolved(tmp_path):
    config = fixture_graph(tmp_path, {'project_watch.studio.core': 'from .. import insight',
                                     'project_watch.insight': 'import project_watch.more',
                                     'project_watch.more': ''},
                           {'project_watch.studio.core': 'Core', 'project_watch.insight': 'Insight',
                            'project_watch.more': 'Insight'})
    assert [(e.source, e.target) for e in audit(tmp_path, config).violations] == [
        ('project_watch.studio.core', 'project_watch.insight')]


def test_unknown_module_and_stale_classification_fail(tmp_path):
    config = fixture_graph(tmp_path, {'project_watch.new': ''}, {'project_watch.old': 'Core'})
    result = audit(tmp_path, config)
    assert result.unknown == ['project_watch.new']
    assert result.errors
    assert not result.ok


def test_unknown_page_fails(tmp_path):
    config = fixture_graph(tmp_path, {}, {})
    page = tmp_path / 'frontend/src/pages/New.tsx'
    page.parent.mkdir(parents=True)
    page.write_text('export default function New() {}', encoding='utf-8')
    assert audit(tmp_path, config).unknown_pages == ['frontend/src/pages/New.tsx']


def test_exact_exception_needs_reason_and_removal_task(tmp_path):
    config = fixture_graph(tmp_path, {'project_watch.core': 'import project_watch.insight',
                                     'project_watch.insight': ''},
                           {'project_watch.core': 'Core', 'project_watch.insight': 'Insight'},
                           exceptions=[{'source': 'project_watch.core', 'target': 'project_watch.insight',
                                        'reason': 'Existing mixed scheduler', 'remove_task': 'L2 injection'}])
    result = audit(tmp_path, config)
    assert result.ok and len(result.accepted) == 1
    config['exceptions'][0]['reason'] = ''
    assert not audit(tmp_path, config).ok


def test_allowlist_cannot_expand_or_use_wildcards():
    approved = {('project_watch.core', 'project_watch.insight')}
    assert not frozen_exceptions([{'source': 'project_watch.core', 'target': 'project_watch.insight'}], approved)
    assert frozen_exceptions([{'source': 'project_watch.other', 'target': 'project_watch.insight'}], approved)
    assert frozen_exceptions([{'source': 'project_watch.*', 'target': 'project_watch.insight'}], approved)


def test_cli_fails_on_synthetic_violation(tmp_path):
    config = fixture_graph(tmp_path, {'project_watch.core': 'import project_watch.insight',
                                     'project_watch.insight': ''},
                           {'project_watch.core': 'Core', 'project_watch.insight': 'Insight'})
    mapping = tmp_path / 'map.json'
    mapping.write_text(json.dumps(config), encoding='utf-8')
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_layer_boundaries.py'),
                             '--root', str(tmp_path), '--map', str(mapping)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert 'core_to_insight' in result.stdout


def test_repository_is_fully_registered():
    result = audit(ROOT, load_map(ROOT / 'scripts/layer_map.json'))
    assert result.ok, result.summary()
    assert not result.unknown
    assert not result.unknown_pages
