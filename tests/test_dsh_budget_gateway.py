"""The optional live-model gate must reject over-budget requests before egress."""

import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from io import BytesIO

import pytest

from scripts.dsh_budget_gateway import BudgetGate, BudgetPolicy, BudgetProxy, GateRejected
from scripts.dsh_live_probe import _probe_child_environment
from scripts.dsh_tool_turn_probe import _live_policy


def _body(*, max_tokens: int = 200, content: str = "synthetic only") -> bytes:
    return json.dumps(
        {
            "model": "deepseek-v4-flash",
            "stream": True,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
    ).encode()


def _gate(**overrides: object) -> BudgetGate:
    settings = {
        "model": "deepseek-v4-flash",
        "max_requests": 2,
        "max_request_bytes": 2_000,
        "max_output_tokens_per_request": 200,
        "max_cost_cny": Decimal("0.01"),
        "input_cny_per_million": Decimal("1"),
        "output_cny_per_million": Decimal("10"),
    }
    settings.update(overrides)
    return BudgetGate(BudgetPolicy(**settings))


def test_gate_rejects_missing_output_cap_model_mismatch_and_non_streaming() -> None:
    gate = _gate()
    for changes in (
        {"max_tokens": None},
        {"model": "other"},
        {"stream": False},
    ):
        request = json.loads(_body())
        request.update(changes)
        with pytest.raises(GateRejected):
            gate.admit(json.dumps(request).encode())
    assert gate.receipt()["requests_admitted"] == 0


def test_live_policy_requires_explicit_finite_budget_and_model(monkeypatch) -> None:
    for name in (
        "PROJECT_WATCH_DEEPSEEK_API_KEY",
        "PROJECT_WATCH_DSH_MODEL",
        "PROJECT_WATCH_DSH_MAX_CNY",
        "PROJECT_WATCH_DSH_INPUT_CNY_PER_MILLION",
        "PROJECT_WATCH_DSH_OUTPUT_CNY_PER_MILLION",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="model and provider key"):
        _live_policy()
    monkeypatch.setenv("PROJECT_WATCH_DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("PROJECT_WATCH_DSH_MODEL", "test-model")
    with pytest.raises(ValueError, match="CNY cap and both model rates"):
        _live_policy()
    monkeypatch.setenv("PROJECT_WATCH_DSH_MAX_CNY", "NaN")
    monkeypatch.setenv("PROJECT_WATCH_DSH_INPUT_CNY_PER_MILLION", "1")
    monkeypatch.setenv("PROJECT_WATCH_DSH_OUTPUT_CNY_PER_MILLION", "10")
    with pytest.raises(ValueError, match="positive"):
        _live_policy()


def test_probe_children_do_not_inherit_model_credentials(monkeypatch) -> None:
    for name in (
        "PROJECT_WATCH_DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.setenv(name, "must-not-leak")
    monkeypatch.setenv("PROJECT_WATCH_TEST_MARKER", "kept")
    child = _probe_child_environment()
    assert child["PROJECT_WATCH_TEST_MARKER"] == "kept"
    assert "must-not-leak" not in child.values()


def test_gate_rejects_size_token_call_and_cost_limits_before_egress() -> None:
    gate = _gate()
    with pytest.raises(GateRejected, match="request size"):
        gate.admit(_body(content="x" * 3_000))
    with pytest.raises(GateRejected, match="output tokens"):
        gate.admit(_body(max_tokens=201))
    with pytest.raises(GateRejected, match="cost ceiling"):
        _gate(max_cost_cny=Decimal("0.00001")).admit(_body())
    gate.admit(_body())
    gate.admit(_body())
    with pytest.raises(GateRejected, match="request count"):
        gate.admit(_body())
    assert gate.receipt()["requests_admitted"] == 2


def test_gate_receipt_contains_usage_not_prompt_or_credentials() -> None:
    gate = _gate()
    gate.admit(_body(content="synthetic private marker"))
    gate.record_response(
        b'data: {"usage":{"prompt_tokens":12,"completion_tokens":7}}\n\ndata: [DONE]\n\n'
    )
    receipt = gate.receipt()
    assert receipt["observed_prompt_tokens"] == 12
    assert receipt["observed_completion_tokens"] == 7
    assert "synthetic private marker" not in json.dumps(receipt)
    assert "api_key" not in json.dumps(receipt)


def test_proxy_keeps_provider_key_upstream_and_rejects_before_egress() -> None:
    class Opener:
        def __init__(self) -> None:
            self.requests: list[urllib.request.Request] = []

        def open(self, request: urllib.request.Request, *, timeout: int) -> BytesIO:
            assert timeout == 60
            self.requests.append(request)
            return BytesIO(
                b'data: {"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\ndata: [DONE]\n\n'
            )

    opener = Opener()
    gate = _gate(max_requests=1)
    proxy = BudgetProxy(
        gate,
        upstream_url="https://api.deepseek.com/chat/completions",
        provider_key="provider-secret-test",
        local_token="loopback-only-test",
        opener=opener,
    )
    server = proxy.server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(url, data=b"", method="POST")
        with pytest.raises(urllib.error.HTTPError) as denied:
            client.open(request, timeout=5)
        assert denied.value.code == 401
        assert not opener.requests

        request = urllib.request.Request(url, _body(), method="POST")
        request.add_header("Authorization", "Bearer loopback-only-test")
        with client.open(request, timeout=5) as response:
            assert response.status == 200
            assert b'"prompt_tokens":3' in response.read()
        assert len(opener.requests) == 1
        forwarded = opener.requests[0]
        assert forwarded.get_header("Authorization") == "Bearer provider-secret-test"
        assert forwarded.get_header("Content-type") == "application/json"

        with pytest.raises(urllib.error.HTTPError) as denied:
            client.open(request, timeout=5)
        assert denied.value.code == 429
        assert len(opener.requests) == 1
        receipt = json.dumps(gate.receipt())
        assert "provider-secret-test" not in receipt
        assert "loopback-only-test" not in receipt
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_anthropic_stream_usage_merges_start_delta_and_cached_input():
    gate = _gate(max_cost_cny=None, input_cny_per_million=None, output_cny_per_million=None)
    gate.admit(_body())
    gate.record_response(
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":10,"cache_read_input_tokens":8,"output_tokens":1}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":25}}\n\n'
    )
    receipt = gate.receipt()
    assert receipt["observed_prompt_tokens"] == 18
    assert receipt["observed_completion_tokens"] == 25
    assert receipt["requests_with_usage"] == 1
    assert receipt["budget_mode"] == "request_quota"
    assert receipt["observed_usage_estimate_cny"] is None
    gate.admit(_body())
    with pytest.raises(GateRejected, match="request count"):
        gate.admit(_body())


def test_partial_monetary_policy_is_rejected():
    with pytest.raises(ValueError, match="together"):
        _gate(max_cost_cny=None)


def test_anthropic_forward_uses_native_wire_and_key_only_upstream():
    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url == "https://example.test/v1/messages"
            assert request.get_header("X-api-key") == "synthetic-provider-secret"
            assert request.get_header("Anthropic-version") == "2023-06-01"
            assert json.loads(request.data)["max_tokens"] == 200
            return BytesIO(b'data: {"type":"message_delta","usage":{"output_tokens":1}}\n\n')

    proxy = BudgetProxy(
        _gate(),
        upstream_url="https://example.test/v1/messages",
        provider_key="synthetic-provider-secret",
        local_token="synthetic-local",
        protocol="anthropic-messages",
        opener=Opener(),
    )
    proxy.forward(_body())
    assert "synthetic-provider-secret" not in json.dumps(proxy.gate.receipt())


def test_anthropic_proxy_accepts_sdk_beta_path_but_not_arbitrary_queries():
    class Opener:
        calls = 0

        def open(self, request, *, timeout):
            self.calls += 1
            assert request.full_url == "https://example.test/v1/messages"
            return BytesIO(b'data: {"type":"message_stop"}\n\n')

    opener = Opener()
    proxy = BudgetProxy(
        _gate(),
        upstream_url="https://example.test/v1/messages",
        provider_key="synthetic-provider",
        local_token="synthetic-local",
        protocol="anthropic-messages",
        opener=opener,
    )
    server = proxy.server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        base = f"http://127.0.0.1:{server.server_port}/v1/messages"
        for suffix in ("", "?beta=true"):
            request = urllib.request.Request(
                base + suffix, data=_body(), headers={"x-api-key": "synthetic-local"}
            )
            with client.open(request, timeout=5) as response:
                assert response.status == 200
        assert opener.calls == 2
        request = urllib.request.Request(
            base + "?redirect=elsewhere", data=_body(), headers={"x-api-key": "synthetic-local"}
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            client.open(request, timeout=5)
        assert error.value.code == 404
        assert opener.calls == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_explicit_anthropic_thinking_disable_and_safe_diagnostics():
    class Opener:
        def open(self, request, *, timeout):
            assert json.loads(request.data)["thinking"] == {"type": "disabled"}
            return BytesIO(
                b'data: {"type":"content_block_start","content_block":'
                b'{"type":"thinking","thinking":"DO-NOT-LOG"}}\n\n'
                b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
                b'"usage":{"output_tokens":200}}\n\n'
            )

    proxy = BudgetProxy(
        _gate(),
        upstream_url="https://example.test/v1/messages",
        provider_key="synthetic-key",
        local_token="synthetic-token",
        protocol="anthropic-messages",
        thinking_mode="disabled",
        opener=Opener(),
    )
    proxy.forward(_body())
    assert proxy.gate.receipt()["response_block_types"] == ["thinking"]
    assert proxy.gate.receipt()["stop_reasons"] == ["max_tokens"]
    assert "DO-NOT-LOG" not in json.dumps(proxy.gate.receipt())
    for body in (b"[]", b"not json", b"x" * 2001):
        with pytest.raises(GateRejected):
            proxy.forward(body)
    assert proxy.gate.receipt()["requests_admitted"] == 1
