"""Optional official DSH tool-turn probe with a loopback scripted model.

All source data and model responses are synthetic. This is not an LLM benchmark.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from project_watch.domain import FragmentInput
from project_watch.dsh_profile import TOKEN_ENV, render_dsh_patch
from project_watch.model_provider import ModelProviderConfig
from project_watch.repository import ProjectRepository
from project_watch.team_auth import TeamAuthStore
from scripts.dsh_budget_gateway import BudgetGate, BudgetPolicy, BudgetProxy
from scripts.dsh_live_probe import (
    DSH_PACKAGE,
    _certificate,
    _loopback_port,
    _methods,
    _probe_child_environment,
    _stop,
    _wait_for_server,
)

TURN_TIMEOUT_SECONDS = 90
_SOURCE_TEXT = "Synthetic 2026 revenue was 42 million yuan; confirm against the original."


class ScriptedProvider:
    """Fixed tool-call wire responses; does not inspect or persist prompt contents."""

    def __init__(self, source_id: str, source_sha256: str) -> None:
        self.source_id = source_id
        self.source_sha256 = source_sha256
        self.stages: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def response(self, request: dict[str, object]) -> tuple[dict[str, object], bool]:
        tools = request.get("tools")
        names = (
            sorted(
                item.get("function", {}).get("name", "")
                for item in tools
                if isinstance(item, dict) and isinstance(item.get("function"), dict)
            )
            if isinstance(tools, list)
            else []
        )
        with self._lock:
            stage = len(self.stages)
            if stage >= 3:
                raise ValueError("scripted provider received more than three model requests")
            self.stages.append({"stage": stage, "tools": names, "stream": request.get("stream")})
        if stage == 0:
            name = "mcp__project_watch__source_excerpts"
            arguments = {"source_id": self.source_id, "limit": 1}
        elif stage == 1:
            name = "mcp__project_watch__propose_candidate"
            arguments = {
                "source_id": self.source_id,
                "expected_source_sha256": self.source_sha256,
                "source_locator": "line 1",
                "subject": "收入",
                "statement": "合成资料记载 2026 年收入为 42 百万元，待人工核验。",
                "number": "42",
                "unit": "百万元",
                "period": "2026",
                "scope": "公司",
            }
        else:
            name = ""
            arguments = {}
        if name and name not in names:
            raise ValueError("DSH did not offer the expected bounded MCP tool")
        if name:
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"synthetic-call-{stage}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "候选已提交人工审核，未确认。"}
            finish = "stop"
        payload = {
            "id": f"synthetic-response-{stage}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        end = {
            **payload,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        }
        return {"chunks": [payload, end]}, bool(request.get("stream"))


class _ScriptedOpener:
    """An in-memory upstream for the same budget proxy used by the live probe."""

    def __init__(self, provider: ScriptedProvider) -> None:
        self.provider = provider

    def open(self, request: urllib.request.Request, *, timeout: int) -> BytesIO:
        if timeout <= 0 or request.data is None:
            raise ValueError("invalid scripted model request")
        result, stream = self.provider.response(json.loads(request.data))
        if not stream:
            raise ValueError("scripted model must stream")
        chunks = result["chunks"]
        body = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
        body += 'data: {"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
        body += "data: [DONE]\n\n"
        return BytesIO(body.encode("utf-8"))


def _sdk_turn(
    process: subprocess.Popen,
    cwd: Path,
    *,
    model: str = "deepseek-v4-flash",
    live: bool = False,
    provider: str = "deepseek-official",
    max_output_tokens: int = 300,
) -> dict[str, object]:
    lines: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read_stdout, daemon=True).start()

    def send(request_id: int, method: str, params: dict[str, object] | None = None) -> None:
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    send(
        1,
        "initialize",
        {
            "cwd": str(cwd),
            "provider": provider,
            "model": model,
            "maxTokens": max_output_tokens if live else 200,
        },
    )
    deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
    initialized = False
    prompt_accepted = False
    idle = False
    events: list[str] = []
    diagnostic_codes: set[str] = set()
    while time.monotonic() < deadline and not idle:
        try:
            line = lines.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if line is None:
            break
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 1:
            initialized = message.get("result", {}).get("serverInfo", {}).get("name") == (
                "deepseek-harness-sdk-runtime"
            )
            if not initialized:
                break
            send(
                2,
                "session/prompt",
                {
                    "sessionId": "synthetic-tool-turn",
                    "contentBlocks": [
                        {
                            "type": "text",
                            "text": (
                                "This is a disposable synthetic project. Call project_overview "
                                "to find the source, read its located text using source_excerpts, "
                                "then submit exactly one evidence-linked revenue candidate with "
                                "propose_candidate. Preserve the source hash and locator. Never "
                                "confirm a candidate or invent a number."
                                if live
                                else "Read the synthetic source using source_excerpts, then submit "
                                "one evidence-linked candidate for human review. Never confirm it."
                            ),
                        }
                    ],
                },
            )
        elif message.get("id") == 2:
            prompt_accepted = isinstance(message.get("result", {}).get("messageId"), str)
        elif message.get("method") == "session.event":
            params = message.get("params", {})
            if params.get("sessionId") == "synthetic-tool-turn":
                event = params.get("event", {})
                if isinstance(event, dict) and isinstance(event.get("type"), str):
                    events.append(event["type"])
                    # Never print upstream error prose (it may echo prompts or credentials).
                    encoded = json.dumps(event)
                    for code in (
                        "UNKNOWN_PROVIDER",
                        "UNKNOWN_MODEL",
                        "MISSING_CREDENTIAL",
                        "INVALID_CONFIG",
                        "UNSUPPORTED_API",
                        "ECONNREFUSED",
                        "ECONNRESET",
                        "ENOTFOUND",
                        "API_ERROR",
                        "INVALID_REQUEST",
                        "UNSUPPORTED_REASONING_EFFORT",
                        "UNSUPPORTED_OPTION",
                        "INVALID_CREDENTIAL",
                        "UNKNOWN_ADAPTER",
                    ):
                        if code in encoded:
                            diagnostic_codes.add(code)
        elif message.get("method") == "session.status":
            params = message.get("params", {})
            if params.get("sessionId") == "synthetic-tool-turn" and params.get("status") == "idle":
                idle = True
    with suppress(BrokenPipeError):
        send(3, "shutdown")
    return {
        "initialized": initialized,
        "prompt_accepted": prompt_accepted,
        "idle": idle,
        "event_types": sorted(set(events)),
        "diagnostic_codes": sorted(diagnostic_codes),
    }


def _live_policy() -> tuple[BudgetPolicy, str]:
    key = os.environ.get("PROJECT_WATCH_DEEPSEEK_API_KEY", "")
    model = os.environ.get("PROJECT_WATCH_DSH_MODEL", "")
    if not key or not model:
        raise ValueError("live synthetic probe requires a model and provider key")
    try:
        policy = BudgetPolicy(
            model=model,
            max_requests=4,
            max_request_bytes=150_000,
            max_output_tokens_per_request=300,
            max_cost_cny=Decimal(os.environ["PROJECT_WATCH_DSH_MAX_CNY"]),
            input_cny_per_million=Decimal(os.environ["PROJECT_WATCH_DSH_INPUT_CNY_PER_MILLION"]),
            output_cny_per_million=Decimal(os.environ["PROJECT_WATCH_DSH_OUTPUT_CNY_PER_MILLION"]),
        )
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("live synthetic probe requires a CNY cap and both model rates") from exc
    return policy, key


def _configured_policy(config: ModelProviderConfig) -> BudgetPolicy:
    mode = os.environ.get("PROJECT_WATCH_MODEL_BUDGET_MODE", "estimated_cny")
    if mode not in {"request_quota", "estimated_cny"}:
        raise ValueError("unsupported model budget mode")
    try:
        prices = (
            [None, None, None]
            if mode == "request_quota"
            else [
                Decimal(os.environ[name])
                for name in (
                    "PROJECT_WATCH_DSH_MAX_CNY",
                    "PROJECT_WATCH_DSH_INPUT_CNY_PER_MILLION",
                    "PROJECT_WATCH_DSH_OUTPUT_CNY_PER_MILLION",
                )
            ]
        )
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("metered model requires a CNY cap and both rates") from exc
    try:
        output_cap = int(os.environ.get("PROJECT_WATCH_MODEL_MAX_OUTPUT_TOKENS", "300"))
    except ValueError as exc:
        raise ValueError("invalid synthetic output cap") from exc
    if not 1 <= output_cap <= 1024:
        raise ValueError("synthetic output cap must be between 1 and 1024")
    return BudgetPolicy(config.model, 4, 150_000, output_cap, *prices)


def run_probe(
    *, live_synthetic_model: bool = False, configured_model: bool = False
) -> dict[str, object]:
    config = ModelProviderConfig.from_environment(os.environ) if configured_model else None
    live_synthetic_model = live_synthetic_model or configured_model
    npx = shutil.which("npx")
    if not npx:
        raise RuntimeError("npx is required for the pinned official DSH probe")
    with tempfile.TemporaryDirectory(prefix="project-watch-dsh-tool-turn-") as directory:
        root = Path(directory)
        db_path = root / "synthetic-watch.sqlite3"
        repository = ProjectRepository(db_path)
        project = repository.create_project("Synthetic DSH Tool Turn")
        source = repository.add_source(
            project.id,
            kind="document",
            reference="synthetic-only.txt",
            content_sha256=hashlib.sha256(_SOURCE_TEXT.encode("utf-8")).hexdigest(),
        )
        repository.enqueue_intake(project.id, source.id)
        job = repository.claim_next_intake()
        if job is None:
            raise RuntimeError("synthetic intake could not be claimed")
        repository.complete_intake(
            job.id, [FragmentInput(ordinal=1, locator="line 1", text=_SOURCE_TEXT)]
        )
        auth = TeamAuthStore(db_path)
        admin = auth.bootstrap_admin("synthetic@example.test", "synthetic-local-password")
        token, _grant = auth.issue_executor_token(
            project.id, admin.id, label="DSH synthetic tool turn", permission="propose"
        )
        provider = None
        if live_synthetic_model:
            if config is None:
                policy, provider_key = _live_policy()
            else:
                policy, provider_key = _configured_policy(config), config.resolve_key(os.environ)
            gate = BudgetGate(policy)
            local_token = secrets.token_urlsafe(32)
            proxy = BudgetProxy(
                gate,
                upstream_url=config.upstream_url
                if config
                else "https://api.deepseek.com/chat/completions",
                provider_key=provider_key,
                local_token=local_token,
                protocol=config.protocol if config else "openai-completions",
                thinking_mode=config.thinking_mode if config else "provider_default",
            )
        else:
            provider = ScriptedProvider(source.id, source.content_sha256)
            gate = BudgetGate(
                BudgetPolicy(
                    model="deepseek-v4-flash",
                    max_requests=3,
                    max_request_bytes=150_000,
                    max_output_tokens_per_request=200,
                    max_cost_cny=Decimal("1"),
                    input_cny_per_million=Decimal("1"),
                    output_cny_per_million=Decimal("10"),
                )
            )
            local_token = secrets.token_urlsafe(32)
            proxy = BudgetProxy(
                gate,
                upstream_url="https://synthetic.invalid/chat/completions",
                provider_key="synthetic-not-a-secret",
                local_token=local_token,
                opener=_ScriptedOpener(provider),
            )
        model_server = proxy.server()
        model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
        model_thread.start()
        cert, key = _certificate(root)
        port = _loopback_port()
        url = f"https://localhost:{port}/mcp"
        patch = root / "watch.patch.yml"
        patch.write_text(
            render_dsh_patch(url)
            + (
                config.dsh_proxy_patch(
                    model_server.server_port, gate.policy.max_output_tokens_per_request
                )
                if config
                else ""
            ),
            encoding="utf-8",
        )
        log_path = root / "mcp-methods.jsonl"
        environment = _probe_child_environment()
        environment.update(
            {
                "PROJECT_WATCH_DSH_PROBE_SYNTHETIC": "1",
                "PROJECT_WATCH_DSH_PROBE_LOG": str(log_path),
                "PROJECT_WATCH_DB_PATH": str(db_path),
                "PROJECT_WATCH_PROJECT_ID": project.id,
                "PROJECT_WATCH_MCP_RESOURCE_URL": url,
                "PROJECT_WATCH_MCP_ISSUER_URL": f"https://localhost:{port}/",
            }
        )
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.dsh_probe_server:create_probe_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--ssl-keyfile",
                str(key),
                "--ssl-certfile",
                str(cert),
                "--no-access-log",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        dsh = None
        try:
            _wait_for_server(url, cert, server)
            log_path.write_text("", encoding="utf-8")
            dsh_environment = _probe_child_environment()
            dsh_environment.update(
                {
                    "DSH_HOME": str(root / "dsh-home"),
                    "DSH_TELEMETRY_MODE": "DISABLED",
                    "DSH_TOOLS_MODE": "native",
                    "NODE_EXTRA_CA_CERTS": str(cert),
                    "NO_PROXY": "localhost,127.0.0.1",
                    TOKEN_ENV: token,
                    "DEEPSEEK_API_KEY": local_token,
                    "DEEPSEEK_BASE_URL": f"http://127.0.0.1:{model_server.server_port}/v1",
                }
            )
            if config is not None:
                dsh_environment.pop("DEEPSEEK_API_KEY", None)
                dsh_environment.pop("DEEPSEEK_BASE_URL", None)
                dsh_environment["PROJECT_WATCH_MODEL_PROXY_TOKEN"] = local_token
            dsh = subprocess.Popen(
                [npx, "-y", DSH_PACKAGE, "--profile", "sdk-minimal", "--patch", str(patch)],
                cwd=root,
                env=dsh_environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
            turn = _sdk_turn(
                dsh,
                root,
                model=gate.policy.model,
                live=live_synthetic_model,
                provider="project-watch-model" if config else "deepseek-official",
                max_output_tokens=gate.policy.max_output_tokens_per_request,
            )
            methods = _methods(log_path)
            observations = repository.list_observations(project.id)
            audits = [
                event
                for event in repository.list_audit_events(project.id)
                if event.action == "candidate.created"
            ]
        finally:
            if dsh is not None:
                _stop(dsh)
                dsh.stdin.close()
                dsh.stdout.close()
            _stop(server)
            model_server.shutdown()
            model_server.server_close()
            model_thread.join(timeout=2)
        called_tools = [
            item.get("tool")
            for item in methods
            if item.get("method") == "tools/call"
            and item.get("status") == 200
            and item.get("tool_error") is not True
        ]
        read_before_propose = (
            "source_excerpts" in called_tools
            and "propose_candidate" in called_tools
            and called_tools.index("source_excerpts") < called_tools.index("propose_candidate")
        )
        checks = {
            "sdk_initialized": turn["initialized"],
            "prompt_accepted": turn["prompt_accepted"],
            "turn_idle": turn["idle"],
            "provider_request_bound": (
                1 <= gate.receipt()["requests_admitted"] <= 4
                and gate.receipt()["requests_with_usage"] > 0
                and gate.receipt()["last_provider_error"] is None
                if live_synthetic_model
                else provider is not None
                and len(provider.stages) == 3
                and gate.receipt()["requests_admitted"] == 3
            ),
            "mcp_read_then_propose": (
                read_before_propose
                if live_synthetic_model
                else called_tools == ["source_excerpts", "propose_candidate"]
            ),
            "candidate_unconfirmed": len(observations) == 1
            and observations[0].status == "candidate",
            "candidate_number_from_excerpt": len(observations) == 1
            and observations[0].number == Decimal("42")
            and observations[0].period == "2026",
            "provenance_preserved": len(audits) == 1
            and audits[0].source_id == source.id
            and audits[0].source_sha256 == source.content_sha256
            and audits[0].source_locator == "line 1",
        }
        return {
            "corpus": "synthetic_only",
            "provider": "configured_live_model"
            if config
            else (
                "deepseek_live_model" if live_synthetic_model else "scripted_loopback_not_a_model"
            ),
            "protocol": config.protocol if config else "openai-completions",
            "thinking_mode": config.thinking_mode if config else "provider_default",
            "dsh_package": DSH_PACKAGE,
            "stages": provider.stages if provider is not None else [],
            "mcp_calls": [item for item in methods if item.get("method") == "tools/call"],
            "turn": turn,
            "budget_receipt": gate.receipt(),
            "checks": checks,
            "unexpected_failures": sum(not value for value in checks.values()),
        }


def main() -> int:
    live = "--live-synthetic-model" in sys.argv[1:]
    configured = "--live-configured-model" in sys.argv[1:]
    if sys.argv[1:] not in ([], ["--live-synthetic-model"], ["--live-configured-model"]):
        raise SystemExit(
            "usage: dsh_tool_turn_probe.py [--live-synthetic-model | --live-configured-model]"
        )
    report = run_probe(live_synthetic_model=live, configured_model=configured)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["unexpected_failures"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
