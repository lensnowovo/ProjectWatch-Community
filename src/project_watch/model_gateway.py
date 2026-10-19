"""Loopback-only, bounded model egress for explicitly authorized DSH turns.

The caller supplies current prices and a cap. This is a conservative preflight
estimate, not a guarantee about provider billing. No prompt or secret is logged.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from project_watch.model_provider import ModelProviderConfig


class GateRejected(ValueError):
    """A model request failed the local egress policy."""


@dataclass(frozen=True)
class BudgetPolicy:
    model: str
    max_requests: int
    max_request_bytes: int
    max_output_tokens_per_request: int
    max_cost_cny: Decimal | None
    input_cny_per_million: Decimal | None
    output_cny_per_million: Decimal | None

    def __post_init__(self) -> None:
        prices = (self.max_cost_cny, self.input_cny_per_million, self.output_cny_per_million)
        if any(value is None for value in prices) and not all(value is None for value in prices):
            raise ValueError("all monetary limits must be supplied together or all omitted")
        if (
            not self.model
            or not all(value.is_finite() for value in prices if value is not None)
            or any(
                value <= 0
                for value in (
                    self.max_requests,
                    self.max_request_bytes,
                    self.max_output_tokens_per_request,
                    *(value for value in prices if value is not None),
                )
            )
        ):
            raise ValueError("model and all budget limits must be positive")


class BudgetGate:
    """Reserve a pessimistic per-request amount before sending anything upstream."""

    def __init__(self, policy: BudgetPolicy) -> None:
        self.policy = policy
        self._lock = threading.Lock()
        self._requests = 0
        self._reserved_cny = Decimal(0)
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._responses_with_usage = 0
        self._last_provider_error: str | None = None
        self._stop_reasons: list[str] = []
        self._response_block_types: set[str] = set()

    def admit(self, body: bytes) -> None:
        if not 0 < len(body) <= self.policy.max_request_bytes:
            raise GateRejected("model request size exceeds the configured limit")
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GateRejected("model request is not valid JSON") from exc
        if not isinstance(request, dict):
            raise GateRejected("model request must be an object")
        if request.get("model") != self.policy.model:
            raise GateRejected("model differs from the configured model")
        if request.get("stream") is not True:
            raise GateRejected("model request must stream usage")
        output_tokens = request.get("max_tokens")
        if (
            type(output_tokens) is not int
            or not 0 < output_tokens <= self.policy.max_output_tokens_per_request
        ):
            raise GateRejected("model output tokens exceed or omit the configured limit")
        if not isinstance(request.get("messages"), list):
            raise GateRejected("model messages are missing")
        # Byte length overestimates ordinary UTF-8 token count. The extra 2x
        # allowance covers chat serialization overhead; the receipt still labels
        # this a preflight estimate, not a billing guarantee.
        estimated_input_tokens = len(body) * 2
        estimate = (
            Decimal(0)
            if self.policy.max_cost_cny is None
            else (
                Decimal(estimated_input_tokens) * self.policy.input_cny_per_million
                + Decimal(output_tokens) * self.policy.output_cny_per_million
            )
            / Decimal(1_000_000)
        )
        with self._lock:
            if self._requests >= self.policy.max_requests:
                raise GateRejected("model request count reached the configured limit")
            if (
                self.policy.max_cost_cny is not None
                and self._reserved_cny + estimate > self.policy.max_cost_cny
            ):
                raise GateRejected("model cost ceiling would be exceeded")
            self._requests += 1
            self._reserved_cny += estimate

    def record_response(self, body: bytes) -> None:
        usage = None
        anthropic_usage = {}
        for line in body.splitlines():
            if not line.startswith(b"data: "):
                continue
            try:
                item = json.loads(line[6:])
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict) and item.get("type") == "content_block_start":
                block = item.get("content_block")
                kind = block.get("type") if isinstance(block, dict) else None
                if kind in {"thinking", "text", "tool_use", "redacted_thinking"}:
                    with self._lock:
                        self._response_block_types.add(kind)
            if isinstance(item, dict) and isinstance(item.get("usage"), dict):
                if "prompt_tokens" in item["usage"]:
                    usage = item["usage"]
                elif item.get("type") == "message_delta":
                    anthropic_usage.update(item["usage"])
                    delta = item.get("delta")
                    reason = delta.get("stop_reason") if isinstance(delta, dict) else None
                    if reason in {"end_turn", "tool_use", "max_tokens", "stop_sequence"}:
                        with self._lock:
                            self._stop_reasons.append(reason)
            if isinstance(item, dict) and item.get("type") == "message_start":
                message = item.get("message")
                start_usage = message.get("usage") if isinstance(message, dict) else None
                if isinstance(start_usage, dict):
                    anthropic_usage.update(start_usage)
        if usage is None and "input_tokens" in anthropic_usage:
            counts = [
                anthropic_usage.get(field, 0)
                for field in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            ]
            if all(type(value) is int and value >= 0 for value in counts):
                usage = {
                    "prompt_tokens": sum(counts),
                    "completion_tokens": anthropic_usage.get("output_tokens"),
                }
        if usage is None:
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if any(type(value) is not int or value < 0 for value in (prompt, completion)):
            return
        with self._lock:
            self._prompt_tokens += prompt
            self._completion_tokens += completion
            self._responses_with_usage += 1

    def record_provider_error(self, status: str) -> None:
        with self._lock:
            self._last_provider_error = status

    def receipt(self) -> dict[str, object]:
        with self._lock:
            return {
                "model": self.policy.model,
                "max_requests": self.policy.max_requests,
                "max_output_tokens_per_request": self.policy.max_output_tokens_per_request,
                "requests_admitted": self._requests,
                "requests_with_usage": self._responses_with_usage,
                "budget_mode": "request_quota"
                if self.policy.max_cost_cny is None
                else "estimated_cny",
                "estimated_reserved_cny": None
                if self.policy.max_cost_cny is None
                else str(self._reserved_cny),
                "configured_ceiling_cny": None
                if self.policy.max_cost_cny is None
                else str(self.policy.max_cost_cny),
                "observed_prompt_tokens": self._prompt_tokens,
                "observed_completion_tokens": self._completion_tokens,
                "observed_usage_estimate_cny": None
                if self.policy.max_cost_cny is None
                else str(
                    (
                        Decimal(self._prompt_tokens) * self.policy.input_cny_per_million
                        + Decimal(self._completion_tokens) * self.policy.output_cny_per_million
                    )
                    / Decimal(1_000_000)
                ),
                "last_provider_error": self._last_provider_error,
                "stop_reasons": list(self._stop_reasons),
                "response_block_types": sorted(self._response_block_types),
                "billing_note": (
                    "Request/token caps only; subscription usage and charges must be checked with provider."
                    if self.policy.max_cost_cny is None
                    else "Conservative local estimate; check provider invoice for actual charge."
                ),
            }


class BudgetProxy:
    """Forward accepted requests without passing the provider key to DSH."""

    def __init__(
        self,
        gate: BudgetGate,
        *,
        upstream_url: str,
        provider_key: str,
        local_token: str,
        opener: urllib.request.OpenerDirector | None = None,
        max_response_bytes: int = 2_000_000,
        protocol: str = "openai-completions",
        thinking_mode: str = "provider_default",
        before_send=None,
    ) -> None:
        ModelProviderConfig(
            base_url=upstream_url,
            protocol=protocol,
            model=gate.policy.model,
            thinking_mode=thinking_mode,
        )
        if not provider_key or not local_token or max_response_bytes <= 0:
            raise ValueError("model proxy requires credentials and a response limit")
        self.gate = gate
        self.upstream_url = upstream_url
        self.provider_key = provider_key
        self.local_token = local_token
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.max_response_bytes = max_response_bytes
        self.protocol = protocol
        self.thinking_mode = thinking_mode
        self.before_send = before_send

    def forward(self, body: bytes) -> bytes:
        # Validate the original request before applying the explicit operator option.
        if self.thinking_mode == "disabled":
            if len(body) > self.gate.policy.max_request_bytes:
                raise GateRejected("model request size exceeds the configured limit")
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GateRejected("model request is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise GateRejected("model request must be an object")
            payload["thinking"] = {"type": "disabled"}
            body = json.dumps(payload).encode("utf-8")
        self.gate.admit(body)
        request = urllib.request.Request(
            self.upstream_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.provider_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                **(
                    {"anthropic-version": "2023-06-01", "x-api-key": self.provider_key}
                    if self.protocol == "anthropic-messages"
                    else {}
                ),
            },
        )
        # Durable authorization/usage reservation, if supplied by a background
        # reading job. No HTTP request occurs if this fails. The commit marks the
        # dispatch boundary: a later pause cannot recall an already sent request.
        if self.before_send is not None:
            self.before_send(body)
        try:
            with self.opener.open(request, timeout=60) as response:
                result = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            self.gate.record_provider_error(f"http_{exc.code}")
            raise
        except (urllib.error.URLError, TimeoutError):
            self.gate.record_provider_error("unavailable")
            raise
        if len(result) > self.max_response_bytes:
            raise GateRejected("model response size exceeds the configured limit")
        self.gate.record_response(result)
        return result

    def server(self) -> ThreadingHTTPServer:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                expected_path = (
                    "/v1/messages"
                    if proxy.protocol == "anthropic-messages"
                    else "/v1/chat/completions"
                )
                allowed_paths = {expected_path}
                if proxy.protocol == "anthropic-messages":
                    allowed_paths.add(expected_path + "?beta=true")
                if self.path not in allowed_paths:
                    proxy.gate.record_provider_error("local_wrong_protocol_path")
                    self.send_error(404)
                    return
                authorized = self.headers.get("Authorization") == f"Bearer {proxy.local_token}"
                if proxy.protocol == "anthropic-messages":
                    authorized = authorized or self.headers.get("x-api-key") == proxy.local_token
                if not authorized:
                    proxy.gate.record_provider_error("local_auth_denied")
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= proxy.gate.policy.max_request_bytes:
                        raise GateRejected("model request size exceeds the configured limit")
                    result = proxy.forward(self.rfile.read(length))
                except GateRejected:
                    proxy.gate.record_provider_error("local_budget_gate_rejected")
                    self.send_error(429, "local model budget gate rejected request")
                    return
                except (urllib.error.URLError, TimeoutError):
                    self.send_error(502, "model provider unavailable")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None
