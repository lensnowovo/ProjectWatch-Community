"""Offline PostgreSQL plus object-store backup for an isolated team database.

Operators must stop API and workers before invoking this module.  The archive
and object tree are verified together; restore targets a *new empty database*
and revokes credentials before the restored service is allowed to start.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from project_watch.backup import BackupError, _object_files, _sha256
from project_watch.postgres_migrations import (
    VERSION,
    MigrationError,
    inspect_postgres,
    verify_postgres_schema,
)

ARCHIVE = "project-watch.dump"
_CONN_ENV = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "dbname": "PGDATABASE",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
}


def _psycopg():
    try:
        import psycopg
    except ImportError as exc:
        raise BackupError("install the project-watch postgres extra first") from exc
    return psycopg


def _client_environment(dsn: str) -> dict[str, str]:
    """Pass credentials through process environment, never command arguments."""
    psycopg = _psycopg()
    settings = psycopg.conninfo.conninfo_to_dict(dsn)
    unknown = set(settings) - set(_CONN_ENV)
    if unknown or not settings.get("dbname"):
        raise BackupError("unsupported or incomplete PostgreSQL connection settings")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment.update(
        {_CONN_ENV[key]: str(value) for key, value in settings.items() if value is not None}
    )
    return environment


def _run_client(command: list[str], environment: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise BackupError("PostgreSQL client executable is unavailable") from exc
    if result.returncode != 0:
        # Never echo client stderr: it may contain server or connection details.
        raise BackupError("PostgreSQL client command failed; keep target quarantined")
    return result.stdout


def _table_counts(dsn: str) -> dict[str, int]:
    psycopg = _psycopg()
    with psycopg.connect(dsn) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'project_watch' ORDER BY tablename"
            )
        ]
        return {
            name: connection.execute(
                psycopg.sql.SQL("SELECT count(*) FROM {}.{}").format(
                    psycopg.sql.Identifier("project_watch"), psycopg.sql.Identifier(name)
                )
            ).fetchone()[0]
            for name in names
        }


def _source_hashes(dsn: str) -> set[str]:
    psycopg = _psycopg()
    with psycopg.connect(dsn) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT content_sha256 FROM project_watch.sources"
            )
        }


def _empty_target(dsn: str) -> str:
    state = inspect_postgres(dsn)
    if state.version != 0:
        raise BackupError("restore target must be a new empty database")
    psycopg = _psycopg()
    with psycopg.connect(dsn) as connection:
        existing = connection.execute(
            """SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE c.relkind IN ('r', 'p', 'f')
                 AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                 AND n.nspname NOT LIKE 'pg_toast%'
                 AND n.nspname NOT LIKE 'pg_temp_%' LIMIT 1"""
        ).fetchone()
        schema = connection.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = 'project_watch'"
        ).fetchone()
    if existing is not None or schema is not None:
        raise BackupError("restore target must be a new empty database")
    return state.database


def create_postgres_backup(
    dsn: str, object_root: Path | str, backup_dir: Path | str
) -> dict[str, object]:
    """Archive a stopped team service; destination is never replaced."""
    try:
        state = verify_postgres_schema(dsn)
    except MigrationError as exc:
        raise BackupError("PostgreSQL source schema is not verified") from exc
    objects = Path(object_root).resolve()
    destination = Path(backup_dir).resolve()
    if not objects.is_dir() or Path(object_root).is_symlink():
        raise BackupError("object store must be an existing regular directory")
    if destination.exists() or destination.is_relative_to(objects):
        raise BackupError("backup destination must be new and outside the object store")
    if not destination.parent.is_dir():
        raise BackupError("backup parent directory does not exist")
    originals = _object_files(objects)
    object_hashes = {path.name for path in originals}
    references = _source_hashes(dsn)
    if references - object_hashes:
        raise BackupError("source original is missing from the object store")
    environment = _client_environment(dsn)
    destination.mkdir()
    archive = destination / ARCHIVE
    _run_client(
        [
            "pg_dump",
            "--format=custom",
            "--schema=project_watch",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(archive),
        ],
        environment,
    )
    if not archive.is_file() or archive.stat().st_size == 0:
        raise BackupError("PostgreSQL archive is empty")
    files = {ARCHIVE: _sha256(archive)}
    for original in originals:
        relative = Path("objects") / original.relative_to(objects)
        copied = destination / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, copied)
        digest = _sha256(copied)
        if digest != copied.name:
            raise BackupError("copied object hash mismatch")
        files[relative.as_posix()] = digest
    (destination / "objects").mkdir(exist_ok=True)
    manifest: dict[str, object] = {
        "format_version": 1,
        "kind": "project-watch-postgres",
        "created_at": datetime.now(UTC).isoformat(),
        "database": state.database,
        "schema_version": state.version,
        "schema_checksum": state.checksum,
        "table_counts": _table_counts(dsn),
        "source_hashes": sorted(references),
        "files": files,
    }
    with (destination / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
    verify_postgres_backup(destination)
    return manifest


def verify_postgres_backup(backup_dir: Path | str) -> dict[str, object]:
    """Check archive/object bytes and reject unlisted or unsafe paths."""
    raw_root = Path(backup_dir)
    root = raw_root.resolve()
    if not root.is_dir() or raw_root.is_symlink():
        raise BackupError("backup directory does not exist")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError("backup manifest is missing or invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format_version") != 1
        or manifest.get("kind") != "project-watch-postgres"
        or manifest.get("schema_version") != VERSION
        or not isinstance(manifest.get("schema_checksum"), str)
        or len(manifest["schema_checksum"]) != 64
        or not isinstance(manifest.get("database"), str)
        or not isinstance(manifest.get("source_hashes"), list)
        or not isinstance(manifest.get("table_counts"), dict)
        or not isinstance(manifest.get("files"), dict)
    ):
        raise BackupError("unsupported PostgreSQL backup manifest")
    files = manifest["files"]
    if ARCHIVE not in files:
        raise BackupError("PostgreSQL archive is missing")
    for relative, digest in files.items():
        if (
            not isinstance(relative, str)
            or "\\" in relative
            or ":" in relative
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise BackupError("backup manifest has unsafe file entries")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise BackupError("backup manifest has unsafe file entries")
        if relative != ARCHIVE and (
            len(path.parts) != 3
            or path.parts[0] != "objects"
            or path.parts[1] != path.parts[2][:2]
            or path.parts[2] != digest
        ):
            raise BackupError("backup manifest has unsafe object entries")
        candidate = root / relative
        if (
            any(parent.is_symlink() for parent in candidate.parents if parent != root)
            or not candidate.is_file()
            or candidate.is_symlink()
            or _sha256(candidate) != digest
        ):
            raise BackupError("backup file hash mismatch")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != {*files, "manifest.json"}:
        raise BackupError("backup contains unlisted or missing files")
    references = manifest["source_hashes"]
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
        for value in references
    ):
        raise BackupError("invalid source hash manifest")
    if set(references) - {Path(path).name for path in files if path != ARCHIVE}:
        raise BackupError("backup is missing a referenced original")
    listing = _run_client(
        ["pg_restore", "--list", str(root / ARCHIVE)],
        _client_environment("dbname=project_watch_verify"),
    )
    if "project_watch" not in listing:
        raise BackupError("archive does not contain the Project Watch schema")
    return manifest


def restore_postgres_backup(
    backup_dir: Path | str, target_dsn: str, object_root: Path | str
) -> None:
    """Restore to a separate empty DB, then revoke captured sessions/grants."""
    manifest = verify_postgres_backup(backup_dir)
    database = _empty_target(target_dsn)
    if database == manifest["database"]:
        raise BackupError("restore database must have a different name from the source")
    destination = Path(object_root).resolve()
    if destination.exists() or not destination.parent.is_dir():
        raise BackupError("restored object store must be a new directory")
    source = Path(backup_dir).resolve()
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise BackupError("restored object store must be separate from the backup")
    environment = _client_environment(target_dsn)
    _run_client(
        [
            "pg_restore",
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            database,
            str(source / ARCHIVE),
        ],
        environment,
    )
    state = verify_postgres_schema(target_dsn)
    if (
        state.checksum != manifest["schema_checksum"]
        or _table_counts(target_dsn) != manifest["table_counts"]
    ):
        raise BackupError("restored schema or table counts differ; keep target quarantined")
    if _source_hashes(target_dsn) != set(manifest["source_hashes"]):
        raise BackupError("restored source references differ; keep target quarantined")
    destination.mkdir()
    files = manifest["files"]
    for relative, digest in files.items():
        if relative == ARCHIVE:
            continue
        copied = destination / Path(relative).relative_to("objects")
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, copied)
        if _sha256(copied) != digest:
            raise BackupError("restored object hash mismatch; keep target quarantined")
    psycopg = _psycopg()
    now = datetime.now(UTC).isoformat()
    with psycopg.connect(target_dsn) as connection:
        connection.execute(
            "UPDATE project_watch.team_sessions SET revoked_at = %s WHERE revoked_at IS NULL",
            (now,),
        )
        connection.execute(
            """UPDATE project_watch.team_executor_tokens SET revoked_at = %s
               WHERE revoked_at IS NULL""",
            (now,),
        )
        connection.execute(
            """INSERT INTO project_watch.team_security_events
               (id, actor_user_id, action, target_user_id, project_id, subject_id,
                previous_role, new_role, created_at)
               VALUES (%s, NULL, 'restore_credentials_revoked', NULL, NULL, NULL,
                       NULL, NULL, %s)""",
            (str(uuid.uuid4()), now),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Project Watch PostgreSQL backup")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("--objects", type=Path, required=True)
    create.add_argument("--dest", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--objects", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify":
        result = verify_postgres_backup(args.backup)
    else:
        dsn = os.environ.get("PROJECT_WATCH_DATABASE_URL")
        if not dsn:
            parser.error("PROJECT_WATCH_DATABASE_URL is required")
        if args.command == "backup":
            result = create_postgres_backup(dsn, args.objects, args.dest)
        else:
            restore_postgres_backup(args.backup, dsn, args.objects)
            print("Verified restore to separate database; captured credentials revoked")
            return
    print(f"Verified PostgreSQL backup: {len(result['files'])} files")


if __name__ == "__main__":
    main()
