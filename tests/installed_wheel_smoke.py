"""Run only from a newly installed wheel in an isolated synthetic environment."""

from __future__ import annotations

import importlib.util
import json
import runpy
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import project_watch
from project_watch.api import create_app
from project_watch.studio.app import create_studio_app


def main() -> None:
    without_insight = '--without-insight' in sys.argv[1:]
    installed_at = Path(project_watch.__file__).resolve()
    if not installed_at.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Project Watch was loaded from source rather than the fresh venv")
    private = ('competition', 'competition_evaluation', 'competition_research', 'landscape',
               'product_matching', 'question_advisor', 'plan_replan', 'race', 'red_flags', 'registry_peers',
               'techroutes', 'track', 'insight_generation', 'brief_analysis', 'crosscheck_analysis')
    if without_insight:
        for name in private:
            assert not (installed_at.parent / 'studio' / (name + '.py')).exists(), name
            assert importlib.util.find_spec('project_watch.studio.' + name) is None, name
    else:
        for name in private:
            module = importlib.import_module('project_watch.studio.' + name)
            assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), name
        print(json.dumps({'installed_analysis_modules': len(private), 'full_distribution': True}))
    with tempfile.TemporaryDirectory(prefix="project-watch-synthetic-") as temp_dir:
        # Load only the fake transport harness from the checkout, never product
        # source. Product imports above must resolve inside this fresh venv.
        journey = runpy.run_path(str(Path(__file__).with_name('distribution_journey_support.py')))
        core = runpy.run_path(str(Path(__file__).with_name('core_without_insight_support.py')))
        def no_network(*_args, **_kwargs):
            raise AssertionError('Installed-wheel journey attempted outbound network')

        with patch('socket.create_connection', no_network), patch('socket.getaddrinfo', no_network):
            fresh, transport = journey['make_app'](Path(temp_dir) / 'first-user')
            if without_insight:
                core['with_public_peer'](transport)
            with TestClient(fresh) as client:
                settings = client.get('/api/studio/settings').json()
                assert not settings['model']['ready'] and not settings['search']['ready']
                journey['configure'](client)
                result = client.post('/api/studio/settings/test').json()
                assert all(result[k]['ok'] for k in ('model', 'news', 'search_bocha'))
                pid, detail = journey['run_name_journey'](client, fresh)
                assert detail['monitoring_progress']['state'] == 'observed'
                for category in ('company', 'competitor', 'industry', 'policy'):
                    assert client.get('/api/studio/feed', params={'project_id': pid, 'category': category}).json()
                assert not any(j['status'] == 'failed' for j in detail['jobs'])
                assert client.get('/api/studio/settings').json()['commercial']['ready'] == []
                if without_insight:
                    core['assert_public_peer'](fresh, pid)
            print(json.dumps({'installed_at': str(installed_at), 'first_user': 'passed',
                              'synthetic_http_calls': len(transport.calls), 'live_calls': 0,
                              'without_insight': without_insight}))
            if without_insight:
                core_home = Path(temp_dir) / 'core-materials'
                material_app, material_transport = journey['make_app'](core_home)
                with TestClient(material_app) as client:
                    journey['configure'](client)
                    receipt = core['bp_incremental_history'](client, material_app, material_transport)
                    entity = journey['SyntheticTransport'].model_result('准确工商主体', '')['candidates'][0]
                    failure = core['unknown_outcome_not_retried'](client, material_app, material_transport, entity)
                reopened, _ = journey['make_app'](core_home)
                with TestClient(reopened) as client:
                    history = client.get(f"/api/studio/projects/{receipt['project_id']}/questions/{receipt['watch_id']}/history").json()
                    assert sorted(row['id'] for row in history) == receipt['history_ids']
                    core['recover_after_restart'](client, reopened, failure)
                assert not any('project_watch.studio.' + name in sys.modules for name in private)
                print(json.dumps({'core_bp_t1': 'passed', 'scoped_agent_history_restart_recovery': 'passed', 'live_calls': 0}))
        studio = create_studio_app(Path(temp_dir) / "studio", start_worker=False)
        with TestClient(studio) as client:
            assert client.get("/").status_code == 200
            assert "assets/app.js" in client.get("/").text
            assert "Project Watch" in client.get("/classic").text
            assert client.get("/api/runtime").json()["parser"] == "subprocess"
            for asset in (
                "app.js", "index.css", "newsreader-latin-wght-normal.woff2",
                "noto-serif-sc-chinese-simplified-600-normal.woff2",
            ):
                response = client.get(f"/assets/{asset}")
                assert response.status_code == 200 and response.content, asset
            notices = client.get("/assets/THIRD-PARTY-LICENSES.txt")
            assert notices.status_code == 200
            assert "Commons Clause" in notices.text and "SIL OPEN FONT LICENSE" in notices.text
            assert client.get("/api/studio/projects").json() == []
            project = studio.state.intake_worker.repository.create_project("Synthetic Peer Selection")
            studio.state.studio_store.upsert_project(project.id, {"legal_name": project.name}, consent=True)
            selection = f"/api/studio/projects/{project.id}/competitors/selection"
            for version, decision in enumerate(("include", "exclude", "include"), start=1):
                selected = client.post(selection, json={"name": "Synthetic Peer", "decision": decision,
                    "expected_version": version, "idempotency_key": f"synthetic-{version}"})
                assert selected.status_code == 200, selected.text
                assert selected.json()["job_status"] == "not_requested"
            assert not studio.state.studio_store.jobs(project.id)
            assert studio.state.studio_application.relationships.view(project.id)["items"][0]["peer_name"] == "Synthetic Peer"
            for projection in ('race', 'red-flags', 'track', 'tech', 'commitments'):
                response = client.get(f'/api/studio/projects/{project.id}/{projection}')
                expected = 503 if without_insight and projection != 'commitments' else 200
                assert response.status_code == expected, (projection, response.status_code)
                if expected == 503:
                    assert response.json()['detail']['code'] == 'insight_unavailable'
            tech = f'/api/studio/projects/{project.id}/tech'
            if without_insight:
                assert client.put(tech, json={'terms': ['FMCW'], 'own': 'FMCW'}).status_code == 503
                from project_watch.studio import insight

                assert not hasattr(insight, 'SYSTEM') and not hasattr(insight, 'run')
                assert studio.state.studio_pipeline.insight_generator is None
                assert studio.state.studio_pipeline.schedule_insight(project.id) is None
                view = client.get(f'/api/studio/projects/{project.id}/insight').json()
                assert view['generation_available'] is False and view['record'] is None
                refresh = client.post(f'/api/studio/projects/{project.id}/refresh', json={'kind': 'insight'})
                assert refresh.status_code == 503 and refresh.json()['detail']['code'] == 'insight_unavailable'
                assert not studio.state.studio_store.jobs(project.id)
            else:
                assert client.put(tech, json={'terms': ['FMCW', 'MEMS'], 'own': 'FMCW'}).json()['source'] == 'user'
                assert client.get(tech).json()['routes'][0]['term'] == 'FMCW'
                assert client.put(tech, json={'terms': [], 'own': None}).json()['source'] == 'auto'
        print(json.dumps({"studio_ui": "loaded", "classic_ui": "loaded", "parser": "subprocess"}))
        app = create_app(Path(temp_dir) / "watch.sqlite3", start_worker=True)
        with TestClient(app) as client:
            page = client.get("/")
            css = client.get("/static/styles.css")
            batch = client.get("/static/upload_batch.mjs")
            assert page.status_code == 200
            assert css.status_code == batch.status_code == 200
            assert css.headers["cache-control"] == batch.headers["cache-control"] == "no-store"
            assert ".upload-status{" in css.text
            project_response = client.post(
                "/api/projects", json={"name": "Synthetic Windows Trial"}
            )
            assert project_response.status_code == 201
            project_id = project_response.json()["id"]
            route = f"/api/projects/{project_id}"
            uploaded = client.post(
                f"{route}/files",
                files={"file": ("intro.txt", b"Synthetic company introduction", "text/plain")},
            )
            assert uploaded.status_code == 202
            deadline = time.monotonic() + 8
            status = "queued"
            while time.monotonic() < deadline:
                jobs = client.get(f"{route}/intake").json()
                status = jobs[0]["status"]
                if status != "queued" and status != "processing":
                    break
                time.sleep(0.05)
            assert status == "completed", f"synthetic intake ended as {status}"
            domains_response = client.get(f"{route}/monitoring/domains")
            report_response = client.get(f"{route}/report/current")
            assert domains_response.status_code == report_response.status_code == 200
            assert len(domains_response.json()["domains"]) == 6
            print(json.dumps({"synthetic_project": project_id, "intake": status, "domains": 6}))


if __name__ == "__main__":
    main()
