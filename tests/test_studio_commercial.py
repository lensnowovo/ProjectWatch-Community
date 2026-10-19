"""Synthetic vendor contracts: no real company replies or credentials in Git."""
import json
from contextlib import closing

import pytest

from project_watch.studio.commercial import CommercialStore, MCPClient, normalize
from project_watch.studio.http import HttpError, Response
from project_watch.studio.settings import CommercialConfig, SettingsStore

COMPANY = "星舟合成科技有限公司"
KEY = "synthetic-secret-never-public"


def reply(data):
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": False}


def xiniu(company=COMPANY):
    return reply({"公司信息": {"公司名称": company}, "融资事件详情": [
        {"融资轮次": "A轮", "融资日期": "2026-06-01", "融资金额": "数千万人民币", "投资方": "合成基金"}],
        "融资相关新闻": [{"标题": company + "宣布融资", "发布日期": "2026-06-02",
                         "源链接": "https://synthetic.example/news"}]})


def fixture(tmp_path):
    from project_watch.studio.app import create_studio_app
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project(COMPANY)
    pipeline.store.upsert_project(project.id, {"legal_name": COMPANY}, consent=True)
    return pipeline, project.id, CommercialStore(pipeline.store)


def test_settings_default_mask_preserve_clear_and_fixed_destinations(tmp_path):
    settings = SettingsStore(tmp_path / "settings.json")
    assert settings.load().commercial.providers == []
    settings.update({"commercial": {"providers": ["xiniu"], "keys": {"xiniu": KEY}}})
    settings.update({"commercial": {"keys": {"xiniu": ""}}})
    assert settings.load().commercial.keys["xiniu"] == KEY
    public = settings.public_view(settings.load())
    assert KEY not in json.dumps(public)
    assert public["commercial"]["ready"] == ["xiniu"]
    for bad in ({"providers": ["unknown"]}, {"keys": {"wind": "bad\nkey"}},
                {"endpoint": "https://evil.test"}, {"providers": ["wind", "wind"]}):
        with pytest.raises(ValueError):
            settings.update({"commercial": bad})
    settings.update({"commercial": {"clear_keys": ["xiniu"]}})
    assert settings.load().commercial.ready_providers() == []


def test_identity_amounts_and_original_dates_are_not_upgraded():
    data = normalize("xiniu", xiniu(), COMPANY, [])
    assert data["records"][0]["amount"] == "数千万人民币"
    assert "value" not in data["records"][0]
    assert data["records"][0]["date"] == "2026-06-01"
    assert data["records"][1]["kind"] == "news"
    assert normalize("xiniu", xiniu("星舟其他有限公司"), COMPANY, ["星舟"])['records'] == []
    pharm = reply({"total": 2, "data": [
        {"company_name": "星舟", "company_intro": COMPANY, "event_name": "融资", "financing_date": "2026-06-03",
         "event_round_cn": "A轮", "amount_str": "数千万元", "amount_value_cn": 3000, "total_capitals": "合成基金"},
        {"company_name": "别家公司", "company_intro": "报道提到" + COMPANY, "event_name": "融资"}]})
    result = normalize("pharmcube", pharm, COMPANY, ["星舟"])
    assert len(result["records"]) == 1
    assert result["rejected"] == 1
    assert result["records"][0]["amount"] == "数千万元"
    assert "3000" not in json.dumps(result)


