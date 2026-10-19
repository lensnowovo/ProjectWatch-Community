"""Synthetic, offline same-instance and copy boundaries for Studio Agents."""

import shutil

from project_watch.repository import ProjectRepository
from project_watch.studio.application import StudioApplication
from project_watch.studio.store import StudioStore


def test_database_copied_to_another_machine_same_path_rebinds_and_revokes(tmp_path, monkeypatch):
    import pytest

    from project_watch.studio import application

    monkeypatch.setattr(application, "machine_identity", lambda: "synthetic-host-a")
    service, pid = make_service(tmp_path / "machine.sqlite3")
    grant = service.issue_grant(pid, "read", "host-a")
    monkeypatch.setattr(application, "machine_identity", lambda: "synthetic-host-b")
    moved = StudioApplication(service.store)
    assert moved.instance_id != service.instance_id
    with pytest.raises(application.AccessDenied):
        moved.authorize(grant["token"], pid, "read")


def make_service(path):
    repository = ProjectRepository(path)
    project = repository.create_project("Synthetic Optics")
    store = StudioStore(path)
    store.upsert_project(project.id, {"legal_name": project.name}, consent=False)
    return StudioApplication(store), project.id


def test_instance_survives_restart_and_copy_is_a_different_instance(tmp_path):
    path = tmp_path / "source.sqlite3"
    service, project_id = make_service(path)
    identity = service.manifest()["instance_id"]
    grant = service.issue_grant(project_id, "read", "synthetic-reader")
    restarted = StudioApplication(StudioStore(path))
    assert restarted.manifest()["instance_id"] == identity
    assert restarted.authorize(grant["token"], project_id, "read")["id"] == grant["id"]
    copied = tmp_path / "copy.sqlite3"
    shutil.copyfile(path, copied)
    copy_service = StudioApplication(StudioStore(copied))
    assert copy_service.manifest()["instance_id"] != identity
    assert str(tmp_path) not in str(copy_service.manifest())
    import pytest

    from project_watch.studio.application import AccessDenied

    with pytest.raises(AccessDenied):
        copy_service.authorize(grant["token"], project_id, "read")


def test_reads_do_not_recover_or_enqueue_running_jobs(tmp_path):
    service, project_id = make_service(tmp_path / "read.sqlite3")
    job = service.store.enqueue(project_id, "external")
    service.store.claim()
    service.project(project_id)
    service.search("Optics", [project_id])
    assert service.store.jobs(project_id)[0]["status"] == "running"
    assert service.store.jobs(project_id)[0]["id"] == job["id"]
