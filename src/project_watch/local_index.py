"""Recompute only a local virtual filing index from already extracted fragments.

Run offline with an explicit database and project ID. Does not reparse originals,
approve facts, replace observations, or touch the institutional database.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from project_watch.native_intake import analyze_document
from project_watch.repository import ProjectRepository


def reindex_local_project(db_path: Path, project_id: str, *, apply: bool = False) -> int:
    if not db_path.is_file():
        raise ValueError("an existing explicit local database is required")
    repository = ProjectRepository(db_path)
    updates = []
    for source in repository.list_sources(project_id):
        if source.kind != "document" or source.topic is None:
            continue
        result = analyze_document(source.reference, repository.list_fragments(source.id))
        if (source.topic, source.topic_basis) != (result.primary_topic, result.topic_basis):
            updates.append((result.primary_topic, result.topic_basis, source.id, project_id))
    if apply and updates:
        with sqlite3.connect(db_path) as connection:
            connection.executemany(
                "UPDATE document_index SET topic=?, basis=?, rule_version=2 WHERE source_id=? AND project_id=?",
                updates,
            )
    return len(updates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    count = reindex_local_project(args.db, args.project_id, apply=args.apply)
    print(f"{'Updated' if args.apply else 'Would update'} {count} virtual index entries; facts unchanged")


if __name__ == "__main__":
    main()