def test_mcp_session_headers_and_matching_ids_are_bounded():
    calls = []
    def send(method, url, headers, body, timeout):
        message = json.loads(body)
        calls.append((url, headers, message))
        if message['method'] == 'initialize':
            return Response(200, {"mcp-session-id": "test-session"}, json.dumps({
                "jsonrpc": "2.0", "id": message['id'], "result": {"protocolVersion": "2025-03-26"}}).encode())
        assert headers['Mcp-Session-Id'] == 'test-session'
        if message['method'] == 'notifications/initialized':
            return Response(202, {}, b'')
        data = json.dumps({"jsonrpc": "2.0", "id": message['id'], "result": xiniu()}, ensure_ascii=False).encode()
        return Response(200, {"content-type": "text/event-stream"}, b'event: message\ndata: ' + data + b'\n\n')
    result = MCPClient('xiniu', KEY, send).fetch(COMPANY, [], '2026-10-09')
    assert len(result['records']) == 2
    assert len(calls) == 3
    assert all(url.startswith('https://vip.xiniudata.com/mcp?api_key=') for url, _, _ in calls)
    assert calls[-1][2]['params']['name'] == 'get_company_info'


@pytest.mark.parametrize('response,code', [
    (Response(302, {'location': 'https://evil.test'}, b''), 'commercial_http'),
    (Response(401, {}, b'secret-raw-error'), 'commercial_http'),
    (Response(200, {}, b'{"id":99,"result":{}}'), 'commercial_protocol'),
    (Response(200, {}, b'{"id":1,"error":{"message":"secret-raw-error"}}'), 'commercial_protocol'),
])
def test_mcp_errors_do_not_expose_key_or_server_text(response, code):
    with pytest.raises(HttpError) as error:
        MCPClient('xiniu', KEY, lambda *args: response).fetch(COMPANY, [], '2026-10-09')
    assert error.value.code == code
    assert KEY not in str(error.value) and 'secret-raw-error' not in str(error.value)


def test_cache_survives_restart_and_private_records_do_not_enter_items(tmp_path):
    pipeline, pid, sources = fixture(tmp_path)
    calls = []
    def factory(provider, key, proxy):
        class Client:
            def fetch(self, name, aliases, end):
                calls.append(name)
                return normalize(provider, xiniu(name), name, aliases)
        return Client()
    config = CommercialConfig(['xiniu'], {'xiniu': KEY})
    project = pipeline.store.get_project(pid)
    first = sources.collect(project, config, client_factory=factory)
    second = CommercialStore(pipeline.store).collect(project, config, client_factory=factory)
    assert len(calls) == 1 and first['xiniu']['status'] == 'ok' and second['xiniu']['cached']
    view = sources.view(pid, config)
    assert len(view['records']) == 2 and KEY not in json.dumps(view)
    assert pipeline.store.list_items(project_id=pid) == []
    changed = CommercialConfig(['xiniu'], {'xiniu': KEY + '-rotated'})
    sources.collect(project, changed, client_factory=factory)
    assert len(calls) == 2


def test_uncertain_request_is_held_across_jobs_and_keeps_old_records(tmp_path):
    pipeline, pid, sources = fixture(tmp_path)
    config = CommercialConfig(['xiniu'], {'xiniu': KEY})
    count = []
    def broken(provider, key, proxy):
        class Client:
            def fetch(self, *args):
                count.append(1)
                raise HttpError('connection_failed', detail=KEY)
        return Client()
    project = pipeline.store.get_project(pid)
    a = sources.collect(project, config, client_factory=broken)
    b = sources.collect(project, config, client_factory=broken)
    assert len(count) == 1 and a['xiniu']['status'] == b['xiniu']['status'] == 'held'
    assert KEY not in json.dumps(sources.view(pid, config))


def test_paused_unactivated_or_disabled_never_calls(tmp_path):
    pipeline, pid, sources = fixture(tmp_path)
    project = pipeline.store.get_project(pid)
    config = CommercialConfig(['xiniu'], {'xiniu': KEY})
    def factory(*args):
        pytest.fail('no provider calls allowed')
    assert sources.collect({**project, 'status': 'paused'}, config, client_factory=factory) == {}
    assert sources.collect({**project, 'activated': False}, config, client_factory=factory) == {}
    assert sources.collect(project, CommercialConfig(), client_factory=factory) == {}


