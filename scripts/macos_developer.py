"""Explicit macOS developer setup. Never installs dependencies during startup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXTRAS = "dev,ocr,mcp,team,postgres"
MARKER = "projectwatch-ready.json"


class SetupError(RuntimeError):
    pass


def environment_path(repo: Path, name: str) -> Path:
    # Only a new, checkout-local venv; never repoint another agent's shared env.
    if not name.startswith(".venv-") or Path(name).name != name or "/" in name or "\\" in name:
        raise SetupError("环境名须为仓库内的 .venv-* 单层目录，例如 .venv-macos-r2")
    candidate = repo / name
    if candidate.is_symlink() or candidate.resolve().parent != repo.resolve():
        raise SetupError("不使用符号链接或仓库外的共享环境")
    return candidate


def data_path(repo: Path, value: str | None) -> Path:
    identity = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:12]
    home = Path(value).expanduser() if value else (
        Path.home() / "Library/Application Support/ProjectWatch-Developer" / identity
    )
    if not home.is_absolute():
        raise SetupError("数据目录请使用绝对路径")
    home, root = home.resolve(), repo.resolve()
    if home == root or home.is_relative_to(root) or root.is_relative_to(home):
        raise SetupError("数据目录与源码目录不可互相包含")
    return home


def fingerprint(repo: Path) -> str:
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "frontend/package-lock.json"):
        digest.update(name.encode())
        digest.update((repo / name).read_bytes())
    return digest.hexdigest()


def prerequisites() -> dict:
    if sys.platform != "darwin":
        raise SetupError("此入口用于 macOS；Windows 请使用 Windows 启动器")
    if sys.version_info[:2] not in {(3, 12), (3, 13)}:
        raise SetupError("本开发入口使用 Python 3.12 或 3.13，请显式选择对应解释器")
    if platform.machine() not in {"arm64", "x86_64"}:
        raise SetupError("当前仅面向 Apple Silicon arm64 或 Intel x86_64")
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise SetupError("缺少 Node.js/npm，请先安装 Node.js 22+，然后重新打开终端")
    result = subprocess.run(
        [node, "-p", "JSON.stringify({version:process.versions.node,arch:process.arch})"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    info = json.loads(result.stdout)
    if int(info["version"].split(".")[0]) < 22:
        raise SetupError("Node.js 版本过旧，需要 22 或更新版本")
    expected_arch = "arm64" if platform.machine() == "arm64" else "x64"
    if info["arch"] != expected_arch:
        raise SetupError("Python 与 Node 架构不同；请使用同一原生架构，勿混用 Rosetta 环境")
    if platform.machine() == "x86_64":
        translated = subprocess.run(
            ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
            capture_output=True, text=True, timeout=10,
        )
        if translated.stdout.strip() == "1":
            raise SetupError("当前 Python 运行于 Rosetta，请改用原生 arm64 Python 与 Node")
    return {"python": platform.python_version(), "architecture": platform.machine(),
            "node": info["version"], "npm": npm}


def run_checked(command: list[str], repo: Path) -> None:
    subprocess.run(command, cwd=repo, check=True)


@contextmanager
def install_lease(repo: Path):
    """Use the existing Git runtime lease without importing product dependencies."""
    if not (repo / ".git").exists():
        # ZIP checkouts have no shared source lease; documented manual shutdown.
        yield
        return
    import fcntl

    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, env=clean_env,
        check=True, capture_output=True, text=True, timeout=15,
    )
    location = Path(result.stdout.strip())
    if not location.is_absolute():
        location = repo / location
    with (location / "projectwatch-source.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SetupError("此源码仍有实例运行或正在维护，请先正常退出；未改动运行环境") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def install(repo: Path, environment: Path, info: dict) -> None:
    if environment.exists():
        raise SetupError("环境已存在，未覆盖。请用 --venv .venv-macos-r2 建立新环境；旧环境和数据保留")
    initial = fingerprint(repo)
    print("将下载 Python/前端依赖并构建完整应用；不会启动监控或调用模型/搜索。", flush=True)
    # Reserve first: venv must not accidentally reuse an existing environment.
    environment.mkdir()
    python = environment / "bin/python"
    run_checked([sys.executable, "-I", "-m", "venv", str(environment)], repo)
    run_checked([str(python), "-I", "-m", "pip", "install", "--upgrade", "pip>=26.2"], repo)
    run_checked([str(python), "-I", "-m", "pip", "install", "-e", f"{repo}[{EXTRAS}]",
                 "build", "ruff"], repo)
    run_checked([info["npm"], "--prefix", "frontend", "ci"], repo)
    run_checked([info["npm"], "--prefix", "frontend", "run", "build"], repo)
    run_checked([str(python), "-I", "-m", "pip", "check"], repo)
    run_checked([str(python), "-I", "-B", "-c",
                 "import project_watch.studio.app, rapidocr, onnxruntime, mcp, psycopg"], repo)
    if initial != fingerprint(repo):
        raise SetupError("安装期间依赖声明变化，请在新环境重新安装；当前环境未标记就绪")
    if not (repo / "src/project_watch/web/index.html").is_file():
        raise SetupError("未找到前端构建产物，当前环境未标记就绪")
    receipt = {"schema": 1, "repo": str(repo.resolve()), "dependencies": initial,
               "extras": EXTRAS, "python": info["python"], "architecture": info["architecture"]}
    (environment / MARKER).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print("安装完成。运行同一入口的 run 命令；不会自动启动监控。")


def launch(repo: Path, environment: Path, home: Path, port: int, *, no_browser: bool) -> None:
    try:
        receipt = json.loads((environment / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError("环境未完成安装，请先执行 install；启动不会自动补装依赖") from exc
    if (not isinstance(receipt, dict) or receipt.get("schema") != 1
            or receipt.get("repo") != str(repo.resolve())
            or receipt.get("dependencies") != fingerprint(repo)):
        raise SetupError("源码位置或依赖已变化，请新建环境；不自动覆盖旧环境")
    python = environment / "bin/python"
    if not python.is_file() or not (repo / "src/project_watch/web/index.html").is_file():
        raise SetupError("运行环境或前端产物缺失，请重新安装或构建")
    # Diagnostic only; uvicorn still binds strictly, closing the race safely.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SetupError(f"端口 {port} 已被占用，请换 --port；未停止任何进程") from exc
    env = os.environ.copy()
    env["PROJECT_WATCH_HOME"] = str(home)
    command = [str(python), "-I", "-B", "-m", "project_watch.studio", "--port", str(port), "--strict-port"]
    if no_browser:
        command.append("--no-browser")
    print(f"数据目录：{home}\n启动后未暂停项目会按已有配置监控，可能产生服务费用。Ctrl+C 正常退出。", flush=True)
    os.execve(str(python), command, env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ProjectWatch Mac 开发者入口（非安装器）")
    parser.add_argument("action", choices=["doctor", "install", "run"], nargs="?", default="run")
    parser.add_argument("--venv", default=".venv-macos", help="仓库内独立 .venv-* 目录")
    parser.add_argument("--home", help="源码外数据目录；也可设置 PROJECT_WATCH_HOME")
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin":
            raise SetupError("此入口用于 macOS；未修改环境、数据或服务")
        if not 1 <= args.port <= 65535:
            raise SetupError("端口须在 1–65535 之间")
        environment = environment_path(REPO, args.venv)
        home = data_path(REPO, args.home or os.environ.get("PROJECT_WATCH_HOME"))
        if args.action == "run":
            launch(REPO, environment, home, args.port, no_browser=args.no_browser)
        else:
            info = prerequisites()
            if args.action == "doctor":
                print(json.dumps({**info, "environment": str(environment), "data": str(home),
                                  "mac_hardware_acceptance": "pending"}, ensure_ascii=False, indent=2))
            else:
                with install_lease(REPO):
                    install(REPO, environment, info)
        return 0
    except (SetupError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
