"""Model configuration is explicit, secret-free and protocol-specific."""

import json

import pytest

from project_watch.model_provider import ModelProviderConfig


@pytest.mark.parametrize(
    "base,protocol,expected",
    [
        (
            "https://example.test/v1/",
            "openai-completions",
            "https://example.test/v1/chat/completions",
        ),
        (
            "https://example.test/api/anthropic",
            "anthropic-messages",
            "https://example.test/api/anthropic/v1/messages",
        ),
        ("https://example.test/v1", "anthropic-messages", "https://example.test/v1/messages"),
    ],
)
def test_protocol_endpoint(base, protocol, expected):
    assert ModelProviderConfig(base, protocol, "synthetic-model").upstream_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/v1",
        "https://user:password@example.test/v1",
        "https://example.test/v1?key=secret",
        "https://example.test/v1#secret",
        "https://example.test/\nheader",
        "https:///v1",
    ],
)
def test_reject_unsafe_url(url):
    with pytest.raises(ValueError):
        ModelProviderConfig(url, "openai-completions", "synthetic-model")


def test_config_does_not_accept_arbitrary_protocol_or_credentials_as_reference():
    with pytest.raises(ValueError, match="protocol"):
        ModelProviderConfig("https://example.test", "shell", "test")
    with pytest.raises(ValueError, match="environment variable"):
        ModelProviderConfig("https://example.test", "openai-completions", "test", "secret-key")


def test_patch_cannot_escape_yaml_and_only_points_at_local_proxy():
    model = 'model:!!js-"injected"'
    config = ModelProviderConfig("https://example.test", "anthropic-messages", model)
    patch = config.dsh_proxy_patch(12345)
    route = json.loads(patch.split("      config: ")[1])["providers"]["project-watch-model"]
    assert route["models"][0]["id"] == model
    assert route["apiKeyEnv"] == "PROJECT_WATCH_MODEL_PROXY_TOKEN"
    assert route["baseURL"] == "http://127.0.0.1:12345"
    assert route["retryPolicy"]["maxRetries"] == 0
    assert "example.test" not in patch


def test_key_reference_missing_key_and_child_isolation(monkeypatch):
    from scripts.dsh_live_probe import _probe_child_environment

    config = ModelProviderConfig("https://example.test", "anthropic-messages", "test", "CUSTOM_KEY")
    with pytest.raises(ValueError, match="missing"):
        config.resolve_key({})
    assert config.resolve_key({"CUSTOM_KEY": "synthetic-secret"}) == "synthetic-secret"
    monkeypatch.setenv("PROJECT_WATCH_MODEL_KEY_ENV", "CUSTOM_KEY")
    for name in ("CUSTOM_KEY", "PROJECT_WATCH_MODEL_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.setenv(name, "synthetic-secret")
    assert "synthetic-secret" not in _probe_child_environment().values()


def test_subscription_mode_is_explicit_and_does_not_invent_prices(monkeypatch):
    from scripts.dsh_tool_turn_probe import _configured_policy

    config = ModelProviderConfig("https://example.test", "anthropic-messages", "test")
    monkeypatch.setenv("PROJECT_WATCH_MODEL_BUDGET_MODE", "request_quota")
    policy = _configured_policy(config)
    assert policy.max_cost_cny is None
    assert policy.max_requests == 4
    assert policy.max_output_tokens_per_request == 300
    monkeypatch.setenv("PROJECT_WATCH_MODEL_MAX_OUTPUT_TOKENS", "768")
    assert _configured_policy(config).max_output_tokens_per_request == 768
    monkeypatch.setenv("PROJECT_WATCH_MODEL_MAX_OUTPUT_TOKENS", "1025")
    with pytest.raises(ValueError, match="1024"):
        _configured_policy(config)
    monkeypatch.setenv("PROJECT_WATCH_MODEL_BUDGET_MODE", "typo")
    with pytest.raises(ValueError):
        _configured_policy(config)


def test_settings_require_explicit_endpoint_protocol_and_model():
    with pytest.raises(ValueError):
        ModelProviderConfig.from_environment({})
    config = ModelProviderConfig.from_environment(
        {
            "PROJECT_WATCH_MODEL_BASE_URL": "https://example.test/v1",
            "PROJECT_WATCH_MODEL_PROTOCOL": "openai-completions",
            "PROJECT_WATCH_MODEL_ID": "test",
        }
    )
    assert config.credential_env == "PROJECT_WATCH_MODEL_API_KEY"


def test_thinking_disable_is_explicit_and_protocol_scoped():
    assert (
        ModelProviderConfig("https://example.test", "anthropic-messages", "test").thinking_mode
        == "provider_default"
    )
    with pytest.raises(ValueError, match="thinking"):
        ModelProviderConfig(
            "https://example.test", "openai-completions", "test", thinking_mode="disabled"
        )
    with pytest.raises(ValueError, match="thinking"):
        ModelProviderConfig(
            "https://example.test", "anthropic-messages", "test", thinking_mode="typo"
        )