def test_owner_focus_not_inferred_and_current_identity_hides_history(tmp_path):
    pipeline, pid, sources = fixture(tmp_path)
    assert sources.view(pid, CommercialConfig())['investment_status'] == 'watching'
    sources.set_focus(pid, 'invested')
    assert CommercialStore(pipeline.store).view(pid, CommercialConfig())['investment_status'] == 'invested'
    with pytest.raises(ValueError):
        sources.set_focus(pid, 'A轮')
    with closing(pipeline.store._connect()) as db:
        assert db.execute('SELECT count(*) FROM studio_commercial_preferences').fetchone()[0] == 1


def test_http_read_is_free_and_refresh_is_queued_not_a_synchronous_paid_call(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post('/api/studio/projects', json={'entity': {'legal_name': COMPANY}, 'consent': True}).json()['id']
    assert client.get(f'/api/studio/projects/{pid}/commercial').json()['records'] == []
    assert client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'commercial'}).status_code == 409
    client.put('/api/studio/settings', json={'commercial': {'providers': ['xiniu'], 'keys': {'xiniu': KEY}}})
    response = client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'commercial'})
    assert response.status_code == 202 and response.json()['kind'] == 'commercial'
    assert client.get(f'/api/studio/projects/{pid}/commercial').json()['records'] == []
    assert client.put(f'/api/studio/projects/{pid}/commercial/preference', json={'investment_status': 'invested'}).status_code == 200
    assert client.put(f'/api/studio/projects/{pid}/commercial/preference', json={'investment_status': 'A轮'}).status_code == 422
    client.patch(f'/api/studio/projects/{pid}', json={'status': 'paused'})
    assert client.post(f'/api/studio/projects/{pid}/refresh', json={'kind': 'commercial'}).status_code == 409


def test_paused_commercial_job_is_not_claimed(tmp_path):
    pipeline, pid, _ = fixture(tmp_path)
    pipeline.store.enqueue(pid, 'commercial')
    pipeline.store.set_status(pid, 'paused')
    assert pipeline.store.claim() is None


def test_concurrent_claim_coalesces_one_paid_request(tmp_path):
    import threading
    pipeline, pid, service = fixture(tmp_path)
    cfg = CommercialConfig(['xiniu'], {'xiniu': KEY})
    project = pipeline.store.get_project(pid)
    entered, release, calls = threading.Event(), threading.Event(), []
    def factory(*args):
        class Client:
            def fetch(self, name, aliases, end):
                calls.append(name)
                entered.set()
                assert release.wait(5)
                return normalize('xiniu', xiniu(), name, aliases)
        return Client()
    thread = threading.Thread(target=lambda: service.collect(project, cfg, client_factory=factory))
    thread.start()
    try:
        assert entered.wait(5)
        second = CommercialStore(pipeline.store).collect(project, cfg, client_factory=factory)
        assert second['xiniu']['status'] == 'held'
    finally:
        release.set()
        thread.join(5)
    assert len(calls) == 1


def test_lease_expiry_cannot_publish_a_paid_result_and_completed_response_replays(tmp_path):
    from test_studio_job_recovery import expire

    from project_watch.studio.execution import JobExecution
    pipeline, pid, service = fixture(tmp_path)
    store = pipeline.store
    store.enqueue(pid, 'commercial')
    job = store.claim()
    execution = JobExecution(store, job, budgets={'commercial': 9})
    calls = []
    def factory(*args):
        class Client:
            def fetch(self, name, aliases, end):
                calls.append(name)
                return normalize('xiniu', xiniu(), name, aliases)
        return Client()
    cfg = CommercialConfig(['xiniu'], {'xiniu': KEY})
    with store.write_scope(execution._guard):
        service.collect(store.get_project(pid), cfg, execution=execution, client_factory=factory)
    # Simulate crash after journal completion but before cache result write.
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_commercial_calls SET status='started',data='{}' WHERE project_id=?", (pid,))
    expire(store, job)
    recovered = store.claim()
    recovered_execution = JobExecution(store, recovered, budgets={'commercial': 9})
    with store.write_scope(recovered_execution._guard):
        service.collect(store.get_project(pid), cfg, execution=recovered_execution,
                        client_factory=lambda *args: pytest.fail('must replay the completed result'))
    assert len(calls) == 1 and len(service.view(pid, cfg)['records']) == 2
    with pytest.raises(RuntimeError), store.write_scope(execution._guard):
        service.set_focus(pid, 'invested')


