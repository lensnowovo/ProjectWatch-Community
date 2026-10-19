"""Per-reply output ceiling: configurable, stepped down to what the provider accepts. Synthetic, no network."""

from __future__ import annotations

import json

import pytest

from project_watch.studio import llm as llm_mod
from project_watch.studio.http import Http, Response
from project_watch.studio.llm import LLM, ModelError
from project_watch.studio.settings import ModelConfig, SettingsStore


def _ok(text='{"ok": true}', finish="stop"):
    return Response(200, {}, json.dumps({"choices": [{"message": {"content": text}, "finish_reason": finish}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode())


class Provider:
    """Accepts max_tokens up to `ceiling`; above it answers 400 like DeepSeek does."""

    def __init__(self, ceiling, reply=None):
        self.ceiling, self.reply, self.asked = ceiling, reply or _ok(), []

    def __call__(self, method, url, headers, body, timeout):
        asked = json.loads(body)["max_tokens"]
        self.asked.append(asked)
        if asked > self.ceiling:
            msg = f"Invalid max_tokens value, the valid range of max_tokens is [1, {self.ceiling}]"
            return Response(400, {}, json.dumps({"error": {"message": msg}}).encode())
        return self.reply


@pytest.fixture(autouse=True)
def fresh_ceilings():
    llm_mod._CEILINGS.clear()
    yield
    llm_mod._CEILINGS.clear()


def _config(**kw):
    return ModelConfig(base_url="https://model.example/v1", model="m", api_key="k", **kw)


def test_requests_the_configured_ceiling_and_steps_down_once_per_model():
    provider = Provider(ceiling=8192)
    model = LLM(_config(), Http(provider))
    assert model.json("s", "u", max_tokens=6000) == {"ok": True}
    assert provider.asked == [1000000, 8192]  # stated ceiling taken from the error text
    model.json("s", "u", max_tokens=1500)
    assert provider.asked[-1] == 8192  # remembered: no second rejection
    LLM(_config(), Http(provider)).json("s", "u")
    assert provider.asked[-1] == 8192  # also for new clients of the same endpoint and model


def test_unhelpful_errors_halve_until_the_provider_accepts():
    asked = []

    def provider(method, url, headers, body, timeout):
        asked.append(json.loads(body)["max_tokens"])
        if asked[-1] > 30000:
            return Response(400, {}, b'{"error":{"message":"max_tokens too large"}}')
        return _ok()

    LLM(_config(), Http(provider)).json("s", "u", max_tokens=6000)
    assert asked == [1000000, 500000, 250000, 125000, 62500, 31250, 15625]


def test_configured_and_provider_limits_override_legacy_call_budget():
    provider = Provider(ceiling=4096)
    model = LLM(_config(max_output_tokens=8000), Http(provider))
    assert model.json("s", "u", max_tokens=16000) == {"ok": True}
    assert provider.asked == [8000, 4096]
    model.json("s", "u", max_tokens=16000)
    assert provider.asked[-1] == 4096


def test_rejected_budgets_are_not_cached():
    def provider(method, url, headers, body, timeout):
        asked = json.loads(body)["max_tokens"]
        if asked > 8192:
            return Response(400, {}, b'{"error":{"message":"max_tokens valid range [1, 8192]"}}')
        return Response(401, {}, b'{}')

    with pytest.raises(ModelError):
        LLM(_config(), Http(provider)).json("s", "u")
    assert llm_mod._CEILINGS == {}


def test_anthropic_truncation_has_the_same_retry_signal():
    def provider(method, url, headers, body, timeout):
        return Response(200, {}, json.dumps({"content": [{"type": "text", "text": '{"a": "'}],
                                           "stop_reason": "max_tokens", "usage": {}}).encode())

    with pytest.raises(ModelError) as exc:
        LLM(_config(protocol="anthropic"), Http(provider)).json("s", "u")
    assert exc.value.code == "model_output_truncated"


def test_accepted_limits_are_scoped_to_protocol():
    first = Provider(ceiling=4096)
    LLM(_config(), Http(first)).json("s", "u")
    requested = []

    def anthropic(method, url, headers, body, timeout):
        requested.append(json.loads(body)["max_tokens"])
        return Response(200, {}, b'{"content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn"}')

    assert LLM(_config(protocol="anthropic"), Http(anthropic)).complete("s", "u") == "ok"
    assert requested == [1000000]


def test_other_bad_requests_are_not_retried():
    def provider(method, url, headers, body, timeout):
        return Response(400, {}, b'{"error":{"message":"messages must not be empty"}}')

    with pytest.raises(ModelError) as exc:
        LLM(_config(), Http(provider)).complete("s", "u")
    assert exc.value.code == "model_http_failure"


def test_truncation_message_names_the_ceiling():
    provider = Provider(ceiling=200000, reply=_ok('{"a": "', finish="length"))
    with pytest.raises(ModelError) as exc:
        LLM(_config(max_output_tokens=16000), Http(provider)).json("s", "u", max_tokens=6000)
    assert exc.value.code == "model_output_truncated" and "16000" in str(exc.value)


def test_setting_is_validated_and_shown(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    assert store.load().model.max_output_tokens == 1000000
    store.update({"model": {"max_output_tokens": 32000}})
    assert SettingsStore.public_view(store.load())["model"]["max_output_tokens"] == 32000
    with pytest.raises(ValueError):
        store.update({"model": {"max_output_tokens": 999}})


def test_million_output_setting_and_saved_lower_limit(tmp_path):
    store = SettingsStore(tmp_path / "million.json")
    assert store.load().model.max_output_tokens == 1_000_000
    store.update({"model": {"max_output_tokens": 1_000_000}})
    assert SettingsStore.public_view(store.load())["model"]["max_output_tokens"] == 1_000_000
    store.update({"model": {"max_output_tokens": 32000}})
    assert SettingsStore(store.path).load().model.max_output_tokens == 32000


def test_million_negotiation_is_bounded_and_cached():
    provider = Provider(ceiling=128000)
    llm = LLM(_config(max_output_tokens=1_000_000), Http(provider))
    assert llm.json("s", "u") == {"ok": True}
    assert provider.asked == [1_000_000, 128000]
    llm.json("s", "u")
    assert provider.asked == [1_000_000, 128000, 128000]
