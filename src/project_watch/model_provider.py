"""Operator-owned model configuration, without credentials or browser persistence.

This is a configuration contract for local acceptance and future settings UI,
not an unauthenticated endpoint that lets web callers choose outbound URLs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelProviderConfig:
    base_url: str
    protocol: str
    model: str
    credential_env: str = "PROJECT_WATCH_MODEL_API_KEY"
    thinking_mode: str = "provider_default"

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(c.isspace() or ord(c) < 32 for c in self.base_url)
        ):
            raise ValueError("model base URL must be HTTPS without credentials, query or fragment")
        if self.protocol not in {"openai-completions", "anthropic-messages"}:
            raise ValueError("unsupported model API protocol")
        if self.thinking_mode not in {"provider_default", "disabled"} or (
            self.thinking_mode == "disabled" and self.protocol != "anthropic-messages"
        ):
            raise ValueError("explicit thinking disable is only supported for Anthropic Messages")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", self.credential_env):
            raise ValueError("credential must be an environment variable reference")
        if (
            not self.model
            or len(self.model) > 160
            or any(c.isspace() or ord(c) < 32 for c in self.model)
        ):
            raise ValueError("a bounded model ID is required")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> ModelProviderConfig:
        return cls(
            base_url=environment.get("PROJECT_WATCH_MODEL_BASE_URL", ""),
            protocol=environment.get("PROJECT_WATCH_MODEL_PROTOCOL", ""),
            model=environment.get("PROJECT_WATCH_MODEL_ID", ""),
            credential_env=environment.get(
                "PROJECT_WATCH_MODEL_KEY_ENV", "PROJECT_WATCH_MODEL_API_KEY"
            ),
            thinking_mode=environment.get("PROJECT_WATCH_MODEL_THINKING", "provider_default"),
        )

    @property
    def upstream_url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.protocol == "anthropic-messages":
            return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        return base + "/chat/completions"

    def resolve_key(self, environment: Mapping[str, str]) -> str:
        key = environment.get(self.credential_env, "")
        if not key or any(c in key for c in "\r\n"):
            raise ValueError("configured model credential is missing or invalid")
        return key