def test_identity_change_hides_old_provider_records(tmp_path):
    pipeline, pid, service = fixture(tmp_path)
    cfg = CommercialConfig(['xiniu'], {'xiniu': KEY})
    class Client:
        def fetch(self, name, aliases, end):
            return normalize('xiniu', xiniu(), name, aliases)
    service.collect(pipeline.store.get_project(pid), cfg, client_factory=lambda *args: Client())
    pipeline.store.upsert_project(pid, {'legal_name': '其他合成公司'}, consent=False)
    assert service.view(pid, cfg)['records'] == []


def test_public_targets_reject_private_primary_and_candidate_peers(tmp_path):
    from project_watch.studio.commercial import targets
    from project_watch.studio.monitoring_contracts import ObjectInput
    pipeline, pid, _ = fixture(tmp_path)
    obj = pipeline.objects.ensure_subject(pid)
    pipeline.objects.save(pid, ObjectInput(kind='company', name='未核验候选公司', visibility='public'), actor='user')
    project = pipeline.store.get_project(pid)
    assert [r['legal_name'] for r in targets(pipeline, project)] == [COMPANY]
    pipeline.objects.save(pid, ObjectInput(kind='company', name=COMPANY, purpose='subject', visibility='internal'),
                          object_id=obj['id'], expected_version=obj['version'], actor='user')
    assert targets(pipeline, project) == []


def test_transport_refuses_unknown_hosts_without_network():
    from project_watch.studio.commercial import mcp_transport
    send = mcp_transport()
    for url in ('http://vip.xiniudata.com/mcp', 'https://evil.test/mcp',
                'https://vip.xiniudata.com:444/mcp', 'https://vip.xiniudata.com/other'):
        with pytest.raises(HttpError, match='commercial_destination'):
            send('POST', url, {}, b'{}', 1)


def test_wind_never_fabricates_funding_rows_from_news_and_links_are_safe():
    data = reply({'data': {'items': [{'title': COMPANY + '宣布融资', 'content': '模糊金额数千万',
        'date': '2026-06-01', 'url': 'javascript:alert(1)'}, {'title': '别家公司融资', 'content': '行业进展'}]}})
    result = normalize('wind', data, COMPANY, [])
    assert len(result['records']) == 1 and result['records'][0]['kind'] == 'news'
    assert result['records'][0]['amount'] == '' and result['records'][0]['url'] == ''
    assert result['rejected'] == 1


def test_unknown_date_and_different_rounds_do_not_fabricate_date_conflict():
    data = xiniu()
    raw = json.loads(data['content'][0]['text'])
    raw['融资事件详情'][0]['融资日期'] = '未知'
    assert normalize('xiniu', reply(raw), COMPANY, [])['records'][0]['date'] == ''


def test_vendor_legal_intro_can_locate_subject_without_guessing_a_brand():
    result = normalize('pharmcube', reply({'total': 2, 'data': [
        {'company_name': '星舟', 'company_intro': COMPANY + '，成立于2025年。', 'amount_str': '数千万'},
        {'company_name': '星舟', 'company_intro': '别家公司与' + COMPANY + '合作。'}]}), COMPANY, [])
    assert len(result['records']) == 1 and result['rejected'] == 1


