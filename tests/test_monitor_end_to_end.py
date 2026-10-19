"""One isolated project, actual API/worker/storage path, synthetic boundaries."""
import io
import json
import runpy
import sys
from contextlib import redirect_stdout
from pathlib import Path


def test_installed_journey_contract(tmp_path):
    journey = runpy.run_path(str(Path(__file__).parents[1] / 'scripts' / 'smoke_monitor_journey.py'))
    app, project_id, report = journey['exercise'](tmp_path)
    assert project_id
    assert report['passed'] is True
    assert report['real_model_quality'] == 'not_tested'
    assert report['real_public_collection'] == 'not_tested'
    assert report['private_materials_used'] is False
    assert len(report['checks']) >= 12
    assert all(report['checks'].values())
    app.state.reading_worker.stop()


def test_real_office_containers_through_installed_reading_contract(tmp_path):
    journey = runpy.run_path(str(Path(__file__).parents[1] / 'scripts' / 'smoke_office_reading.py'))
    app, project_id, report = journey['exercise'](tmp_path)
    assert project_id and report['passed']
    assert report['formats'] == ['pptx', 'xlsx']
    assert not report['network_used'] and not report['private_materials_used']
    assert report['real_model_quality'] == 'not_tested'
    assert report['synthetic_model_calls'] == 3
    assert report['checks']['versions_both_readable']
    assert report['checks']['missing_cell_not_zero']
    assert report['checks']['no_automatic_confirmed_fact']
    app.state.reading_worker.stop()


def test_office_smoke_report_supports_windows_non_unicode_console(monkeypatch):
    journey = runpy.run_path(str(Path(__file__).parents[1] / 'scripts' / 'smoke_office_reading.py'))
    monkeypatch.setattr(sys, 'argv', ['smoke_office_reading.py'])
    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding='cp1252')
    with redirect_stdout(output):
        journey['main']()
    output.flush()
    report = json.loads(raw.getvalue().decode('cp1252'))
    assert report['passed'] and report['checks']['parsed_合成项目BP.pptx']
