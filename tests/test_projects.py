from pathlib import Path

import pytest

from project_watch.repository import ProjectRepository


def test_project_starts_unverified_without_invented_identity(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")

    project = repository.create_project("  示例项目  ")

    assert project.id
    assert project.name == "示例项目"
    assert project.identity_status == "unverified"
    assert repository.get_project(project.id) == project


def test_same_normalized_name_is_idempotent(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")

    first = repository.create_project("Example   Project")
    second = repository.create_project(" example project ")

    assert second.id == first.id
    assert second.name == "Example Project"
    assert len(repository.list_projects()) == 1


def test_project_persists_between_repository_instances(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    first = ProjectRepository(path).create_project("Test Company")

    assert ProjectRepository(path).get_project(first.id) == first


@pytest.mark.parametrize("name", ["", "  \t ", "x" * 201])
def test_invalid_project_name_rejected(tmp_path: Path, name: str) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")

    with pytest.raises(ValueError, match="project name"):
        repository.create_project(name)
