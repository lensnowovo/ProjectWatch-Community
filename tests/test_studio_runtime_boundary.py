"""Offline dependency closure must preserve required Legacy and reject silent expansion."""
import json
from pathlib import Path

import pytest
from test_layer_boundaries import fixture_graph

from scripts.build_core_probe import inventory
from scripts.check_layer_boundaries import audit
from scripts.studio_runtime_boundary import legacy_closure

ROOT = Path(__file__).resolve().parents[1]


def test_actual_profile_removes_classic_but_preserves_all_core_contract_app():
    config = json.loads((ROOT / 'scripts/layer_map.json').read_text(encoding='utf-8'))
    full = inventory(ROOT)
    studio = inventory(ROOT, profile='studio')
    assert len(full['retained_legacy_modules']) == 71
    assert 'project_watch.api' in studio['excluded_legacy_modules']
    for module in ('model_settings', 'dsh_executor', 'external_analysis', 'monitor_briefing',
                   'monitor_profile', 'company_state', 'reading_jobs', 'candidate_metrics', 'tasks',
                   'holding_snapshots', 'identity_review', 'watch_items'):
        assert 'project_watch.' + module in studio['excluded_legacy_modules']
    assert config['modules']['project_watch.model_configuration'] == 'Core'
    assert config['modules']['project_watch.model_settings'] == 'Legacy'
    assert config['modules']['project_watch.monitor_profile_store'] == 'Core'
    assert config['modules']['project_watch.company_reading_view'] == 'Core'
    assert config['modules']['project_watch.literal_metrics'] == 'Core'
    assert config['modules']['project_watch.evidence_proposals'] == 'Core'
    for module in ('analysis', 'analysis.financial_mapping', 'analysis.operating_mapping',
                   'analysis.source_update', 'analysis.financial_projection', 'analysis.financial_integrity'):
        assert 'project_watch.' + module in studio['excluded_legacy_modules']
    assert 'project_watch.team_http' in studio['retained_legacy_modules']
    assert len(studio['retained_legacy_modules']) < len(full['retained_legacy_modules'])
    for name, path in studio['retained_legacy_paths'].items():
        assert path[-1] == name
        assert config['modules'][path[0]] in {'Core', 'Contract', 'App'}
    for name, layer in config['modules'].items():
        if layer not in {'Core', 'Contract', 'App'}:
            continue
        stem = name.replace('.', '/')
        assert stem + '.py' in studio['files'] or stem + '/__init__.py' in studio['files']
    assert studio['mixed_private_symbols'] == full['mixed_private_symbols']


def test_storage_and_runner_no_longer_import_legacy_reading_module():
    config = json.loads((ROOT / 'scripts/layer_map.json').read_text(encoding='utf-8'))
    edges = {(edge.source, edge.target) for edge in audit(ROOT, config).edges}
    assert ('project_watch.repository', 'project_watch.external_focus_schema') in edges
    assert ('project_watch.monitoring', 'project_watch.external_scopes') in edges
    for module in ('repository', 'monitoring', 'external_scopes', 'external_focus_schema'):
        assert ('project_watch.' + module, 'project_watch.external_focus') not in edges
    assert config['modules']['project_watch.external_scopes'] == 'Core'
    assert config['modules']['project_watch.external_focus_schema'] == 'Core'


def test_transitive_dependency_kept_unused_legacy_removed(tmp_path):
    config = fixture_graph(tmp_path, {
        'project_watch.core': 'from .legacy import function',
        'project_watch.legacy': 'from .storage import function',
        'project_watch.storage': '', 'project_watch.unused': '',
    }, {'project_watch.core': 'Core', 'project_watch.legacy': 'Legacy',
        'project_watch.storage': 'Legacy', 'project_watch.unused': 'Legacy'})
    result = legacy_closure(tmp_path, config)
    assert result['retained_legacy_paths']['project_watch.storage'] == [
        'project_watch.core', 'project_watch.legacy', 'project_watch.storage']
    assert result['excluded_legacy_modules'] == ['project_watch.unused']


def test_package_initializer_dependency_is_preserved(tmp_path):
    config = fixture_graph(tmp_path, {'project_watch.legacy.child': '',
                                     'project_watch.storage': ''},
                           {'project_watch.legacy.child': 'Core', 'project_watch.storage': 'Legacy'})
    config['modules']['project_watch.legacy'] = 'Legacy'
    (tmp_path / 'src/project_watch/legacy/__init__.py').write_text('from ..storage import function')
    result = legacy_closure(tmp_path, config)
    assert result['retained_legacy_paths']['project_watch.storage'] == [
        'project_watch.legacy.child', 'project_watch.legacy', 'project_watch.storage']


def test_new_required_classic_import_fails_closed(tmp_path):
    config = fixture_graph(tmp_path, {'project_watch.core': 'from .api import create_app',
                                     'project_watch.api': ''},
                           {'project_watch.core': 'Core', 'project_watch.api': 'Legacy'})
    with pytest.raises(ValueError, match='unapproved path'):
        legacy_closure(tmp_path, config)


def test_invalid_profile_cannot_silently_build_compat():
    with pytest.raises(ValueError, match='Unknown probe profile'):
        inventory(ROOT, profile='typo')
