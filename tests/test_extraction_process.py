"""The parser child is bounded and receives no application credentials."""

import json
import os
import subprocess
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from project_watch.api import create_app
from project_watch.extraction_process import (
    _decode_result,
    extract_document_subprocess,
)
from project_watch.extractors import ExtractionError
from project_watch.parser_container import (
    _container_command,
    _ensure_no_leftover_parsers,
    _is_rootless_security_options,
    _require_nonroot_service_uid,
    extract_document_container,
    validate_parser_docker_host,
    validate_parser_image,
    validate_parser_runtime,
)
from project_watch.repository import ProjectRepository
from project_watch.team_auth import TeamAuthStore


def test_subprocess_extracts_located_text(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.txt"
    source.write_text("第一行\n第二行", encoding="utf-8")
    result = extract_document_subprocess(source, suffix=".txt")
    assert [(fragment.locator, fragment.text) for fragment in result.fragments] == [
        ("line 1", "第一行"),
        ("line 2", "第二行"),
    ]
    assert not result.needs_ocr


def test_opt_in_container_ocr_reads_synthetic_scan(tmp_path: Path) -> None:
    image_id = os.environ.get("PROJECT_WATCH_PARSER_TEST_IMAGE")
    if not image_id:
        pytest.skip("no pinned local parser test image")
    host = (
        "npipe:////./pipe/dockerDesktopLinuxEngine"
        if os.name == "nt"
        else f"unix:///run/user/{os.getuid()}/docker.sock"
    )
    picture = Image.new("RGB", (1200, 400), "white")
    ImageDraw.Draw(picture).text(
        (80, 110),
        "TOTAL ASSETS 12345",
        fill="black",
        font=ImageFont.load_default(size=72),
    )
    source = tmp_path / "synthetic-scan.pdf"
    canvas = Canvas(str(source))
    canvas.drawImage(ImageReader(picture), 72, 350, 468, 156)
    canvas.save()

    result = extract_document_container(source, suffix=".pdf", image=image_id, docker_host=host)

    assert result.needs_ocr is False
    assert result.fragments[0].locator == "page 1 OCR"
    assert "12345" in result.fragments[0].text


def test_subprocess_drops_secret_environment_and_crash_is_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "synthetic.txt"
    source.write_text("synthetic", encoding="utf-8")
    monkeypatch.setenv("PROJECT_WATCH_DATABASE_URL", "synthetic-secret-marker")

    def crashed(_args, **kwargs):
        assert "PROJECT_WATCH_DATABASE_URL" not in kwargs["env"]
        assert kwargs["env"]["OPENBLAS_NUM_THREADS"] == "1"
        assert kwargs["env"]["OMP_NUM_THREADS"] == "1"
        assert kwargs["timeout"] == 45
        return CompletedProcess([], 1, b"")

    monkeypatch.setattr("project_watch.extraction_process.subprocess.run", crashed)
    with pytest.raises(ExtractionError) as exc:
        extract_document_subprocess(source, suffix=".txt", timeout_seconds=45)
    assert exc.value.error_code == "parser_crashed"


def test_subprocess_timeout_and_invalid_result_are_not_persisted(tmp_path: Path, monkeypatch):
    source = tmp_path / "synthetic.txt"
    source.write_text("synthetic", encoding="utf-8")

    def timed_out(_args, **_kwargs):
        raise TimeoutExpired("synthetic-parser", 1)

    monkeypatch.setattr("project_watch.extraction_process.subprocess.run", timed_out)
    with pytest.raises(ExtractionError) as exc:
        extract_document_subprocess(source, suffix=".txt", timeout_seconds=45)
    assert exc.value.error_code == "parser_timeout"
    with pytest.raises(ExtractionError) as exc:
        _decode_result(
            b'{"fragments":[{"ordinal":2,"locator":"line 1","text":"x"}],"needs_ocr":false}'
        )
    assert exc.value.error_code == "parser_output_invalid"


def test_container_configuration_is_pinned_local_and_has_no_host_mount() -> None:
    image = "sha256:" + "a" * 64
    assert validate_parser_image(image) == image
    with pytest.raises(ValueError):
        validate_parser_image("project-watch-parser:latest")
    with pytest.raises(ValueError):
        validate_parser_docker_host("tcp://127.0.0.1:2375")
    host = (
        "npipe:////./pipe/dockerDesktopLinuxEngine"
        if os.name == "nt"
        else f"unix:///run/user/{os.getuid()}/docker.sock"
    )
    assert validate_parser_docker_host(host) == host
    command = _container_command(image, host, "project-watch-parser-synthetic", ".txt")
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert "--cap-drop" in command
    assert "--log-driver" in command and command[command.index("--log-driver") + 1] == "none"
    assert "--mount" not in command and "--volume" not in command
    assert command[-3:] == [image, "--stdin", ".txt"]


def test_rootless_engine_identity_requires_an_exact_security_option() -> None:
    assert _is_rootless_security_options(["name=seccomp,profile=builtin", "name=rootless"])
    assert _is_rootless_security_options(["seccomp", "rootless"])
    assert not _is_rootless_security_options(["name=seccomp", "name=not-rootless"])
    assert not _is_rootless_security_options(["name=seccomp", "rootless-disabled"])
    assert not _is_rootless_security_options(["name=rootless", 1])


def test_linux_team_service_must_not_run_as_root() -> None:
    with pytest.raises(RuntimeError, match="non-root service user"):
        _require_nonroot_service_uid(0)
    _require_nonroot_service_uid(1000)


def test_runtime_refuses_leftover_parser_or_unverified_daemon(monkeypatch) -> None:
    host = (
        "npipe:////./pipe/dockerDesktopLinuxEngine"
        if os.name == "nt"
        else f"unix:///run/user/{os.getuid()}/docker.sock"
    )
    monkeypatch.setenv("PROJECT_WATCH_DATABASE_URL", "synthetic-secret-marker")

    def docker_result(_command, **kwargs):
        assert _command == [
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
        ]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 10
        assert "PROJECT_WATCH_DATABASE_URL" not in kwargs["env"]
        return CompletedProcess(_command, 0, b"synthetic-leftover\n")

    monkeypatch.setattr("project_watch.parser_container.subprocess.run", docker_result)
    with pytest.raises(RuntimeError, match="leftover parser container"):
        _ensure_no_leftover_parsers(host)

    def daemon_failed(command, **_kwargs):
        return CompletedProcess(command, 1, b"")

    monkeypatch.setattr("project_watch.parser_container.subprocess.run", daemon_failed)
    with pytest.raises(RuntimeError, match="cannot verify parser container cleanup"):
        _ensure_no_leftover_parsers(host)

    def daemon_timeout(_command, **_kwargs):
        raise TimeoutExpired("synthetic-docker", 10)

    monkeypatch.setattr("project_watch.parser_container.subprocess.run", daemon_timeout)
    with pytest.raises(RuntimeError, match="cannot verify parser container cleanup"):
        _ensure_no_leftover_parsers(host)


def test_runtime_accepts_clean_parser_container_list(monkeypatch) -> None:
    host = (
        "npipe:////./pipe/dockerDesktopLinuxEngine"
        if os.name == "nt"
        else f"unix:///run/user/{os.getuid()}/docker.sock"
    )
    monkeypatch.setattr(
        "project_watch.parser_container.subprocess.run",
        lambda command, **_kwargs: CompletedProcess(command, 0, b""),
    )
    _ensure_no_leftover_parsers(host)


@pytest.mark.skipif(
    not os.environ.get("PROJECT_WATCH_PARSER_TEST_IMAGE"),
    reason="set PROJECT_WATCH_PARSER_TEST_IMAGE to a locally built synthetic parser image ID",
)
def test_container_parser_extracts_synthetic_file_without_host_mount(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.txt"
    source.write_text("第一行\n第二行", encoding="utf-8")
    image = os.environ["PROJECT_WATCH_PARSER_TEST_IMAGE"]
    host = os.environ["PROJECT_WATCH_PARSER_DOCKER_HOST"]
    validate_parser_runtime(image, host)
    result = extract_document_container(source, suffix=".txt", image=image, docker_host=host)
    assert [(fragment.locator, fragment.text) for fragment in result.fragments] == [
        ("line 1", "第一行"),
        ("line 2", "第二行"),
    ]
    probe = (
        "import json,os; "
        "print(json.dumps({'uid':os.getuid(), "
        "'interfaces':os.listdir('/sys/class/net'), "
        "'root_readonly':bool(os.statvfs('/').f_flag & os.ST_RDONLY), "
        "'memory_max':open('/sys/fs/cgroup/memory.max').read().strip()}))"
    )
    command = _container_command(image, host, "project-watch-parser-synthetic-probe", ".txt")
    command = [*command[:-3], "--entrypoint", "python", image, "-c", probe]
    checked = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        check=True,
    )
    boundary = json.loads(checked.stdout)
    assert boundary["uid"] == 65534
    assert boundary["interfaces"] == ["lo"]
    assert boundary["root_readonly"]
    assert boundary["memory_max"] == str(512 * 1024 * 1024)


@pytest.mark.skipif(
    not os.environ.get("PROJECT_WATCH_PARSER_TEST_IMAGE"),
    reason="set PROJECT_WATCH_PARSER_TEST_IMAGE to a locally built synthetic parser image ID",
)
def test_container_parser_has_patched_xml_and_regex_libraries() -> None:
    image = os.environ["PROJECT_WATCH_PARSER_TEST_IMAGE"]
    host = os.environ["PROJECT_WATCH_PARSER_DOCKER_HOST"]
    probe = (
        "import json,subprocess; from lxml import etree; "
        "print(json.dumps({'libxml':etree.LIBXML_VERSION, "
        "'libxslt':etree.LIBXSLT_VERSION, "
        "'pcre2':subprocess.check_output(["
        "'apk','list','--installed','pcre2'],text=True).strip()}))"
    )
    command = _container_command(image, host, "project-watch-parser-synthetic-version", ".txt")
    command = [*command[:-3], "--entrypoint", "python", image, "-c", probe]
    checked = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        check=True,
    )
    versions = json.loads(checked.stdout)
    assert versions["libxml"] == [2, 15, 4]
    assert versions["libxslt"] == [1, 1, 45]
    assert versions["pcre2"].startswith("pcre2-10.49-r0 ")