def test_daily_intent_limit_is_global_and_cached_reads_remain_free(tmp_path):
    pipeline, pid, service = fixture(tmp_path)
    cfg = CommercialConfig(['xiniu'], {'xiniu': KEY}, daily_limit=3)
    class Client:
        def fetch(self, name, aliases, end):
            return normalize('xiniu', xiniu(name), name, aliases)
    for index in range(3):
        config = CommercialConfig(['xiniu'], {'xiniu': KEY + str(index)}, daily_limit=3)
        service.collect(pipeline.store.get_project(pid), config, client_factory=lambda *args: Client())
    result = service.collect(pipeline.store.get_project(pid), cfg,
        client_factory=lambda *args: pytest.fail('global cap must prevent any provider request'))
    assert result['xiniu']['status'] == 'limited'
    assert service.view(pid, cfg)['daily_queries']['remaining'] == 0
    cached = service.collect(pipeline.store.get_project(pid), CommercialConfig(['xiniu'], {'xiniu': KEY + '0'}, 3),
        client_factory=lambda *args: pytest.fail('cached read is free'))
    assert cached['xiniu']['cached']


def test_unknown_outcomes_do_not_automatically_repeat_next_day(tmp_path, monkeypatch):
    from datetime import date

    from project_watch.studio import commercial
    pipeline, pid, service = fixture(tmp_path)
    cfg = CommercialConfig(['xiniu'], {'xiniu': KEY})
    class Broken:
        def fetch(self, *args):
            raise HttpError('commercial_connection')
    service.collect(pipeline.store.get_project(pid), cfg, client_factory=lambda *args: Broken())
    class Tomorrow(date):
        @classmethod
        def today(cls):
            from datetime import timedelta
            return date.today() + timedelta(days=1)
    monkeypatch.setattr(commercial, 'date', Tomorrow)
    result = service.collect(pipeline.store.get_project(pid), cfg,
        client_factory=lambda *args: pytest.fail('uncertain source call must stay held across days'))
    assert result['xiniu']['status'] == 'held'


def test_24_hour_cache_survives_calendar_midnight(tmp_path, monkeypatch):
    from datetime import date, timedelta

    from project_watch.studio import commercial
    pipeline, pid, service = fixture(tmp_path)
    cfg = CommercialConfig(['xiniu'], {'xiniu': KEY})
    class Client:
        def fetch(self, name, aliases, end):
            return normalize('xiniu', xiniu(name), name, aliases)
    service.collect(pipeline.store.get_project(pid), cfg, client_factory=lambda *args: Client())
    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date.today() + timedelta(days=1)
    monkeypatch.setattr(commercial, 'date', Tomorrow)
    result = service.collect(pipeline.store.get_project(pid), cfg,
        client_factory=lambda *args: pytest.fail('midnight must not repeat a cached paid query'))
    assert result['xiniu']['cached']


def test_one_failing_source_does_not_fail_the_whole_update(tmp_path, monkeypatch):
    """希格生科: 烯牛 errored (cached for the day) while 医药魔方/Wind answered — the task is complete, not failed."""
    import pytest
    from fastapi.testclient import TestClient

    from project_watch.studio import commercial
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    pid = TestClient(app).post("/api/studio/projects", json={"entity": {"legal_name": "合成公司"}, "consent": True}).json()["id"]
    monkeypatch.setattr(commercial, "targets", lambda *_a: [{"legal_name": "合成公司"}])
    outcomes = {"xiniu": {"status": "error", "cached": True}, "pharmcube": {"status": "ok", "cached": False}}
    monkeypatch.setattr(commercial.CommercialStore, "collect", lambda *_a, **_k: dict(outcomes))
    pipeline.run_commercial({"id": "j", "project_id": pid})  # no exception: partial success
    outcomes["pharmcube"] = {"status": "error", "cached": False}
    with pytest.raises(ValueError, match="专业来源暂不可用"):
        pipeline.run_commercial({"id": "j", "project_id": pid})
