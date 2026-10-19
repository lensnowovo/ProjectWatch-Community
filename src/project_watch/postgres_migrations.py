"""Explicit bootstrap and reviewed upgrade for a dedicated Project Watch database.

Neither operation runs during web app startup.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from importlib import resources

SCHEMA = "project_watch"
VERSION = 12
_MIGRATIONS = {
    1: "001_initial.sql",
    2: "002_document_index.sql",
    3: "003_review_impacts.sql",
    4: "004_source_run_matching.sql",
    5: "005_project_identity_reviews.sql",
    6: "006_observation_basis.sql",
    7: "007_source_rate_limit.sql",
    8: "008_competitive_peers.sql",
    9: "009_brave_news_budget.sql",
    10: "010_watch_items.sql",
    11: "011_watch_item_lifecycle.sql",
    12: "012_holding_snapshots.sql",
}
_LOCK_ID = 604596779367113459


class MigrationError(RuntimeError):
    """The target is not safe to bootstrap or its schema is inconsistent."""


@dataclass(frozen=True)
class SchemaState:
    database: str
    version: int
    checksum: str | None


def _psycopg():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("install the project-watch postgres extra first") from exc
    return psycopg


def _migration_sql(version: int = 1) -> str:
    if version not in _MIGRATIONS:
        raise MigrationError("unsupported Project Watch migration version")
    return (
        resources.files("project_watch")
        .joinpath("migrations", _MIGRATIONS[version])
        .read_text(encoding="utf-8")
    )


def _checksum(version: int) -> str:
    return hashlib.sha256(_migration_sql(version).encode("utf-8")).hexdigest()


def _schema_state(connection) -> SchemaState:
    database = connection.execute("SELECT current_database()").fetchone()[0]
    exists = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)", (SCHEMA,)
    ).fetchone()[0]
    if not exists:
        return SchemaState(database, 0, None)
    version_table = connection.execute(
        "SELECT to_regclass('project_watch.schema_version')"
    ).fetchone()[0]
    if version_table is None:
        raise MigrationError("Project Watch schema exists without a version ledger; restore backup")
    rows = connection.execute(
        "SELECT version, checksum FROM project_watch.schema_version ORDER BY version"
    ).fetchall()
    if not rows or [int(row[0]) for row in rows] != list(range(1, len(rows) + 1)):
        raise MigrationError("Project Watch version ledger is incomplete or ambiguous")
    if len(rows) > VERSION:
        raise MigrationError("Project Watch schema is newer than this runtime")
    for version, checksum in rows:
        if checksum != _checksum(int(version)):
            raise MigrationError("Project Watch migration checksum/version mismatch")
    return SchemaState(database, int(rows[-1][0]), str(rows[-1][1]))


def inspect_postgres(dsn: str) -> SchemaState:
    """Read schema state without changing the target database."""
    psycopg = _psycopg()
    with psycopg.connect(dsn) as connection:
        return _schema_state(connection)


def verify_postgres_schema(dsn: str) -> SchemaState:
    """Reject unknown or locally changed schema without mutating the database."""
    state = inspect_postgres(dsn)
    if state.version != VERSION or state.checksum != _checksum(VERSION):
        raise MigrationError("Project Watch migration checksum/version mismatch")
    return state


def bootstrap_postgres(dsn: str) -> SchemaState:
    """Create the current schema atomically, only if the target database is empty.

    Existing databases are deliberately rejected, even if they merely have a
    public table: an operator must not accidentally initialize Greenpine DB.
    Existing v1 databases require an independently verified backup and a
    separate explicit upgrade command.
    """
    psycopg = _psycopg()
    with psycopg.connect(dsn) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
        state = _schema_state(connection)
        if state.version:
            if state.version != VERSION:
                raise MigrationError("existing Project Watch schema requires explicit upgrade")
            return state
        user_tables = connection.execute(
            """SELECT n.nspname, c.relname
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE c.relkind IN ('r', 'p', 'f')
                 AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                 AND n.nspname NOT LIKE 'pg_toast%'
                 AND n.nspname NOT LIKE 'pg_temp_%'
               LIMIT 1"""
        ).fetchone()
        if user_tables is not None:
            raise MigrationError("bootstrap requires an empty dedicated database")
        connection.execute("CREATE SCHEMA project_watch")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        connection.execute(
            """CREATE TABLE schema_version (
                   version integer PRIMARY KEY CHECK (version > 0),
                   checksum text NOT NULL CHECK (length(checksum) = 64),
                   applied_at timestamptz NOT NULL DEFAULT now()
               )"""
        )
        for version in range(1, VERSION + 1):
            connection.execute(_migration_sql(version))
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, _checksum(version)),
            )
        return SchemaState(state.database, VERSION, _checksum(VERSION))


def upgrade_postgres(dsn: str, *, backup_verified: bool = False) -> SchemaState:
    """Explicit sequential upgrade after an operator verified an independent backup."""
    if not backup_verified:
        raise MigrationError("schema upgrade requires a verified backup")
    psycopg = _psycopg()
    with psycopg.connect(dsn) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
        state = _schema_state(connection)
        if state.version < 1 or state.version >= VERSION:
            raise MigrationError("schema upgrade requires a verified older schema")
        connection.execute("SET LOCAL search_path TO project_watch, public")
        for version in range(state.version + 1, VERSION + 1):
            connection.execute(_migration_sql(version))
            connection.execute(
                "INSERT INTO schema_version (version, checksum) VALUES (%s, %s)",
                (version, _checksum(version)),
            )
        return SchemaState(state.database, VERSION, _checksum(VERSION))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect or bootstrap a dedicated Project Watch DB"
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--inspect", action="store_true")
    operation.add_argument("--bootstrap-empty", action="store_true")
    operation.add_argument("--upgrade-existing", action="store_true")
    parser.add_argument("--backup-verified", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("PROJECT_WATCH_DATABASE_URL")
    if not dsn:
        parser.error("PROJECT_WATCH_DATABASE_URL is required")
    if args.inspect:
        state = inspect_postgres(dsn)
    elif args.upgrade_existing:
        state = upgrade_postgres(dsn, backup_verified=args.backup_verified)
    else:
        state = bootstrap_postgres(dsn)
    print(f"database={state.database} schema_version={state.version} checksum={state.checksum}")


if __name__ == "__main__":
    main()
