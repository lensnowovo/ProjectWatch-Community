"""Offline recovery drills use only temporary synthetic state."""

import hashlib
import io
import sqlite3
from pathlib import Path

import pytest

from project_watch.backup import BackupError, create_backup, restore_backup, verify_backup
from project_watch.blob_store import BlobStore
from project_watch.repository import ProjectRepository


def test_backup_restore_preserves_wal_sources_originals_and_audit(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    db_path = live / "watch.sqlite3"
    repo = ProjectRepository(db_path)
    blob = BlobStore(live / "objects").put_stream("synthetic.txt", io.BytesIO(b"synthetic"))
    project = repo.create_project("Synthetic Restore")
    source = repo.add_source(
        project.id, kind="document", reference="synthetic.txt", content_sha256=blob.sha256
    )
    candidate = repo.add_observation(
        project.id,
        source_id=source.id,
        subject="revenue",
        statement="Synthetic statement",
        source_locator="line 1",
    )
    repo.confirm_observation(candidate.id, reviewer="unverified-test", expected_version=1)
    plan = repo.create_monitoring_plan(
        project.id, kind="rss", target="https://example.org/synthetic.xml", cadence_minutes=60
    )
    repo.record_source_run(plan.id, status="no_results", error_code=None)

    backup_dir = tmp_path / "backup"
    manifest = create_backup(db_path, backup_dir)
    assert manifest["database"] == "watch.sqlite3"
    assert verify_backup(backup_dir) == manifest

    restored_dir = tmp_path / "restored"
    restored_db = restore_backup(backup_dir, restored_dir)
    restored = ProjectRepository(restored_db)
    assert restored.list_projects() == repo.list_projects()
    assert restored.list_sources(project.id) == repo.list_sources(project.id)
    assert restored.list_observations(project.id) == repo.list_observations(project.id)
    assert restored.review_history(candidate.id) == repo.review_history(candidate.id)
    assert restored.list_audit_events(project.id) == repo.list_audit_events(project.id)
    assert restored.list_monitoring_plans(project.id) == repo.list_monitoring_plans(project.id)
    assert restored.list_source_runs(plan.id) == repo.list_source_runs(plan.id)
    assert (restored_dir / "objects" / blob.sha256[:2] / blob.sha256).read_bytes() == b"synthetic"
    with sqlite3.connect(restored_db) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with pytest.raises(BackupError, match="already exists"):
        restore_backup(backup_dir, restored_dir)


def test_backup_rejects_tampered_object_without_restoring(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    db_path = live / "watch.sqlite3"
    ProjectRepository(db_path)
    blob = BlobStore(live / "objects").put_stream("synthetic.txt", io.BytesIO(b"synthetic"))
    backup_dir = tmp_path / "backup"
    create_backup(db_path, backup_dir)
    object_path = backup_dir / "objects" / blob.sha256[:2] / blob.sha256
    object_path.write_bytes(b"tampered")

    with pytest.raises(BackupError, match="hash mismatch"):
        restore_backup(backup_dir, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_backup_rejects_live_directory_or_existing_destination(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    db_path = live / "watch.sqlite3"
    ProjectRepository(db_path)
    with pytest.raises(BackupError, match="outside the live data directory"):
        create_backup(db_path, live / "inside")
    with pytest.raises(BackupError, match="already exists"):
        create_backup(db_path, live)


def test_backup_manifest_hash_is_actual_database_bytes(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    db_path = live / "watch.sqlite3"
    ProjectRepository(db_path)
    backup_dir = tmp_path / "backup"
    manifest = create_backup(db_path, backup_dir)
    expected = hashlib.sha256((backup_dir / "watch.sqlite3").read_bytes()).hexdigest()
    assert manifest["files"]["watch.sqlite3"] == expected


def test_backup_rejects_unlisted_file(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    db_path = live / "watch.sqlite3"
    ProjectRepository(db_path)
    backup_dir = tmp_path / "backup"
    create_backup(db_path, backup_dir)
    (backup_dir / "unlisted.txt").write_text("not part of this backup", encoding="utf-8")
    with pytest.raises(BackupError, match="unlisted"):
        verify_backup(backup_dir)


def test_team_restore_preserves_audit_but_revokes_live_credentials(tmp_path: Path) -> None:
    pytest.importorskip("argon2")
    from project_watch.team_auth import TeamAuthStore

    live = tmp_path / "live"
    live.mkdir()
    db_path = live / "watch.sqlite3"
    repo = ProjectRepository(db_path)
    auth = TeamAuthStore(db_path)
    admin = auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    project = repo.create_project("Synthetic Team Restore", actor_id=admin.id)
    session_token, _ = auth.login("admin@example.test", "synthetic-admin-password")
    executor_token, _ = auth.issue_executor_token(
        project.id, admin.id, label="synthetic restore", permission="read"
    )
    before_audit = auth.list_security_events()
    backup_dir = tmp_path / "team-backup"
    create_backup(db_path, backup_dir)
    restored_db = restore_backup(backup_dir, tmp_path / "team-restored")
    assert not restored_db.with_name(restored_db.name + "-wal").exists()
    restored = TeamAuthStore(restored_db)
    assert restored.list_security_events() == before_audit
    assert restored.session(session_token) is None
    assert restored.executor_grant(executor_token) is None
    assert restored.list_executor_tokens(project.id)[0].revoked_at is not None
    assert restored.login("admin@example.test", "synthetic-admin-password")[0]
