"""Gateway and actual pinned DSH protocol tests; synthetic transport only."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import test_studio_dsh_bootstrap as fixtures

from project_watch.studio.dsh_runtime import (
    Gateway,
    child_environment,
    compact_context_messages,
    guarded_http,
    profile_patch,
    run_dsh,
)
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import ModelConfig


@pytest.fixture
def research(tmp_path):
    return fixtures.research.__wrapped__(tmp_path)


def gateway_for(research, transport):
    model = ModelConfig(base_url="https://model.example/v1", model="synthetic-model", api_key="synthetic-only")
    http = research.execution.http(Http(transport), lambda _url: ("model", "synthetic"))
    return Gateway(research, model, http)


def payload():
    return {"model": "synthetic-model", "messages": [{"role": "user", "content": "synthetic"}],
            "tools": [{"type": "function", "function": {"name": "pw_search"}}]}


def test_context_compaction_preserves_unique_evidence_and_protocol():
    def turn(identifier, data):
        return [{"role": "assistant", "tool_calls": [{"id": identifier, "function": {"name": "pw_context"}}]},
                {"role": "tool", "tool_call_id": identifier, "content": json.dumps(data)}]
    evidence = {"N1": {"id": "one", "text": "意向订单，尚未确认收入" * 100, "report_period": "2025"}}
    messages = [{"role": "system", "content": "never erase"}, {"role": "user", "content": "keep"}]
    messages += turn("first", {"fingerprint": "a", "numbered": evidence})
    messages += [{"role": "tool", "tool_call_id": "read", "content": "unique raw body"}]
    messages += turn("last", {"fingerprint": "b", "numbered": {**evidence, "N2": {"id": "two"}}})
    saved = json.dumps(messages)
    compacted = compact_context_messages(messages)
    assert json.dumps(messages) == saved
    assert compacted[:-1] == messages[:-1]
    assert len(compacted) == len(messages)
    assert json.loads(compacted[-1]["content"])["numbered"]["N1"]["same_as"] == {
        "tool_call_id": "first", "ref": "N1"}
    assert len(json.dumps(compacted)) < len(saved) * .8
    # Changed values or reused citation numbers are never considered duplicates.
    messages[-1]["content"] = json.dumps({"fingerprint": "c", "numbered": {"N1": {"id": "different"}}})
    assert compact_context_messages(messages) == messages


def test_context_compaction_is_prefix_stable_and_lossless():
    def turn(identifier, name, data):
        return [{"role": "assistant", "tool_calls": [{"id": identifier, "function": {"name": name}}]},
                {"role": "tool", "tool_call_id": identifier, "content": json.dumps(data, ensure_ascii=False)}]
    body = "2025年意向订单；2026年尚未确认收入。" * 50
    first = {"url": "https://example.com/one", "context": body, "id": "one"}
    second = {**first, "id": "two"}
    other = {**first, "url": "https://example.com/other", "id": "other"}
    ctx = {"fingerprint": "one", "numbered": {"N1": first, "N2": second, "N3": other}}
    messages = turn("c1", "pw_context", ctx)
    prefix = compact_context_messages(messages)
    assert json.loads(prefix[-1]["content"])["numbered"]["N2"]["context_same_as"] == {
        "tool_call_id": "c1", "ref": "N1"}
    assert json.loads(prefix[-1]["content"])["numbered"]["N3"] == other
    read = {"id": "S1", "url": first["url"], "window_offset": 0, "excerpt": body, "read_scope": "partial"}
    messages += turn("r1", "pw_read", read) + turn("r2", "pw_read", read)
    messages += turn("r3", "pw_read", {**read, "window_offset": 100})
    messages += turn("c2", "pw_context", {**ctx, "fingerprint": "two"})
    compacted = compact_context_messages(messages)
    assert compacted[:2] == prefix
    assert json.loads(compacted[5]["content"])["excerpt_same_as"] == {"tool_call_id": "r1"}
    assert compacted[7] == messages[7]  # Distinct read window is not shared.
    # Resolve only earlier tool results/entries; every data value is recoverable.
    resolved = {}
    for original, actual in zip(messages, compacted, strict=True):
        if original["role"] != "tool":
            assert original == actual
            continue
        data = json.loads(actual["content"])
        if "numbered_same_as" in data:
            p = data.pop("numbered_same_as")
            data["numbered"] = resolved[p["tool_call_id"]]["numbered"]
        for ref, entry in list(data.get("numbered", {}).items()):
            if "same_as" in entry:
                p = entry["same_as"]
                entry = dict(resolved[p["tool_call_id"]]["numbered"][p["ref"]])
                data["numbered"][ref] = entry
            if "context_same_as" in entry:
                p = entry.pop("context_same_as")
                target = data if p["tool_call_id"] == original["tool_call_id"] else resolved[p["tool_call_id"]]
                entry["context"] = target["numbered"][p["ref"]]["context"]
        if "excerpt_same_as" in data:
            p = data.pop("excerpt_same_as")
            data["excerpt"] = resolved[p["tool_call_id"]]["excerpt"]
        assert data == json.loads(original["content"])
        resolved[original["tool_call_id"]] = data


def test_gateway_unknown_stops_all_new_requests_and_records_missing_usage(research):
    calls = []
    def lost(*args):
        calls.append(1)
        raise HttpError("connection_failed")
    gateway = gateway_for(research, lost)
    with pytest.raises(HttpError):
        gateway.model_call(payload())
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        gateway.model_call({**payload(), "messages": [{"role": "user", "content": "different"}]})
    assert len(calls) == 1
    assert research.execution.usage()["model"]["unknown"] == 1


def test_model_and_tools_auth_and_no_browser_origin(research):
    gateway = gateway_for(research, lambda *a: pytest.fail("network must not be reached"))
    port = gateway.start()
    try:
        for headers, status in (({}, 401), ({"Authorization": "Bearer " + gateway.token,
                                           "Origin": "https://untrusted.example"}, 403)):
            request = urllib.request.Request(f"http://127.0.0.1:{port}/tools/pw_status",
                                             data=b"{}", headers=headers)
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            assert error.value.code == status
    finally:
        gateway.close()


def test_profile_blocks_shell_files_and_retries(tmp_path, monkeypatch):
    patch = json.loads(profile_patch(1234, "test", 1000000))
    disabled = {r["id"] for r in patch if r.get("disabled")}
    assert {"persistent-pwsh", "persistent-bash", "tool-fs", "tool-working-directory"} <= disabled
    route = patch[-1]["insert"][0]["config"]["providers"]["projectwatch"]
    assert route["retryPolicy"]["maxRetries"] == 0
    monkeypatch.setenv("SECRET_VENDOR_API_KEY", "do-not-forward")
    env = child_environment(tmp_path, 1234, "local-only")
    assert "SECRET_VENDOR_API_KEY" not in env
    assert env["DSH_HOME"] == str(tmp_path / "home")


def test_unexpected_tool_exposure_fails_before_model_dispatch(research):
    gateway = gateway_for(research, lambda *a: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="tool exposure"):
        gateway.model_call({**payload(), "tools": [{"function": {"name": "pwsh"}}]})


@pytest.mark.parametrize('path,expected', [
    (PureWindowsPath(r'D:\App Dir\公司\dsh_tools.mjs'), 'file:///D:/App%20Dir/%E5%85%AC%E5%8F%B8/dsh_tools.mjs'),
    (PureWindowsPath(r'\\?\D:\App Dir\公司\dsh_tools.mjs'), 'file:///D:/App%20Dir/%E5%85%AC%E5%8F%B8/dsh_tools.mjs'),
    (PureWindowsPath(r'\\?\UNC\server\share\a#b%\dsh_tools.mjs'), 'file://server/share/a%23b%25/dsh_tools.mjs'),
    (PureWindowsPath(r'\\server\share\dsh_tools.mjs'), 'file://server/share/dsh_tools.mjs'),
    (PurePosixPath('/Applications/Project Watch/公司/dsh_tools.mjs'),
     'file:///Applications/Project%20Watch/%E5%85%AC%E5%8F%B8/dsh_tools.mjs'),
])
def test_plugin_uri_preserves_path_without_windows_device_namespace(path, expected):
    from project_watch.studio.dsh_runtime import plugin_file_uri

    assert plugin_file_uri(path) == expected


def test_plugin_uri_rejects_non_file_device_namespace():
    from project_watch.studio.dsh_runtime import plugin_file_uri

    with pytest.raises(ValueError, match='dsh_plugin_path_invalid'):
        plugin_file_uri(PureWindowsPath(r'\\?\GLOBALROOT\Device\HarddiskVolume1\plugin.mjs'))


@pytest.mark.parametrize('tools', [None, []])
def test_missing_tools_holds_before_any_billable_model_request(research, tools):
    from project_watch.studio.dsh_runtime import settled_error

    gateway = gateway_for(research, lambda *a: pytest.fail('missing plugin must not dispatch'))
    request = payload()
    if tools is None:
        request.pop('tools')
    else:
        request['tools'] = tools
    with pytest.raises(HttpError, match='dsh_tools_unavailable'):
        gateway.model_call(request)
    assert gateway.model_requests == 0
    assert research.execution.usage() == {}
    assert settled_error(research, 'dsh_runtime_error') == 'dsh_tools_unavailable'
    with pytest.raises(HttpError, match='dsh_tools_unavailable'):
        gateway.model_call(payload())


def test_missing_tools_advice_is_local_not_unknown():
    from project_watch.studio.taskboard import advice

    result = advice({'kind': 'external', 'error': 'dsh_tools_unavailable'})
    assert '工具' in result['why'] and '模型' in result['why']
    assert result['action'] == 'none'
    assert advice({'kind': 'external', 'error': 'dsh_tools_unavailable', 'unknown': 1})['action'] == 'recover'


def test_search_fallback_cannot_bypass_unknown_outcome(research):
    from project_watch.studio.settings import SearchConfig
    from project_watch.studio.sources import Collectors
    calls = []
    def lost(*args):
        calls.append(1)
        raise HttpError("connection_failed")
    original = research.execution.http(Http(lost), lambda _url: ("search", "synthetic"))
    research.collectors = Collectors(guarded_http(research, original),
        SearchConfig(providers=["bocha", "tavily"], keys={"bocha": "synthetic", "tavily": "synthetic"}))
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        research.tool("pw_search", {"query": "Synthetic Optics", "category": "company"})
    assert len(calls) == 1


def test_definitive_provider_failure_is_not_automatically_retried(research):
    calls = []
    def rejected(*args):
        calls.append(1)
        return Response(500, {}, b'{"error":"synthetic"}')
    gateway = gateway_for(research, rejected)
    for _ in range(2):
        with pytest.raises(HttpError, match="model_http_500"):
            gateway.model_call(payload())
    assert len(calls) == 1


def test_hold_survives_adapter_recreation(research):
    from project_watch.studio.dsh_bootstrap import PublicBootstrap
    research.hold("request_outcome_unknown")
    restored = PublicBootstrap(research.pipeline, research.execution, research.collectors, research.reader)
    gateway = gateway_for(restored, lambda *a: pytest.fail("held run must not dispatch"))
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        gateway.model_call(payload())


def test_completed_model_receipt_replays_without_new_dispatch(research):
    calls = []
    def completed(*args):
        calls.append(1)
        return Response(200, {}, json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "synthetic"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode())
    gateway = gateway_for(research, completed)
    assert gateway.model_call(payload()) == gateway.model_call(payload())
    assert len(calls) == 1
    assert research.execution.usage()["model"]["completed"] == 1


def test_client_wait_exceeds_upstream_wait():
    from project_watch.studio.dsh_runtime import MODEL_TIMEOUT_SECONDS
    route = json.loads(profile_patch(1234, "test", 1000))[-1]["insert"][0]["config"]["providers"]["projectwatch"]
    assert route["streamIdleTimeoutMs"] > MODEL_TIMEOUT_SECONDS * 1000
    assert route["timeoutMs"] >= route["streamIdleTimeoutMs"]
    assert route["retryPolicy"]["maxRetries"] == 0


def test_local_request_budget_is_not_invalid_model_response(research):
    research.execution.budgets["model"] = 0
    gateway = gateway_for(research, lambda *a: pytest.fail("no paid dispatch"))
    with pytest.raises(HttpError, match="request_budget"):
        gateway.model_call(payload())
    assert research.execution.read_checkpoint("dsh_hold")["code"] == "request_budget"
    assert not research.execution.usage()


def test_received_invalid_response_remains_response_error(research):
    gateway = gateway_for(research, lambda *a: Response(200, {}, b'{"choices":[]}'))
    with pytest.raises(ValueError, match="invalid_model_response"):
        gateway.model_call(payload())
    assert research.execution.read_checkpoint("dsh_hold")["code"] == "model_response_invalid"
    assert research.execution.usage()["model"]["unknown"] == 0


def test_provider_backoff_retains_its_pre_dispatch_reason(research):
    first = gateway_for(research, lambda *a: Response(429, {}, b'{"error":"synthetic"}'))
    with pytest.raises(HttpError, match="model_http_429"):
        first.model_call(payload())
    research.execution.checkpoint("dsh_hold", {})
    second = gateway_for(research, lambda *a: pytest.fail("backoff must not dispatch"))
    with pytest.raises(HttpError, match="provider_backoff"):
        second.model_call({**payload(), "messages": [{"role": "user", "content": "different"}]})
    assert research.execution.read_checkpoint("dsh_hold")["code"] == "provider_backoff"


@pytest.mark.parametrize("late_unknown", [False, True])
def test_runtime_resolves_error_after_gateway_drain(research, tmp_path, monkeypatch, late_unknown):
    import io

    from project_watch.studio import dsh_runtime

    messages = [
        {"id": 1, "result": {"serverInfo": {"name": "deepseek-harness-sdk-runtime"}}},
        {"id": 2, "result": {"messageId": "synthetic"}},
        {"method": "session.event", "params": {"event": {"type": "turn/end",
            "data": {"reason": {"kind": "error", "error": {"code": "TIMEOUT", "message": "not persisted"}}}}}},
        {"method": "session.status", "params": {"status": "idle"}},
    ]
    class Process:
        def __init__(self, *a, **kw):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
        def poll(self):
            return 0
        def wait(self, **kwargs):
            return 0

    gateway = gateway_for(research, lambda *a: pytest.fail("no transport"))
    monkeypatch.setattr(gateway, "start", lambda: 1234)
    def drain():
        if late_unknown:
            research.hold("request_outcome_unknown")
    monkeypatch.setattr(gateway, "close", drain)
    monkeypatch.setattr(dsh_runtime, "runtime_entry", lambda _root: tmp_path / "synthetic.js")
    monkeypatch.setattr(dsh_runtime.shutil, "which", lambda _name: "synthetic-node")
    monkeypatch.setattr(dsh_runtime.subprocess, "Popen", Process)
    expected = "request_outcome_unknown" if late_unknown else "dsh_model_timeout"
    with pytest.raises(HttpError, match=expected):
        run_dsh(research, gateway, tmp_path, tmp_path / "run")
    receipt = research.execution.read_checkpoint("dsh_runtime")
    assert receipt["error"] == expected
    assert receipt["runtime_failure"] == "dsh_model_timeout"
    assert receipt["drained"] is True


def test_unknown_journal_wins_over_generic_hold_and_finished_state(research):
    from project_watch.studio.dsh_runtime import settled_error

    gateway = gateway_for(research, lambda *a: (_ for _ in ()).throw(HttpError("connection_failed")))
    with pytest.raises(HttpError):
        gateway.model_call(payload())
    research.hold("model_response_invalid")
    research.state["finished"] = {"status": "partial"}
    assert settled_error(research, "dsh_no_valid_submission") == "request_outcome_unknown"
    assert research.execution.usage()["model"]["unknown"] == 1


def test_drain_keeps_existing_lease_and_closed_gateway_refuses_new_dispatch(research, monkeypatch):
    import threading

    from project_watch.studio.dsh_runtime import drain_gateway

    heartbeaten = threading.Event()
    real = research.store.heartbeat
    def beat(job_id, token):
        assert token == research.execution.job["lease_token"]
        result = real(job_id, token)
        heartbeaten.set()
        return result
    monkeypatch.setattr(research.store, "heartbeat", beat)
    gateway = gateway_for(research, lambda *a: pytest.fail("must not dispatch"))
    def close():
        gateway.closing.set()
        assert heartbeaten.wait(2)
    monkeypatch.setattr(gateway, "close", close)
    drain_gateway(research, gateway)
    with pytest.raises(HttpError, match="execution_stopped"):
        gateway.model_call(payload())


def test_disconnected_client_does_not_mask_journal(research):
    gateway = gateway_for(research, lambda *a: pytest.fail("no network"))
    gateway.start()
    try:
        handler = gateway.server.RequestHandlerClass.__new__(gateway.server.RequestHandlerClass)
        handler.send_response = lambda *a: None
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        class ClosedSocket:
            def write(self, body):
                raise ConnectionAbortedError("synthetic disconnect")
        handler.wfile = ClosedSocket()
        handler.reply(409, {"error": "request_outcome_unknown", "terminal": True})
    finally:
        gateway.close()


def test_dsh_failure_advice_preserves_unknown_priority():
    from project_watch.studio.taskboard import advice

    job = {"kind": "external", "error": "dsh_model_timeout"}
    assert "等待" in advice(job)["why"]
    assert advice(job)["action"] == "none"
    assert advice({**job, "unknown": 1})["action"] == "recover"


def test_record_feedback_survives_http_tool_protocol(research):
    fixtures.call(research, "pw_search", query="synthetic", category="company")
    gateway = gateway_for(research, lambda *a: pytest.fail("no model transport"))
    port = gateway.start()
    try:
        args = {"kind": "business", "label": "订单", "source_id": "S1",
                "quote": "2025年意向订单为500万元。", "text": "2026年订单为900万元。"}
        request = urllib.request.Request(f"http://127.0.0.1:{port}/tools/pw_record",
            data=json.dumps(args).encode(), headers={"Authorization": "Bearer " + gateway.token})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        data = json.loads(error.value.read())
        assert data["error"] == "record_validation" and data["terminal"] is False
        assert {i["code"] for i in data["issues"]} >= {"unsupported_numbers", "missing_qualifications"}
        assert all(i["quote_numbers"] for i in data["issues"] if i["code"] == "unsupported_numbers")
        assert not research.state["records"]
    finally:
        gateway.close()


def test_unlabelled_table_value_does_not_prove_money_or_event_date():
    from project_watch.studio.dsh_bootstrap import RecordValidationError, validate_record

    source = {"id": "S1", "excerpt": "申请日期：2026-06-11；金额：25.00（详见表头）。",
              "published_at": "2026-09-29"}
    with pytest.raises(RecordValidationError) as error:
        validate_record(source, source["excerpt"], "9月29日拟融资25亿元。", "拟融资")
    issue = error.value.feedback()["issues"][0]
    assert "29日" in issue["unsupported"] and "9月" in issue["unsupported"]
    assert any("万元CNY" in value for value in issue["unsupported"])
    assert "25.00" in issue["quote_numbers"]


@pytest.mark.skipif(not os.environ.get("PW_DSH_TEST_RUNTIME"), reason="requires separately installed pinned official DSH")
@pytest.mark.parametrize('extended_plugin_path', [False, pytest.param(True,
    marks=pytest.mark.skipif(os.name != 'nt', reason='Windows installed path namespace'))])
def test_official_dsh_chooses_tools_and_saves_result(research, tmp_path, monkeypatch, extended_plugin_path):
    if extended_plugin_path:
        from project_watch.studio import dsh_runtime

        # Match embedded Python's installed __file__, including Unicode, spaces,
        # escaping and a path beyond MAX_PATH. Run the actual Node/DSH protocol.
        original = Path(dsh_runtime.__file__).with_name('dsh_tools.mjs')
        folder = tmp_path / '已安装 App #%' / ('version-' + 'a'*64) / ('package-' + 'b'*64)
        folder = Path('\\\\?\\' + str(folder.resolve()))
        folder.mkdir(parents=True)
        (folder/'dsh_tools.mjs').write_bytes(original.read_bytes())
        monkeypatch.setattr(dsh_runtime, '__file__', str(folder/'dsh_runtime.py'))
        assert len(str(folder/'dsh_tools.mjs')) > 260
    calls, reviews = [], []
    operations = [
        ("pw_search", {"query": "Synthetic Optics 产品", "category": "company"}),
        ("pw_read", {"source_id": "S1"}),
        ("pw_record", {"kind": "product", "label": "激光模组", "text": "Synthetic Optics 开发激光模组。",
                       "source_id": "S1", "quote": "Synthetic Optics 开发激光模组。"}),
        ("pw_finish", {"gaps": {"company": "仅取得产品资料", "people": "任职时间不明",
                               "competitor": "缺少竞品资料", "industry": "缺少行业资料", "policy": "缺少政策资料"}}),
    ]
    def scripted(method, url, headers, raw, timeout):
        request = json.loads(raw)
        if 'tools' not in request:
            from test_studio_dsh_monitor import synthetic_review
            payload = json.loads(request['messages'][-1]['content'])
            assert payload['purpose'] == 'public_record_admission'
            reviews.append(payload)
            return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(synthetic_review(payload))},
                'finish_reason': 'stop'}]}).encode())
        index = len(calls)
        calls.append(request)
        assert index < len(operations), "scripted provider received unexpected extra model call"
        tool, arguments = operations[index]
        names = {t["function"]["name"] for t in request["tools"]}
        assert names == {"pw_search", "pw_collect", "pw_read", "pw_record", "pw_status", "pw_finish"}
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"synthetic-{index}", "type": "function",
            "function": {"name": tool, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}
        return Response(200, {}, json.dumps({"id": f"response-{index}", "model": "synthetic-model",
            "choices": [{"message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode())
    result = run_dsh(research, gateway_for(research, scripted),
                     Path(os.environ["PW_DSH_TEST_RUNTIME"]), tmp_path / "runtime", timeout=120)
    assert result["status"] == "partial"
    assert len(calls) == 4
    assert [r['phase'] for r in reviews] == ['fields']
    assert research.execution.usage()["model"]["completed"] == 5
    assert research.execution.read_checkpoint("dsh_runtime")["initialized"]
    assert research.store.get_public_profile(research.project_id)["facts"]
