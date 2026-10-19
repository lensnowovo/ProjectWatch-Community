"""Bounded connections to a verified, dedicated Project Watch schema."""

from __future__ import annotations

from project_watch.postgres_migrations import verify_postgres_schema


def open_project_watch_pool(dsn: str, *, pool_size: int = 8):
    if not 1 <= pool_size <= 32:
        raise ValueError("PostgreSQL pool size must be between 1 and 32")
    verify_postgres_schema(dsn)
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError("install project-watch postgres extra first") from exc
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=pool_size,
        kwargs={
            "row_factory": dict_row,
            "options": "-c search_path=project_watch,public -c statement_timeout=10000",
        },
        open=False,
    )
    pool.open(wait=True)
    return pool
