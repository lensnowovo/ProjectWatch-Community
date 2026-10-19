import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.blob_store import BlobStore, UploadError


def test_bad_pdf_magic_rejected_before_source_or_job(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    project = client.post("/api/projects", json={"name": "Synthetic Company"}).json()

    response = client.post(
        f"/api/projects/{project['id']}/files",
        files={"file": ("fake.pdf", b"not a PDF", "application/pdf")},
    )

    assert response.status_code == 422
    assert client.get(f"/api/projects/{project['id']}/sources").json() == []
    assert client.get(f"/api/projects/{project['id']}/intake").json() == []


def test_bad_office_archive_rejected_before_source_or_job(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    project = client.post("/api/projects", json={"name": "Synthetic Company"}).json()

    response = client.post(
        f"/api/projects/{project['id']}/files",
        files={"file": ("fake.docx", b"PK\x03\x04not-a-zip", "application/octet-stream")},
    )

    assert response.status_code == 422
    assert client.get(f"/api/projects/{project['id']}/sources").json() == []
    assert client.get(f"/api/projects/{project['id']}/intake").json() == []


def test_high_compression_office_archive_rejected(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * 1_000_000)
    store = BlobStore(tmp_path / "objects")

    with pytest.raises(UploadError, match="unsafe_archive"):
        store.put_stream("bomb.docx", io.BytesIO(payload.getvalue()))

    assert list((tmp_path / "objects").rglob(".incoming-*")) == []


def test_archive_with_parent_path_rejected(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr("../outside.txt", "synthetic")
    store = BlobStore(tmp_path / "objects")

    with pytest.raises(UploadError, match="unsafe_archive"):
        store.put_stream("unsafe.docx", io.BytesIO(payload.getvalue()))


def test_same_bytes_are_separate_project_sources_but_one_blob(tmp_path: Path) -> None:
    app = create_app(tmp_path / "watch.sqlite3")
    client = TestClient(app)
    first = client.post("/api/projects", json={"name": "First"}).json()
    second = client.post("/api/projects", json={"name": "Second"}).json()
    upload = {"file": ("intro.txt", b"Synthetic source", "text/plain")}

    first_result = client.post(f"/api/projects/{first['id']}/files", files=upload).json()
    second_result = client.post(f"/api/projects/{second['id']}/files", files=upload).json()

    assert first_result["source"]["id"] != second_result["source"]["id"]
    assert first_result["source"]["content_sha256"] == second_result["source"]["content_sha256"]
    store = app.state.intake_worker.blob_store
    digest = first_result["source"]["content_sha256"]
    assert len(list(store.root.rglob(digest))) == 1
