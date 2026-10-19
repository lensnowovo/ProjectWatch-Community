import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app, create_private_local_app
from project_watch.extractors import ExtractionError, extract_document

IMAGE = "sha256:" + "a" * 64
HOST = "npipe:////./pipe/dockerDesktopLinuxEngine" if os.name == "nt" else f"unix:///run/user/{os.getuid()}/docker.sock"


def test_private_factory_requires_isolation(monkeypatch):
    monkeypatch.delenv("PROJECT_WATCH_PARSER_IMAGE", raising=False)
    monkeypatch.delenv("PROJECT_WATCH_PARSER_DOCKER_HOST", raising=False)
    with pytest.raises(RuntimeError, match="requires a pinned"):
        create_private_local_app()


def test_partial_configuration_and_failed_preflight_do_not_create_database(tmp_path, monkeypatch):
    database = tmp_path / "private.sqlite3"
    with pytest.raises(ValueError, match="configured together"):
        create_app(database, parser_image=IMAGE)

    def unavailable(*_args):
        raise RuntimeError("rootless engine unavailable")

    monkeypatch.setattr("project_watch.api.validate_parser_runtime", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        create_app(database, parser_image=IMAGE, parser_docker_host=HOST)
    assert not database.exists()


def test_container_failure_never_falls_back_and_retry_preserves_source(tmp_path, monkeypatch):
    preflights = []
    monkeypatch.setattr("project_watch.api.validate_parser_runtime", lambda *args: preflights.append(args))
    app = create_app(tmp_path / "private.sqlite3", parser_image=IMAGE, parser_docker_host=HOST)
    client = TestClient(app)
    assert preflights == [(IMAGE, HOST)]
    assert client.get("/api/runtime").json() == {
        "mode": "local", "parser": "container", "private_local_intake": True,
        "authenticated_review": False,
    }
    project = client.post("/api/projects", json={"name": "Synthetic private pilot"}).json()
    route = f"/api/projects/{project['id']}"
    uploaded = client.post(f"{route}/files", files={"file": ("intro.txt", b"Synthetic product", "text/plain")}).json()

    def forbidden(*_args, **_kwargs):
        pytest.fail("container mode must not use a host parser")

    def unavailable(*_args, **_kwargs):
        raise ExtractionError("parser_unavailable")

    monkeypatch.setattr("project_watch.intake_worker.extract_document", forbidden)
    monkeypatch.setattr("project_watch.intake_worker.extract_document_subprocess", forbidden)
    monkeypatch.setattr("project_watch.intake_worker.extract_document_container", unavailable)
    assert app.state.intake_worker.process_one().status == "failed"
    assert client.get(f"/api/sources/{uploaded['source']['id']}/fragments").json() == []
    response = client.post(f"{route}/intake/{uploaded['job']['id']}/retry")
    assert response.status_code == 200

    def restored(path, *, suffix, image, docker_host):
        assert (image, docker_host) == (IMAGE, HOST)
        return extract_document(path, suffix=suffix)

    monkeypatch.setattr("project_watch.intake_worker.extract_document_container", restored)
    assert app.state.intake_worker.process_one().status == "completed"
    assert len(client.get(f"{route}/sources").json()) == 1
    assert client.get(f"{route}/summary").json()["confirmed_count"] == 0


def test_preview_does_not_claim_isolation(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "preview.sqlite3"))
    assert client.get("/api/runtime").json()["private_local_intake"] is False