@pytest.mark.skipif(
    not os.environ.get("PROJECT_WATCH_PARSER_TEST_IMAGE"),
    reason="set PROJECT_WATCH_PARSER_TEST_IMAGE to a locally built synthetic parser image ID",
)
def test_container_timeout_does_not_leave_a_running_parser(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "synthetic.txt"
    source.write_text("synthetic", encoding="utf-8")
    name = "project-watch-parser-synthetic-timeout"
    monkeypatch.setattr(
        "project_watch.parser_container.uuid.uuid4",
        lambda: SimpleNamespace(hex="synthetic-timeout"),
    )
    monkeypatch.setattr("project_watch.parser_container.PARSER_TIMEOUT_SECONDS", 0.05)
    host = os.environ["PROJECT_WATCH_PARSER_DOCKER_HOST"]
    with pytest.raises(ExtractionError) as exc:
        extract_document_container(
            source,
            suffix=".txt",
            image=os.environ["PROJECT_WATCH_PARSER_TEST_IMAGE"],
            docker_host=host,
        )
    assert exc.value.error_code == "parser_timeout"
    inspection = subprocess.run(
        ["docker", "--host", host, "container", "inspect", name],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert inspection.returncode != 0


@pytest.mark.skipif(
    not os.environ.get("PROJECT_WATCH_PARSER_TEST_IMAGE"),
    reason="set PROJECT_WATCH_PARSER_TEST_IMAGE to a locally built synthetic parser image ID",
)
def test_container_output_limit_removes_synthetic_parser(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "synthetic.txt"
    source.write_text("synthetic", encoding="utf-8")
    name = "project-watch-parser-synthetic-output"
    monkeypatch.setattr(
        "project_watch.parser_container.uuid.uuid4", lambda: SimpleNamespace(hex="synthetic-output")
    )
    monkeypatch.setattr("project_watch.parser_container.MAX_PARSER_OUTPUT_BYTES", 1)
    host = os.environ["PROJECT_WATCH_PARSER_DOCKER_HOST"]
    with pytest.raises(ExtractionError) as exc:
        extract_document_container(
            source,
            suffix=".txt",
            image=os.environ["PROJECT_WATCH_PARSER_TEST_IMAGE"],
            docker_host=host,
        )
    assert exc.value.error_code == "parser_output_invalid"
    inspection = subprocess.run(
        ["docker", "--host", host, "container", "inspect", name],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert inspection.returncode != 0


def test_sqlite_team_worker_uses_subprocess_for_uploaded_material(tmp_path: Path) -> None:
    path = tmp_path / "team.sqlite3"
    ProjectRepository(path)
    auth = TeamAuthStore(path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    app = create_app(path, team_auth=auth, team_origin="https://testserver")
    assert app.state.intake_worker.isolated
    with TestClient(app, base_url="https://testserver") as client:
        logged = client.post(
            "/api/auth/login",
            json={"username": "admin@example.test", "password": "synthetic-admin-password"},
        )
        csrf = {"X-CSRF-Token": logged.json()["csrf_token"]}
        project = client.post("/api/projects", json={"name": "Synthetic"}, headers=csrf).json()
        intake = client.post(
            f"/api/projects/{project['id']}/files",
            files={"file": ("intro.txt", "合成项目资料".encode(), "text/plain")},
            headers=csrf,
        )
        assert intake.status_code == 202
        assert app.state.intake_worker.process_one().status == "completed"
        fragments = client.get(
            f"/api/projects/{project['id']}/sources/{intake.json()['source']['id']}/fragments"
        ).json()
        assert fragments[0]["text"] == "合成项目资料"


def test_team_worker_records_child_failure_without_fragments(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "team.sqlite3"
    ProjectRepository(path)
    auth = TeamAuthStore(path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    app = create_app(path, team_auth=auth, team_origin="https://testserver")

    def parser_timeout(_path, *, suffix, timeout_seconds, max_output_bytes):
        assert suffix == ".txt"
        assert timeout_seconds > 0 and max_output_bytes > 0
        raise ExtractionError("parser_timeout")

    monkeypatch.setattr("project_watch.intake_worker.extract_document_subprocess", parser_timeout)
    with TestClient(app, base_url="https://testserver") as client:
        logged = client.post(
            "/api/auth/login",
            json={"username": "admin@example.test", "password": "synthetic-admin-password"},
        )
        csrf = {"X-CSRF-Token": logged.json()["csrf_token"]}
        project = client.post("/api/projects", json={"name": "Synthetic"}, headers=csrf).json()
        intake = client.post(
            f"/api/projects/{project['id']}/files",
            files={"file": ("intro.txt", b"synthetic", "text/plain")},
            headers=csrf,
        ).json()
        job = app.state.intake_worker.process_one()
        assert job.status == "failed"
        assert job.error_code == "parser_timeout"
        assert (
            client.get(
                f"/api/projects/{project['id']}/sources/{intake['source']['id']}/fragments"
            ).json()
            == []
        )
