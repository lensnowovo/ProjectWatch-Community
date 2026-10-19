import json
from contextlib import closing

import pytest

from project_watch.model_settings import ModelSettings
from project_watch.monitor_autopilot import MonitorAutopilot
from project_watch.reading_jobs import ReadingDenied
from tests.test_monitor_activation import fixture
from tests.test_reading_worker import FakeModel


def sample(tmp_path):
    repo, packet, strategy = fixture(tmp_path)
    model = FakeModel()
    model.fail = False
    def plan(packet, *, before_send):
        if model.before:
            model.before()
        before_send(json.dumps({'model':'fake','stream':True,'max_tokens':8192,'messages':[]}).encode())
        model.calls += 1
        if model.fail:
            raise TimeoutError('no response')
        return strategy
    model.plan_monitoring = plan
    settings = ModelSettings(model)
    pilot = MonitorAutopilot(repo, settings)
    return repo, packet, strategy, model, settings, pilot


def start(pilot, packet):
    return pilot.authorize(packet, public_consent=True, model_consent=True)


def test_unsent_legacy_consent_cannot_dispatch_new_prompt(tmp_path, monkeypatch):
    import project_watch.monitor_autopilot as module
    _, packet, _, model, _, pilot = sample(tmp_path)
    with monkeypatch.context() as old:
        old.setattr(module, 'RULE_VERSION', 'bp-monitor-strategy-v2')
        start(pilot, packet)
    assert pilot.process_one() is None
    assert model.calls == 0
    assert pilot.read(packet['project_id'])[0]['state'] == 'strategy_changed'


def test_completed_legacy_strategy_stays_active_without_replanning(tmp_path, monkeypatch):
    import project_watch.monitor_autopilot as module
    from project_watch.monitor_auto_analysis import parent_current
    repo, packet, strategy, model, _, pilot = sample(tmp_path)
    strategy['rule_version'] = 'bp-monitor-strategy-v2'
    with monkeypatch.context() as old:
        old.setattr(module, 'RULE_VERSION', 'bp-monitor-strategy-v2')
        started = start(pilot, packet)
        pilot.process_one()
    assert pilot.process_one() is None
    assert model.calls == 1
    assert pilot.read(packet['project_id'])[0]['state'] == 'collecting'
    with closing(repo._connect()) as db:
        parent_current(db, started['id'], packet['project_id'], model.authorization_id())


def test_new_project_no_keywords_to_model_and_collection_jobs(tmp_path):
    repo, packet, _, model, settings, pilot = sample(tmp_path)
    assert pilot.process_one() is None
    assert not repo.list_monitoring_plans(packet['project_id'])
    job = start(pilot, packet)
    assert start(pilot, packet)['id'] == job['id']
    pilot.process_one()
    assert model.calls == 1
    assert len(repo.list_monitoring_jobs(packet['project_id'])) == 2
    assert pilot.read(packet['project_id'])[0]['state'] == 'collecting'
    assert MonitorAutopilot(repo, settings).process_one() is None
    assert model.calls == 1
    assert not repo.list_observations(packet['project_id'])


def test_public_or_model_consent_required_and_other_purpose_not_reused(tmp_path):
    _, packet, _, model, _, pilot = sample(tmp_path)
    for consent in ({'public_consent':False,'model_consent':True},
                    {'public_consent':True,'model_consent':False}):
        with pytest.raises(ReadingDenied):
            pilot.authorize(packet, **consent)
    assert model.calls == 0


def test_timeout_never_automatically_dispatches_twice(tmp_path):
    _, packet, _, model, _, pilot = sample(tmp_path)
    start(pilot, packet)
    model.fail = True
    pilot.process_one()
    assert pilot.read(packet['project_id'])[0]['state'] == 'uncertain'
    assert pilot.process_one() is None
    assert model.calls == 1


