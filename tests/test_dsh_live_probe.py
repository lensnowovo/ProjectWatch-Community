"""The optional DSH probe must log protocol shape without source or token data."""

import asyncio
import io
import json
from pathlib import Path

import pytest

from scripts.dsh_live_probe import _rpc_initialize
from scripts.dsh_probe_server import ProbeRecorder, _tool_names, _tool_outcome, create_probe_app


def test_probe_recorder_only_writes_allowlisted_method_and_status(tmp_path: Path) -> None:
    received = []

    async def inner(scope, receive, send):
        received.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    request = json.dumps(
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"secret": "DO-NOT-LOG"}}
    ).encode()
    message = {"type": "http.request", "body": request, "more_body": False}
    sent = []
    recorder = ProbeRecorder(inner, tmp_path / "methods.jsonl")
    asyncio.run(
        recorder(
            {"type": "http", "path": "/mcp", "headers": [(b"authorization", b"Bearer SECRET")]},
            lambda: asyncio.sleep(0, result=message),
            lambda item: asyncio.sleep(0, result=sent.append(item)),
        )
    )
    assert received == [message]
    assert sent[0]["status"] == 200
    assert (tmp_path / "methods.jsonl").read_text(encoding="utf-8") == (
        '{"method":"tools/call","status":200}\n'
    )


def test_probe_records_only_allowlisted_tool_name(tmp_path: Path) -> None:
    async def inner(_scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def record(name: str, path: Path) -> str:
        request = json.dumps(
            {
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": {"secret": "DO-NOT-LOG"},
                },
            }
        ).encode()
        recorder = ProbeRecorder(inner, path)
        await recorder(
            {"type": "http", "path": "/mcp"},
            lambda: asyncio.sleep(
                0, result={"type": "http.request", "body": request, "more_body": False}
            ),
            lambda _message: asyncio.sleep(0),
        )
        return path.read_text(encoding="utf-8")

    assert asyncio.run(record("source_excerpts", tmp_path / "allowed.jsonl")) == (
        '{"method":"tools/call","status":200,"tool":"source_excerpts"}\n'
    )
    assert asyncio.run(record("evil-tool", tmp_path / "unknown.jsonl")) == (
        '{"method":"tools/call","status":200}\n'
    )


def test_probe_factory_refuses_non_synthetic_use(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PROJECT_WATCH_DSH_PROBE_SYNTHETIC", raising=False)
    monkeypatch.setenv("PROJECT_WATCH_DSH_PROBE_LOG", str(tmp_path / "methods.jsonl"))
    with pytest.raises(RuntimeError, match="synthetic"):
        create_probe_app()


def test_tool_discovery_records_names_only() -> None:
    payload = json.dumps(
        {
            "result": {
                "tools": [
                    {"name": "source_excerpts", "description": "CONFIDENTIAL-DESCRIPTION"},
                    {"name": "project_overview"},
                ]
            }
        }
    )
    assert _tool_names(payload.encode()) == ["project_overview", "source_excerpts"]
    assert _tool_names(f"event: message\ndata: {payload}\n\n".encode()) == [
        "project_overview",
        "source_excerpts",
    ]


def test_tool_outcome_reports_error_not_http_success_without_logging_prose():
    response = json.dumps(
        {
            "result": {
                "isError": True,
                "content": [{"text": "source version changed PRIVATE-MARKER"}],
            }
        }
    ).encode()
    assert _tool_outcome(response) == {"tool_error": True, "error_code": "source_version"}
    assert _tool_outcome(b'data: {"result":{"isError":false}}\n\n') == {"tool_error": False}
    assert _tool_outcome(b"invalid") == {}


def test_sdk_initialize_requires_runtime_identity_and_shuts_down(tmp_path: Path) -> None:
    class FakeProcess:
        def __init__(self, response):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(json.dumps(response) + "\n")
            self.waited = False

        def wait(self, *, timeout):
            self.waited = True
            return 0

    valid = FakeProcess(
        {"id": 1, "result": {"serverInfo": {"name": "deepseek-harness-sdk-runtime"}}}
    )
    assert _rpc_initialize(valid, tmp_path) is True
    assert '"method":"initialize"' in valid.stdin.getvalue()
    assert '"method":"shutdown"' in valid.stdin.getvalue()
    assert valid.waited is True

    failed = FakeProcess({"id": 1, "error": {"message": "synthetic MCP denied"}})
    assert _rpc_initialize(failed, tmp_path) is False
    assert '"method":"shutdown"' not in failed.stdin.getvalue()


def test_sdk_initialize_has_a_bounded_deadline(monkeypatch, tmp_path: Path) -> None:
    class SilentProcess:
        stdin = io.StringIO()
        stdout = io.StringIO("")

    monkeypatch.setattr("scripts.dsh_live_probe.RPC_TIMEOUT_SECONDS", 0)
    with pytest.raises(TimeoutError, match="deadline"):
        _rpc_initialize(SilentProcess(), tmp_path)
