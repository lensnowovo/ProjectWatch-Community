"""Verified, non-overwriting backups for the single-node SQLite preview."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


class BackupError(RuntimeError):
    """Backup or restore cannot be verified safely."""


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _integrity_check(path: Path) -> None:
    try:
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
        ) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise BackupError("database integrity check failed") from exc
    if result is None or result[0] != "ok":
        raise BackupError("database integrity check failed")


def _object_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise BackupError("object store is not a regular directory")
    objects: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackupError("object store contains a symbolic link")
        if path.is_dir():
            continue
        relative = path.relative_to(root)
        if (
            len(relative.parts) != 2
            or len(relative.name) != 64
            or relative.parent.name != relative.name[:2]
            or any(character not in "0123456789abcdef" for character in relative.name)
        ):
            raise BackupError("object store contains an unexpected file")
        if _sha256(path) != relative.name:
            raise BackupError("object hash mismatch")
        objects.append(path)
    return sorted(objects)


def _safe_relative(value: str, database: str) -> bool:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or ":" in value or "\\" in value:
        return False
    if value == database:
        return True
    parts = path.parts
    return (
        len(parts) == 3
        and parts[0] == "objects"
        and len(parts[2]) == 64
        and parts[1] == parts[2][:2]
        and all(character in "0123456789abcdef" for character in parts[2])
    )


def create_backup(db_path: Path | str, backup_dir: Path | str) -> dict[str, object]:
    """Take an SQLite online snapshot and copy verified content-addressed originals.

    The caller must pause upload/worker writes while the objects are copied; the
    SQLite snapshot itself is WAL-consistent. Destinations are created once and
    never replaced. A failed attempt is left in place without a valid manifest.
    """

    raw_database = Path(db_path)
    database = raw_database.resolve()
    destination = Path(backup_dir).resolve()
    if not database.is_file() or raw_database.is_symlink():
        raise BackupError("database must be an existing regular file")
    if destination.exists():
        raise BackupError("backup destination already exists")
    if destination.is_relative_to(database.parent):
        raise BackupError("backup must be outside the live data directory")
    if not destination.parent.is_dir():
        raise BackupError("backup parent directory does not exist")

    destination.mkdir()
    backup_database = destination / database.name
    with (
        closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(backup_database)) as target,
    ):
        source.backup(target)
    _integrity_check(backup_database)
    files = {database.name: _sha256(backup_database)}
    object_root = database.parent / "objects"
    for original in _object_files(object_root):
        relative = original.relative_to(database.parent)
        copy_path = destination / relative
        copy_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, copy_path)
        digest = _sha256(copy_path)
        if digest != original.name:
            raise BackupError("copied object hash mismatch")
        files[relative.as_posix()] = digest
    (destination / "objects").mkdir(exist_ok=True)
    manifest: dict[str, object] = {
        "format_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "database": database.name,
        "files": files,
    }
    with (destination / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
    verify_backup(destination)
    return manifest


def verify_backup(backup_dir: Path | str) -> dict[str, object]:
    """Verify manifest, every file hash, object names, and SQLite integrity."""

    raw_root = Path(backup_dir)
    root = raw_root.resolve()
    if not root.is_dir() or raw_root.is_symlink():
        raise BackupError("backup directory does not exist")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError("backup manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise BackupError("unsupported backup manifest")
    database = manifest.get("database")
    files = manifest.get("files")
    if (
        not isinstance(database, str)
        or not database.endswith(".sqlite3")
        or Path(database).name != database
        or not isinstance(files, dict)
        or database not in files
    ):
        raise BackupError("backup manifest has invalid file entries")
    for relative, expected in files.items():
        if (
            not isinstance(relative, str)
            or not _safe_relative(relative, database)
            or not isinstance(expected, str)
            or len(expected) != 64
        ):
            raise BackupError("backup manifest has unsafe file entries")
        path = root / relative
        if any(parent.is_symlink() for parent in path.parents if parent != root) or (
            not path.is_file() or path.is_symlink() or _sha256(path) != expected
        ):
            raise BackupError("backup file hash mismatch")
        if relative != database and Path(relative).name != expected:
            raise BackupError("backup object hash mismatch")
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_files != {*files, "manifest.json"}:
        raise BackupError("backup contains unlisted or missing files")
    _integrity_check(root / database)
    return manifest


def restore_backup(backup_dir: Path | str, restore_dir: Path | str) -> Path:
    """Restore into a new directory and invalidate recovered live credentials."""

    destination = Path(restore_dir).resolve()
    if destination.exists():
        raise BackupError("restore destination already exists")
    if not destination.parent.is_dir():
        raise BackupError("restore parent directory does not exist")
    manifest = verify_backup(backup_dir)
    source = Path(backup_dir).resolve()
    if destination.is_relative_to(source):
        raise BackupError("restore must be outside the backup directory")
    destination.mkdir()
    files = manifest["files"]
    assert isinstance(files, dict)
    for relative, digest in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
        if _sha256(target) != digest:
            raise BackupError("restored file hash mismatch")
    (destination / "objects").mkdir(exist_ok=True)
    database = destination / str(manifest["database"])
    _integrity_check(database)
    _invalidate_restored_credentials(database)
    _integrity_check(database)
    return database


def _invalidate_restored_credentials(database: Path) -> None:
    """A restored instance must require fresh browser sessions and executor grants."""
    now = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(database)) as connection, connection:
        # The restored directory must not depend on an unlisted WAL sidecar.
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("BEGIN IMMEDIATE")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "team_sessions" in tables:
            connection.execute(
                "UPDATE team_sessions SET revoked_at = ? WHERE revoked_at IS NULL", (now,)
            )
        if "team_executor_tokens" in tables:
            connection.execute(
                "UPDATE team_executor_tokens SET revoked_at = ? WHERE revoked_at IS NULL", (now,)
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Project Watch verified SQLite backup")
    commands = parser.add_subparsers(dest="command", required=True)
    backup_command = commands.add_parser("backup")
    backup_command.add_argument("--db", type=Path, required=True)
    backup_command.add_argument("--dest", type=Path, required=True)
    verify_command = commands.add_parser("verify")
    verify_command.add_argument("--backup", type=Path, required=True)
    restore_command = commands.add_parser("restore")
    restore_command.add_argument("--backup", type=Path, required=True)
    restore_command.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backup":
        result = create_backup(args.db, args.dest)
        print(f"Verified backup: {len(result['files'])} files")
    elif args.command == "verify":
        result = verify_backup(args.backup)
        print(f"Verified backup: {len(result['files'])} files")
    else:
        result = restore_backup(args.backup, args.dest)
        print(f"Verified restore: {result}")


if __name__ == "__main__":
    main()
