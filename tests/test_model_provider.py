"""Model configuration is explicit, secret-free and protocol-specific."""


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


def test_key_reference_and_missing_key():
    config = ModelProviderConfig("https://example.test", "anthropic-messages", "test", "CUSTOM_KEY")
    with pytest.raises(ValueError, match="missing"):
        config.resolve_key({})
    assert config.resolve_key({"CUSTOM_KEY": "synthetic-secret"}) == "synthetic-secret"


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
