"""Offline synthetic journey from a physically stripped PRIVATE Studio wheel.

Run using a fresh venv's Python -I; no source insertion, no live provider calls.
"""
import json
import runpy
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import project_watch


def main():
    package = Path(project_watch.__file__).resolve().parent
    assert package.is_relative_to(Path(sys.prefix).resolve()), 'Must use installed wheel'
    assert not (package / 'api.py').exists(), 'Classic API must be physically absent'
    assert not (package / 'analysis').exists(), 'Old analysis package must be physically absent'
    for module in ('financial_intake', 'operating_intake', 'source_topics', 'financial_vocabulary',
                   'external_focus_schema', 'external_scopes', 'model_configuration',
                   'monitor_profile_store', 'company_reading_view', 'literal_metrics', 'evidence_proposals',
                   'holding_input', 'identity_input', 'followup_records'):
        assert (package / (module + '.py')).is_file(), 'Base intake contract must remain available'
    for module in ('dsh_executor', 'model_settings', 'external_analysis', 'monitor_briefing',
                   'monitor_profile', 'company_state', 'reading_jobs', 'candidate_metrics', 'tasks',
                   'holding_snapshots', 'identity_review', 'watch_items'):
        assert not (package / (module + '.py')).exists(), 'Unused legacy executor chain must be absent'
    assert not (package / 'studio/insight_generation.py').exists()
    journey = runpy.run_path(str(Path(__file__).with_name('distribution_journey_support.py')))
    core = runpy.run_path(str(Path(__file__).with_name('core_without_insight_support.py')))

    def no_network(*_args, **_kwargs):
        raise AssertionError('Installed Studio probe attempted outbound network')

    with tempfile.TemporaryDirectory(prefix='pw-studio-synthetic-') as folder, \
            patch('socket.create_connection', no_network), patch('socket.getaddrinfo', no_network):
        home = Path(folder)
        app, transport = journey['make_app'](home / 'name')
        core['with_public_peer'](transport)
        with TestClient(app) as client:
            assert client.get('/api/runtime').json()['classic_available'] is False
            assert client.get('/classic').status_code == 503
            assert client.get('/api/projects').status_code == 404
            assert client.get('/').status_code == 200
            for asset in ('app.js', 'index.css', 'THIRD-PARTY-LICENSES.txt'):
                assert client.get('/assets/' + asset).status_code == 200
            journey['configure'](client)
            connection = client.post('/api/studio/settings/test').json()
            assert all(connection[key]['ok'] for key in ('model', 'news', 'search_bocha'))
            pid, detail = journey['run_name_journey'](client, app)
            assert detail['monitoring_progress']['state'] == 'observed'
            assert not any(job['status'] == 'failed' for job in detail['jobs'])
            for category in ('company', 'competitor', 'industry', 'policy'):
                assert client.get('/api/studio/feed', params={'project_id': pid, 'category': category}).json()
            core['assert_public_peer'](app, pid)
            assert app.state.monitoring_scheduler is None and app.state.reading_worker is None
            assert client.get('/api/studio/health').json()['restart_required'] is False
            assert client.post('/api/studio/runtime/drain').json()['drained']
        material, material_transport = journey['make_app'](home / 'materials')
        with TestClient(material) as client:
            journey['configure'](client)
            receipt = core['bp_incremental_history'](client, material, material_transport)
            entity = journey['SyntheticTransport'].model_result('准确工商主体', '')['candidates'][0]
            failure = core['unknown_outcome_not_retried'](client, material, material_transport, entity)
        reopened, _ = journey['make_app'](home / 'materials')
        with TestClient(reopened) as client:
            history = client.get(f"/api/studio/projects/{receipt['project_id']}/questions/{receipt['watch_id']}/history").json()
            assert sorted(row['id'] for row in history) == receipt['history_ids']
            core['recover_after_restart'](client, reopened, failure)
        assert 'project_watch.api' not in sys.modules
        assert 'project_watch.reading_worker' not in sys.modules
        print(json.dumps({'installed_at': str(package), 'first_user': 'passed',
                          'bp_t1_history_recovery': 'passed', 'classic': 'absent',
                          'live_calls': 0, 'synthetic_http_calls': len(transport.calls) + len(material_transport.calls)}))


if __name__ == '__main__':
    main()
