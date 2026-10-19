import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app, create_local_app
from project_watch.repository import ProjectRepository


def test_local_factory_requires_explicit_database_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROJECT_WATCH_DB_PATH", raising=False)

    with pytest.raises(RuntimeError, match="PROJECT_WATCH_DB_PATH"):
        create_local_app()


def test_database_parent_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="database directory"):
        ProjectRepository(tmp_path / "missing" / "watch.sqlite3")


def test_duplicate_observation_submission_is_idempotent(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Company")
    source = repository.add_source(
        project.id, kind="document", reference="example.pdf", content_sha256="a" * 64
    )
    fields = dict(
        source_id=source.id,
        subject="revenue",
        statement="Synthetic revenue figure",
        number=Decimal("120.50"),
        unit="CNY million",
        period="2025 FY",
        scope="company",
        source_locator="p. 3",
    )

    first = repository.add_observation(project.id, **fields)
    repeated = repository.add_observation(project.id, **fields)

    assert repeated.id == first.id
    assert len(repository.list_observations(project.id)) == 1


def test_invalid_observation_does_not_leave_partial_record(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    project = client.post("/api/projects", json={"name": "Synthetic Company"}).json()
    source = client.post(
        f"/api/projects/{project['id']}/sources",
        json={"kind": "document", "reference": "example.pdf", "content_sha256": "a" * 64},
    ).json()

    response = client.post(
        f"/api/projects/{project['id']}/observations",
        json={
            "source_id": source["id"],
            "subject": "revenue",
            "statement": "Number missing a reporting period",
            "source_locator": "p. 3",
            "number": "10",
            "unit": "CNY million",
        },
    )

    assert response.status_code == 422
    assert "sqlite" not in response.text.lower()
    assert client.get(f"/api/projects/{project['id']}/observations").json() == []


def test_additive_fingerprint_migration_preserves_legacy_observations(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, name_key TEXT,
                                   identity_status TEXT, created_at TEXT);
            CREATE TABLE sources (id TEXT PRIMARY KEY, project_id TEXT, kind TEXT,
                                  reference TEXT, content_sha256 TEXT, created_at TEXT);
            CREATE TABLE observations (
                id TEXT PRIMARY KEY, project_id TEXT, source_id TEXT, subject TEXT,
                statement TEXT, number TEXT, unit TEXT, period TEXT, scope TEXT,
                source_locator TEXT, status TEXT, version INTEGER, created_at TEXT
            );
            INSERT INTO projects VALUES ('p1', 'Synthetic Company', 'synthetic company',
                                         'unverified', '2026-01-01T00:00:00+00:00');
            INSERT INTO sources VALUES ('s1', 'p1', 'document', 'synthetic.pdf',
                                       'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                                       '2026-01-01T00:00:00+00:00');
            INSERT INTO observations VALUES ('o1', 'p1', 's1', 'product', 'Synthetic claim',
                                            NULL, NULL, NULL, NULL, 'p. 1', 'candidate', 1,
                                            '2026-01-01T00:00:00+00:00');
            INSERT INTO observations VALUES ('o2', 'p1', 's1', 'product', 'Synthetic claim',
                                            NULL, NULL, NULL, NULL, 'p. 1', 'candidate', 1,
                                            '2026-01-02T00:00:00+00:00');
            """
        )

    repository = ProjectRepository(path)

    assert len(repository.list_observations("p1")) == 2
    repeated = repository.add_observation(
        "p1",
        source_id="s1",
        subject="product",
        statement="Synthetic claim",
        source_locator="p. 1",
    )
    assert repeated.id == "o1"
    assert len(repository.list_observations("p1")) == 2

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE observations SET fingerprint = NULL WHERE id = 'o2'")
    assert len(ProjectRepository(path).list_observations("p1")) == 2
