"""Opt-in OS-isolated parser runner for a local Docker Linux engine.

The original is streamed over stdin. No host paths, credentials, volumes or
network are made available to the parser container. Docker access itself is
privileged and must use the operator's local rootless engine in team service.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

from project_watch.extraction_process import (
    MAX_PARSER_OUTPUT_BYTES,
    PARSER_TIMEOUT_SECONDS,
    PDF_PARSER_TIMEOUT_SECONDS,
    _child_environment,
    _decode_result,
)
from project_watch.extractors import ExtractionError, ExtractionResult
from project_watch.file_validation import ALLOWED_SUFFIXES, DEFAULT_MAX_BYTES

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WINDOWS_DOCKER_HOSTS = frozenset(
    {"npipe:////./pipe/dockerDesktopLinuxEngine", "npipe:////./pipe/docker_engine"}
)
_log = logging.getLogger(__name__)


def validate_parser_image(image: str) -> str:
    if not _IMAGE_ID.fullmatch(image):
        raise ValueError("parser image must be a locally pinned sha256 image ID")
    return image


def validate_parser_docker_host(host: str) -> str:
    if os.name == "nt":
        if host not in _WINDOWS_DOCKER_HOSTS:
            raise ValueError("parser Docker host must be a local Windows named pipe")
    elif host != f"unix:///run/user/{os.getuid()}/docker.sock":
        raise ValueError("parser Docker host must be the current user's rootless local socket")
    return host


def _require_nonroot_service_uid(uid: int) -> None:
    if uid == 0:
        raise RuntimeError("parser Docker engine requires a non-root service user")


def _is_rootless_security_options(options: object) -> bool:
    return (
        isinstance(options, list)
        and all(isinstance(option, str) for option in options)
        and any(option in {"name=rootless", "rootless"} for option in options)
    )


def validate_parser_runtime(image: str, host: str) -> None:
    """Fail startup if the pinned image is not present on the local engine."""
    image = validate_parser_image(image)
    host = validate_parser_docker_host(host)
    if os.name != "nt":
        _require_nonroot_service_uid(os.geteuid())
        socket_path = Path(f"/run/user/{os.getuid()}/docker.sock")
        try:
            socket_stat = socket_path.stat()
        except OSError as exc:
            raise RuntimeError("rootless parser Docker socket is unavailable") from exc
        if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid != os.getuid():
            raise RuntimeError("parser Docker socket must be owned by the service user")
    try:
        if os.name != "nt":
            info = subprocess.run(
                ["docker", "--host", host, "info", "--format", "{{json .SecurityOptions}}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                env=_child_environment(),
            )
            try:
                security_options = json.loads(info.stdout)
            except (ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError("cannot verify rootless parser Docker engine") from exc
            if info.returncode != 0 or not _is_rootless_security_options(security_options):
                raise RuntimeError("parser Docker engine must run rootless")
        result = subprocess.run(
            ["docker", "--host", host, "image", "inspect", "--format", "{{.Id}}", image],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            env=_child_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("local parser Docker engine is unavailable") from exc
    if result.returncode != 0 or result.stdout.decode("ascii", errors="replace").strip() != image:
        raise RuntimeError("pinned parser image is unavailable on the local Docker engine")
    _ensure_no_leftover_parsers(host)


def _ensure_no_leftover_parsers(host: str) -> None:
    """Refuse team startup until an operator reviews prior parser containers."""
    host = validate_parser_docker_host(host)
    try:
        result = subprocess.run(
            [
                "docker",
                "--host",
                host,
                "container",
                "ls",
                "--all",
                "--filter",
                "name=^/project-watch-parser-",
                "--format",
                "{{.ID}}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            env=_child_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("cannot verify parser container cleanup") from exc
    if result.returncode != 0:
        raise RuntimeError("cannot verify parser container cleanup")
    if result.stdout.strip():
        raise RuntimeError("leftover parser container requires operator review")


def _container_command(image: str, host: str, name: str, suffix: str) -> list[str]:
    """Do not put original paths, user content or secrets in process arguments."""
    return [
        "docker",
        "--host",
        host,
        "run",
        "--rm",
        "--interactive",
        "--name",
        name,
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--user",
        "65534:65534",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "32",
        "--ulimit",
        "nofile=64:64",
        "--ipc",
        "none",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--log-driver",
        "none",
        "--env",
        "HOME=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        image,
        "--stdin",
        suffix,
    ]


def _cleanup_container(host: str, name: str) -> None:
    # The name is generated locally for this single invocation, never user input.
    try:
        result = subprocess.run(
            ["docker", "--host", host, "rm", "--force", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            env=_child_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            _log.warning("parser_container_cleanup_unconfirmed", extra={"container_name": name})
    except (OSError, subprocess.TimeoutExpired):
        # The caller still reports a failed parse; operators must check for
        # leftover project-watch-parser-* containers after daemon failures.
        _log.warning("parser_container_cleanup_failed", extra={"container_name": name})


def extract_document_container(
    path: Path | str, *, suffix: str, image: str, docker_host: str
) -> ExtractionResult:
    """Stream one bounded original into a no-network, disposable parser container."""
    image = validate_parser_image(image)
    docker_host = validate_parser_docker_host(docker_host)
    suffix = suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ExtractionError("unsupported_format")
    try:
        path = Path(path).resolve(strict=True)
        size = path.stat().st_size
    except OSError as exc:
        raise ExtractionError("invalid_document") from exc
    if size == 0:
        raise ExtractionError("empty_upload")
    if size > DEFAULT_MAX_BYTES:
        raise ExtractionError("limit_exceeded")

    name = "project-watch-parser-" + uuid.uuid4().hex
    output = bytearray()
    oversized = threading.Event()
    process: subprocess.Popen[bytes] | None = None
    reader: threading.Thread | None = None
    failure: str | None = None
    try:
        with path.open("rb") as source:
            process = subprocess.Popen(
                _container_command(image, docker_host, name, suffix),
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_child_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            assert process.stdout is not None

            def read_bounded() -> None:
                assert process is not None and process.stdout is not None
                while chunk := process.stdout.read(65536):
                    if len(output) + len(chunk) > MAX_PARSER_OUTPUT_BYTES:
                        oversized.set()
                        return
                    output.extend(chunk)

            reader = threading.Thread(target=read_bounded, daemon=True)
            reader.start()
            deadline = time.monotonic() + (
                PDF_PARSER_TIMEOUT_SECONDS if suffix == ".pdf" else PARSER_TIMEOUT_SECONDS
            )
            while process.poll() is None and not oversized.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "parser_timeout"
                    break
                oversized.wait(min(0.05, remaining))
            if oversized.is_set():
                failure = "parser_output_invalid"
            if failure is not None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _log.error("parser_docker_cli_did_not_exit")
                failure = "parser_timeout"
            reader.join(timeout=5)
            if reader.is_alive():
                failure = "parser_timeout"
            elif oversized.is_set():
                failure = "parser_output_invalid"
            elif process.returncode != 0 and failure is None:
                failure = "parser_crashed"
    except OSError as exc:
        failure = "parser_crashed"
        raise ExtractionError(failure) from exc
    finally:
        if failure is not None:
            _cleanup_container(docker_host, name)
        if (
            process is not None
            and process.stdout is not None
            and (reader is None or not reader.is_alive())
        ):
            process.stdout.close()
    if failure is not None:
        raise ExtractionError(failure)
    return _decode_result(bytes(output), max_output_bytes=MAX_PARSER_OUTPUT_BYTES)
