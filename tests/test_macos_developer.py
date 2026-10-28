"""Offline launcher tests; mocked Darwin is not macOS hardware acceptance."""

import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import macos_developer as mac


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "源码 with spaces"
    (root / "frontend").mkdir(parents=True)
    (root / "pyproject.toml").write_text("synthetic")
    (root / "frontend/package-lock.json").write_text("{}")
    (root / "src/project_watch/web").mkdir(parents=True)
    (root / "src/project_watch/web/index.html").write_text("synthetic")
    return root


@pytest.mark.parametrize("name", ["../shared", ".venv-x/other", ".venv-x\\other", "venv", "/tmp/.venv-a"])
def test_reject_shared_environment(checkout, name):
    with pytest.raises(mac.SetupError):
        mac.environment_path(checkout, name)


def test_reject_data_in_source_or_source_in_data(checkout):
    for path in (checkout, checkout / "data", checkout.parent):
        with pytest.raises(mac.SetupError):
            mac.data_path(checkout, str(path))
    assert mac.data_path(checkout, str(checkout.parent / "私有 data")).name == "私有 data"
    assert mac.data_path(checkout, None) != mac.data_path(checkout / "other", None)


def test_doctor_no_install_or_data_write(checkout, monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac, "REPO", checkout)
    monkeypatch.setattr(mac, "prerequisites", lambda: {"python": "3.12", "npm": "npm"})
    before = sorted(checkout.rglob("*"))
    assert mac.main(["doctor"]) == 0
    assert sorted(checkout.rglob("*")) == before


def test_platform_rejection_before_io(checkout, monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "win32")
    monkeypatch.setattr(mac, "REPO", checkout)
    assert mac.main(["install"]) == 1
    assert not (checkout / ".venv-macos").exists()


def test_install_full_features_and_paths(checkout, monkeypatch):
    calls = []
    monkeypatch.setattr(mac, "run_checked", lambda command, repo: calls.append(command))
    environment = mac.environment_path(checkout, ".venv-macos")
    mac.install(checkout, environment, {"npm": "/node path/npm", "python": "3.12", "architecture": "arm64"})
    assert f"{checkout}[dev,ocr,mcp,team,postgres]" in calls[2]
    assert calls[3][0] == "/node path/npm"
    assert not any("project_watch.studio" in command for command in calls)
    receipt = json.loads((environment / mac.MARKER).read_text(encoding="utf-8"))
    assert receipt["repo"] == str(checkout.resolve())
    with pytest.raises(mac.SetupError, match="环境已存在"):
        mac.install(checkout, environment, {})


def test_failed_install_has_no_ready_receipt(checkout, monkeypatch):
    def fail(command, repo):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(mac, "run_checked", fail)
    environment = mac.environment_path(checkout, ".venv-macos")
    with pytest.raises(subprocess.CalledProcessError):
        mac.install(checkout, environment, {})
    assert environment.exists()
    assert not (environment / mac.MARKER).exists()


def ready(checkout):
    environment = checkout / ".venv-macos"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").touch()
    (environment / mac.MARKER).write_text(json.dumps({
        "schema": 1, "repo": str(checkout.resolve()), "dependencies": mac.fingerprint(checkout),
    }))
    return environment


def test_run_strict_port_private_home_no_install(checkout, monkeypatch):
    environment = ready(checkout)
    captured = []
    monkeypatch.setattr(mac.os, "execve", lambda *args: captured.append(args))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    home = checkout.parent / "私有 data"
    mac.launch(checkout, environment, home, port, no_browser=True)
    executable, argv, env = captured[0]
    assert executable == str(environment / "bin/python")
    assert "--strict-port" in argv and "--no-browser" in argv and "-I" in argv
    assert env["PROJECT_WATCH_HOME"] == str(home)
    assert not home.exists()


def test_busy_port_does_not_launch(checkout, monkeypatch):
    environment = ready(checkout)
    monkeypatch.setattr(mac.os, "execve", lambda *args: pytest.fail("must not launch"))
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with pytest.raises(mac.SetupError, match="已被占用"):
            mac.launch(checkout, environment, checkout.parent / "data", server.getsockname()[1], no_browser=True)


@pytest.mark.parametrize("change", ["moved", "dependencies", "incomplete", "malformed"])
def test_stale_or_partial_environment_never_launches(checkout, change):
    environment = ready(checkout)
    if change == "moved":
        (environment / mac.MARKER).write_text('{"schema":1,"repo":"elsewhere"}')
    elif change == "dependencies":
        (checkout / "pyproject.toml").write_text("changed")
    elif change == "malformed":
        (environment / mac.MARKER).write_text("[]")
    else:
        (environment / mac.MARKER).unlink()
    with pytest.raises(mac.SetupError):
        mac.launch(checkout, environment, checkout.parent / "data", 8901, no_browser=True)


@pytest.mark.parametrize("version,arch,accepted", [("22.0.0", "arm64", True), ("20.1.0", "arm64", False), ("22.0.0", "x64", False)])
def test_node_version_and_architecture(monkeypatch, version, arch, accepted):
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac.sys, "version_info", (3, 12, 0))
    monkeypatch.setattr(mac.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(mac.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(mac.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=json.dumps({"version": version, "arch": arch})))
    if accepted:
        assert mac.prerequisites()["architecture"] == "arm64"
    else:
        with pytest.raises(mac.SetupError):
            mac.prerequisites()


def test_mac_entry_is_lf_and_in_release_scan():
    from scripts.release_audit import _SOURCE_SUFFIXES
    root = Path(__file__).resolve().parents[1]
    data = (root / "Start-ProjectWatch.command").read_bytes()
    assert data.startswith(b"#!/bin/bash\n") and b"\r" not in data
    assert ".command" in _SOURCE_SUFFIXES and ".sh" in _SOURCE_SUFFIXES


def test_source_lease_blocks_install_before_writes(checkout, monkeypatch):
    (checkout / ".git").mkdir()
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac, "REPO", checkout)
    monkeypatch.setattr(mac, "prerequisites", lambda: {})
    monkeypatch.setattr(mac.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=".git\n"))
    def busy(*args):
        raise BlockingIOError("synthetic busy lease")
    monkeypatch.setitem(mac.sys.modules, "fcntl", SimpleNamespace(flock=busy, LOCK_EX=1, LOCK_NB=2, LOCK_UN=4))
    assert mac.main(["install"]) == 1
    assert not (checkout / ".venv-macos").exists()


def test_source_lease_released_on_failed_install(checkout, monkeypatch):
    (checkout / ".git").mkdir()
    operations = []
    monkeypatch.setattr(mac.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=".git\n"))
    monkeypatch.setitem(mac.sys.modules, "fcntl", SimpleNamespace(
        flock=lambda stream, op: operations.append(op), LOCK_EX=1, LOCK_NB=2, LOCK_UN=4,
    ))
    with pytest.raises(RuntimeError, match="synthetic"), mac.install_lease(checkout):
        raise RuntimeError("synthetic install failure")
    assert operations == [3, 4]