def test_pause_before_egress_and_receiver_change(tmp_path):
    repo, packet, _, model, settings, pilot = sample(tmp_path)
    job = start(pilot, packet)
    model.before = lambda: pilot.pause(job['id'])
    pilot.process_one()
    assert model.calls == 0 and not repo.list_monitoring_plans(packet['project_id'])
    assert pilot.read(packet['project_id'])[0]['state'] == 'paused'
    settings.model = None
    assert pilot.process_one() is None


def test_material_change_and_invalid_output_do_not_activate(tmp_path):
    repo, packet, _strategy, model, _, pilot = sample(tmp_path)
    start(pilot, packet)
    model.before = lambda: repo.add_source(packet['project_id'],kind='document',
        reference=packet['reference'],content_sha256='b'*64)
    pilot.process_one()
    assert model.calls == 0 and not repo.list_monitoring_plans(packet['project_id'])
    assert pilot.read(packet['project_id'])[0]['state'] == 'material_changed'


def test_invalid_model_output_not_published(tmp_path):
    repo, packet, strategy, _model, _, pilot = sample(tmp_path)
    start(pilot, packet)
    strategy['scopes'][0]['anchor'] = 'unsupported private company'
    pilot.process_one()
    assert not repo.list_monitoring_plans(packet['project_id'])
    assert pilot.read(packet['project_id'])[0]['state'] == 'uncertain'


def test_new_dispatch_cannot_publish_a_legacy_strategy(tmp_path):
    repo, packet, strategy, _, _, pilot = sample(tmp_path)
    start(pilot, packet)
    strategy['rule_version'] = 'bp-monitor-strategy-v2'
    pilot.process_one()
    assert not repo.list_monitoring_plans(packet['project_id'])
    assert pilot.read(packet['project_id'])[0]['state'] == 'uncertain'


def test_saved_strategy_activation_recovery_never_repeats_model(tmp_path, monkeypatch):
    repo, packet, _, model, settings, pilot = sample(tmp_path)
    start(pilot, packet)
    original = pilot.activation.activate
    def fail(*args, **kwargs):
        raise RuntimeError('simulated local interruption')
    monkeypatch.setattr(pilot.activation, 'activate', fail)
    pilot.process_one()
    assert model.calls == 1 and not repo.list_monitoring_plans(packet['project_id'])
    monkeypatch.setattr(pilot.activation, 'activate', original)
    with closing(repo._connect()) as db, db:
        db.execute('UPDATE monitor_autopilot_activation_state SET next_attempt=0')
    MonitorAutopilot(repo, settings).process_one()
    assert len(repo.list_monitoring_plans(packet['project_id'])) == 2
    assert model.calls == 1


def test_pause_stops_owned_plans_and_preserves_human_scopes(tmp_path):
    repo, packet, _, _, _, pilot = sample(tmp_path)
    from project_watch.external_focus import ExternalFocusStore
    existing = ExternalFocusStore(repo).create(packet['project_id'],category='company',
        term='Synthetic public company',provider='bing')
    repo.set_monitoring_plan_enabled(existing.id, True)
    job = start(pilot, packet)
    pilot.process_one()
    pilot.pause(job['id'])
    assert repo.get_monitoring_plan(existing.id).enabled
    assert sum(p.enabled for p in repo.list_monitoring_plans(packet['project_id'])) == 1


def test_lost_dispatched_job_recovers_uncertain(tmp_path):
    repo, packet, _, _, _, pilot = sample(tmp_path)
    job = start(pilot, packet)
    claimed = pilot.ledger.claim(job['job_id'])
    pilot.ledger.before_send(job['job_id'], claimed['lease_token'],config_id='b'*64)(
        json.dumps({'model':'fake','stream':True,'max_tokens':8192,'messages':[]}).encode())
    with closing(repo._connect()) as db, db:
        db.execute('UPDATE reading_jobs SET lease_until=0 WHERE id=?',(job['job_id'],))
    assert pilot.process_one() is None
    assert pilot.read(packet['project_id'])[0]['state'] == 'uncertain'


