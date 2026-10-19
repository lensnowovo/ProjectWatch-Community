"""M3A: a fresh process must run Core with every private module unavailable."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Freeze the approved private set independently of future inventory changes.
INSIGHT_MODULES = (
    'competition', 'competition_evaluation', 'competition_research', 'landscape',
    'product_matching', 'question_advisor', 'plan_replan', 'race', 'red_flags', 'registry_peers',
    'techroutes', 'track', 'insight_generation', 'brief_analysis', 'crosscheck_analysis',
)
GUARD = '''
import importlib.abc
import json
import socket
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
blocked = {'project_watch.studio.' + name for name in json.loads(sys.argv[2])}
class DenyInsight(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise ModuleNotFoundError('M3A private module unavailable', name=fullname)
sys.meta_path.insert(0, DenyInsight())
def forbidden(*args, **kwargs):
    raise AssertionError('M3A must not use live networking')
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
'''


def isolated(code, *, root=ROOT):
    result = subprocess.run(
        [sys.executable, '-I', '-c', GUARD + code, str(root / 'src'),
         json.dumps(INSIGHT_MODULES), str(root / 'tests')],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize('module', [
    'business_identity', 'comparisons', 'pipeline', 'relationships', 'structured_facts',
])
def test_core_imports_without_any_insight_module(module):
    isolated(f'\nimport importlib\nimportlib.import_module("project_watch.studio.{module}")\n')


def test_shared_peer_and_reader_tools_without_insight():
    isolated('''
from project_watch.studio.peer_names import peers_for, headline_leads
from project_watch.studio.public_reading import read_page
from project_watch.studio.peer_queries import industry_phrase
assert headline_leads('合成光学获得订单', ['合成光学'])
assert not headline_leads('合成光学的客户获得订单', ['合成光学'])
assert peers_for({'name': '合成光学'}, {'competitors': [{'name': '合成星辰'}]}, {})[0]['name'] == '合成星辰'
assert industry_phrase('汽车连接器制造') == '汽车连接器'
assert callable(read_page)
''')


def test_name_to_results_without_insight(tmp_path):
    isolated('''
import runpy
from fastapi.testclient import TestClient
support = runpy.run_path(str(Path(sys.argv[3]) / 'distribution_journey_support.py'))
core = runpy.run_path(str(Path(sys.argv[3]) / 'core_without_insight_support.py'))
app, transport = support['make_app'](Path(''' + repr(str(tmp_path)) + '''))
core['with_public_peer'](transport)
with TestClient(app) as client:
    support['configure'](client)
    pid, detail = support['run_name_journey'](client, app)
    assert detail['monitoring_progress']['state'] == 'observed', detail['jobs']
    assert not [j for j in detail['jobs'] if j['status'] == 'failed'], detail['jobs']
    for category in ('company', 'competitor', 'industry', 'policy'):
        response = client.get('/api/studio/feed', params={'project_id': pid, 'category': category})
        assert response.status_code == 200 and response.json(), category
    assert detail['brief']['headline'] == '合成公司首轮进展'
    core['assert_public_peer'](app, pid)
    assert client.get('/api/studio/events', params={'project_id': pid}).json()
    assert app.state.studio_pipeline.question_advisor is None
    assert app.state.studio_pipeline.insight_generator is None
    assert app.state.studio_pipeline.brief_analysis is None
    assert app.state.studio_pipeline.crosscheck_analysis is None
    assert detail['brief']['outlook'] == {} and detail['brief']['tone'] == 'neutral'
    assert app.state.studio_pipeline.schedule_insight(pid) is None
    assert client.get(f'/api/studio/projects/{pid}/insight').json()['generation_available'] is False
    response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'insight'})
    assert response.status_code == 503 and response.json()['detail']['code'] == 'insight_unavailable'
    for name in ('race', 'red-flags', 'track', 'tech'):
        response = client.get(f'/api/studio/projects/{pid}/{name}')
        assert response.status_code == 503, (name, response.status_code, response.text)
        assert response.json()['detail']['code'] == 'insight_unavailable'
    assert not blocked.intersection(sys.modules)
''')


def test_bp_incremental_history_permissions_and_restart_without_insight(tmp_path):
    isolated('''
import runpy
from fastapi.testclient import TestClient
support = runpy.run_path(str(Path(sys.argv[3]) / 'distribution_journey_support.py'))
core = runpy.run_path(str(Path(sys.argv[3]) / 'core_without_insight_support.py'))
home = Path(''' + repr(str(tmp_path)) + ''')
app, transport = support['make_app'](home)
with TestClient(app) as client:
    support['configure'](client)
    receipt = core['bp_incremental_history'](client, app, transport)
    entity = support['SyntheticTransport'].model_result('准确工商主体', '')['candidates'][0]
    failure = core['unknown_outcome_not_retried'](client, app, transport, entity)
    assert app.state.studio_pipeline.relationships.assessment_validator is None
    try:
        app.state.studio_pipeline.relationships.save_assessment(receipt['project_id'], {})
    except ValueError as exc:
        assert 'insight_unavailable' in str(exc)
    else:
        raise AssertionError('Core must not certify a private assessment without its validator')
app2, _ = support['make_app'](home)
with TestClient(app2) as client:
    history = client.get(f"/api/studio/projects/{receipt['project_id']}/questions/{receipt['watch_id']}/history").json()
    assert sorted(r['id'] for r in history) == receipt['history_ids']
    assert app2.state.studio_store.jobs(failure['project_id'])[0]['status'] == 'failed'
    core['recover_after_restart'](client, app2, failure)
    assert not blocked.intersection(sys.modules)
''')
