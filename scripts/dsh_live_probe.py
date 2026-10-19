"""Optional real DSH-to-MCP handshake probe using only disposable synthetic data.

This does not send a model prompt and is not a model-quality benchmark.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from project_watch.dsh_profile import TOKEN_ENV, render_dsh_patch
from project_watch.repository import ProjectRepository
from project_watch.team_auth import TeamAuthStore

DSH_PACKAGE = "@deepseek-ai/dsh@0.1.5-rc.3"
RPC_TIMEOUT_SECONDS = 90


def _probe_child_environment() -> dict[str, str]:
    """Do not inherit unrelated model credentials into probe subprocesses."""
    environment = os.environ.copy()
    for name in (
        "PROJECT_WATCH_DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "PROJECT_WATCH_MODEL_API_KEY",
        "PROJECT_WATCH_MODEL_PROXY_TOKEN",
        os.environ.get("PROJECT_WATCH_MODEL_KEY_ENV", "PROJECT_WATCH_MODEL_API_KEY"),
    ):
        environment.pop(name, None)
    return environment


def _loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _certificate(root: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("OpenSSL is required for the synthetic HTTPS probe")
    cert, key = root / "localhost.crt", root / "localhost.key"
    result = subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-config",
            os.devnull,
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        capture_output=True,
        env=_probe_child_environment(),
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("OpenSSL could not create the temporary localhost certificate")
    return cert, key


def _wait_for_server(url: str, cert: Path, process: subprocess.Popen) -> None:
    context = ssl.create_default_context(cafile=str(cert))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("synthetic MCP server exited before readiness")
        try:
            request = urllib.request.Request(
                url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
            )
            with opener.open(request, timeout=1):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise TimeoutError("synthetic HTTPS MCP server did not become ready")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _rpc_initialize(process: subprocess.Popen, cwd: Path) -> bool:
    lines: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read_stdout, daemon=True).start()
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "cwd": str(cwd),
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
        },
    }
    try:
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
    except BrokenPipeError:
        return False
    deadline = time.monotonic() + RPC_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if line is None:
            return False
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 1:
            result = message.get("result")
            initialized = (
                isinstance(result, dict)
                and result.get("serverInfo", {}).get("name") == "deepseek-harness-sdk-runtime"
            )
            if initialized:
                try:
                    process.stdin.write('{"jsonrpc":"2.0","id":2,"method":"shutdown"}\n')
                    process.stdin.flush()
                    process.wait(timeout=8)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    pass
            return initialized
    raise TimeoutError("DSH SDK initialization exceeded its synthetic probe deadline")


def _probe_dsh(npx: str, root: Path, patch: Path, cert: Path, token: str, label: str) -> bool:
    environment = _probe_child_environment()
    environment.update(
        {
            "DSH_HOME": str(root / f"dsh-home-{label}"),
            "DSH_TELEMETRY_MODE": "DISABLED",
            "NODE_EXTRA_CA_CERTS": str(cert),
            "NO_PROXY": "localhost,127.0.0.1",
            TOKEN_ENV: token,
            "DEEPSEEK_API_KEY": "synthetic-not-used",
        }
    )
    process = subprocess.Popen(
        [npx, "-y", DSH_PACKAGE, "--profile", "sdk-minimal", "--patch", str(patch)],
        cwd=root,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    try:
        return _rpc_initialize(process, root)
    finally:
        _stop(process)
        process.stdin.close()
        process.stdout.close()


def _methods(log_path: Path) -> list[dict[str, object]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def run_probe() -> dict[str, object]:
    npx = shutil.which("npx")
    if not npx:
        raise RuntimeError("npx is required to run the pinned official DSH CLI")
    with tempfile.TemporaryDirectory(prefix="project-watch-dsh-synthetic-") as directory:
        root = Path(directory)
        cert, key = _certificate(root)
        port = _loopback_port()
        url = f"https://localhost:{port}/mcp"
        db_path = root / "synthetic-watch.sqlite3"
        repository = ProjectRepository(db_path)
        project = repository.create_project("Synthetic DSH MCP Handshake")
        auth = TeamAuthStore(db_path)
        admin = auth.bootstrap_admin("synthetic@example.test", "synthetic-local-password")
        token, grant = auth.issue_executor_token(
            project.id, admin.id, label="DSH synthetic probe", permission="read"
        )
        patch = root / "watch.patch.yml"
        patch.write_text(render_dsh_patch(url), encoding="utf-8")
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
        try:
            _wait_for_server(url, cert, server)
            log_path.write_text("", encoding="utf-8")
            valid_rpc = _probe_dsh(npx, root, patch, cert, token, "valid")
            valid_methods = _methods(log_path)
            auth.revoke_executor_token(project.id, grant.id, actor_user_id=admin.id)
            log_path.write_text("", encoding="utf-8")
            revoked_rpc = _probe_dsh(npx, root, patch, cert, token, "revoked")
            revoked_methods = _methods(log_path)
        finally:
            _stop(server)
        checks = {
            "valid_dsh_sdk_initialized": valid_rpc,
            "mcp_initialized": any(
                item == {"method": "initialize", "status": 200} for item in valid_methods
            ),
            "tools_discovered": any(
                item.get("method") == "tools/list"
                and item.get("status") == 200
                and item.get("tools")
                == sorted(
                    [
                        "project_overview",
                        "source_excerpts",
                        "monitoring_status",
                        "propose_candidate",
                    ]
                )
                for item in valid_methods
            ),
            "revoked_grant_denied": any(item["status"] == 401 for item in revoked_methods),
            "revoked_no_tool_discovery": not any(
                item.get("method") == "tools/list" and item.get("status") == 200
                for item in revoked_methods
            ),
        }
        return {
            "corpus": "synthetic_only",
            "dsh_package": DSH_PACKAGE,
            "model_prompt_sent": False,
            "revoked_sdk_initialized": revoked_rpc,
            "checks": checks,
            "unexpected_failures": sum(not passed for passed in checks.values()),
        }


def main() -> int:
    report = run_probe()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["unexpected_failures"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
