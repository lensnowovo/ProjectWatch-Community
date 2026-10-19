"""Synthetic-only ASGI wrapper that records MCP method names, never payloads."""

from __future__ import annotations

import json
import os
from pathlib import Path

from project_watch.mcp_server import create_team_mcp_app

_RECORDED_METHODS = {"initialize", "notifications/initialized", "tools/list", "tools/call"}


def _tool_outcome(response: bytes) -> dict[str, object]:
    """Record protocol outcome and fixed error categories, never source/error prose."""
    text = response.decode("utf-8", errors="replace")
    for raw in [text, *(s[5:].strip() for s in text.splitlines() if s.startswith("data:"))]:
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        result = item.get("result") if isinstance(item, dict) else None
        if not isinstance(result, dict):
            continue
        failed = result.get("isError") is True
        outcome: dict[str, object] = {"tool_error": failed}
        if failed:
            encoded = json.dumps(result)
            for phrase, code in (
                ("source version changed", "source_version"),
                ("source locator is not", "source_locator"),
                ("source belongs to", "source_identity"),
                ("metric number", "metric_number"),
                ("short metric name", "metric_name"),
                ("requires a unit", "metric_unit"),
                ("requires a YYYY", "metric_period"),
                ("basis", "metric_basis"),
                ("validation error", "schema_validation"),
            ):
                if phrase in encoded:
                    outcome["error_code"] = code
                    break
        return outcome
    return {}


def _tool_names(response: bytes) -> list[str]:
    """Read only tool names from bounded JSON/SSE, never descriptions or data."""
    text = response.decode("utf-8", errors="replace")
    candidates = [
        text,
        *(line[5:].strip() for line in text.splitlines() if line.startswith("data:")),
    ]
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        tools = result.get("tools") if isinstance(result, dict) else None
        if isinstance(tools, list):
            return sorted(
                item["name"]
                for item in tools[:20]
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and len(item["name"]) <= 80
            )
    return []


class ProbeRecorder:
    def __init__(self, inner, log_path: Path) -> None:
        self.inner = inner
        self.log_path = log_path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            await self.inner(scope, receive, send)
            return
        body = bytearray()
        oversized = False
        response = bytearray()
        response_oversized = False
        response_status = None

        async def watched_receive():
            nonlocal oversized
            message = await receive()
            chunk = message.get("body", b"")
            if len(body) + len(chunk) <= 16_384:
                body.extend(chunk)
            else:
                oversized = True
            return message

        async def watched_send(message):
            nonlocal response_oversized, response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if len(response) + len(chunk) <= 65_536:
                    response.extend(chunk)
                else:
                    response_oversized = True
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                method = "other"
                if not oversized:
                    try:
                        candidate = json.loads(body).get("method")
                        if candidate in _RECORDED_METHODS:
                            method = candidate
                    except (ValueError, AttributeError, TypeError):
                        pass
                record = {"method": method, "status": response_status}
                if method == "tools/list" and response_status == 200 and not response_oversized:
                    record["tools"] = _tool_names(response)
                if method == "tools/call" and response_status == 200 and not oversized:
                    try:
                        params = json.loads(body).get("params", {})
                        name = params.get("name") if isinstance(params, dict) else None
                        if name in {
                            "project_overview",
                            "source_excerpts",
                            "monitoring_status",
                            "propose_candidate",
                        }:
                            record["tool"] = name
                            if not response_oversized:
                                record.update(_tool_outcome(response))
                    except (ValueError, AttributeError, TypeError):
                        pass
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            await send(message)

        await self.inner(scope, watched_receive, watched_send)


def create_probe_app():
    if os.environ.get("PROJECT_WATCH_DSH_PROBE_SYNTHETIC") != "1":
        raise RuntimeError("DSH probe server is restricted to synthetic tests")
    log_path = os.environ.get("PROJECT_WATCH_DSH_PROBE_LOG")
    if not log_path or not Path(log_path).is_absolute():
        raise RuntimeError("DSH probe requires an absolute request-method log path")
    return ProbeRecorder(create_team_mcp_app(), Path(log_path))