def test_api_disclosure_and_wrong_purpose_isolation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from project_watch.api import create_app
    repo, packet, _, model, _, _ = sample(tmp_path)
    monkeypatch.setenv('PROJECT_WATCH_SETTINGS_TOKEN','x'*40)
    app=create_app(repo.db_path,proposal_model=model)
    client=TestClient(app,base_url='http://127.0.0.1')
    path=f"/api/workspace/projects/{packet['project_id']}/monitor-autopilot"
    headers={'origin':'http://127.0.0.1','x-settings-token':'x'*40}
    assert client.post(path+'/preview',json={'packet':packet}).status_code == 403
    preview=client.post(path+'/preview',headers=headers,json={'packet':packet})
    assert preview.status_code == 200
    body={'packet':packet,'model_consent':True,'public_consent':True,
        'disclosure_digest':preview.json()['disclosure_digest']}
    assert model.calls == 0
    assert client.post(path,headers=headers,json={**body,'public_consent':False}).status_code == 409
    started=client.post(path,headers=headers,json=body)
    assert started.status_code == 202
    grant=started.json()['grant_id']
    reading=f"/api/workspace/projects/{packet['project_id']}/reading-authorization"
    assert client.get(reading).json()['authorizations'] == []
    assert client.patch(reading+'/'+grant,headers=headers,json={'status':'active'}).status_code == 404
    foreign=repo.create_project('Other project')
    assert client.post(path.replace(packet['project_id'],foreign.id),headers=headers,json=body).status_code == 409
    assert client.post(path+'/'+started.json()['id']+'/pause',headers=headers).status_code == 200
    assert client.get(path).json()['workflows'][0]['state'] == 'paused'


def test_concurrent_dispatch_is_claimed_once(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    repo, packet, _, model, settings, pilot = sample(tmp_path)
    start(pilot,packet)
    entered,release=threading.Event(),threading.Event()
    model.before=lambda:(entered.set(),release.wait(5))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(pilot.process_one)
        assert entered.wait(5)
        assert MonitorAutopilot(repo,settings).process_one() is None
        release.set()
        first.result(timeout=5)
    assert model.calls == 1


def test_receiver_race_and_service_stop_fail_closed(tmp_path):
    repo,packet,_,model,_,pilot=sample(tmp_path)
    with pytest.raises(ReadingDenied):
        pilot.authorize(packet,public_consent=True,model_consent=True,expected_config='c'*64)
    start(pilot,packet)
    model.before=lambda:setattr(pilot,'stopping',lambda:True)
    pilot.process_one()
    assert model.calls == 0
    assert not repo.list_monitoring_plans(packet['project_id'])


def test_saved_result_activation_failure_is_bounded(tmp_path,monkeypatch):
    repo,packet,_,model,_,pilot=sample(tmp_path)
    start(pilot,packet)
    calls=[]
    def fail(*args,**kwargs):
        calls.append(1)
        raise RuntimeError('local failure')
    monkeypatch.setattr(pilot.activation,'activate',fail)
    for _ in range(5):
        pilot.process_one()
        with closing(repo._connect()) as db, db:
            db.execute('UPDATE monitor_autopilot_activation_state SET next_attempt=0')
    assert len(calls) == 3 and model.calls == 1
    assert pilot.read(packet['project_id'])[0]['state'] == 'activation_failed'


def test_paused_scopes_not_counted_as_current_success(tmp_path):
    repo,packet,_,_,_,pilot=sample(tmp_path)
    start(pilot,packet)
    pilot.process_one()
    plans=repo.list_monitoring_plans(packet['project_id'])
    for i,p in enumerate(plans):
        repo.record_source_run(p.id,status='no_results',error_code=None,idempotency_key=f'empty-{i}')
    repo.set_monitoring_plan_enabled(plans[0].id,False)
    assert pilot.read(packet['project_id'])[0]['state']=='partial_coverage'
