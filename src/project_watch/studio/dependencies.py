"""Persisted evidence lifecycle and targeted/coalesced rebuild requests.

This module never starts a model/search job. A stale saved result remains readable
with its original evidence; failed rebuilds cannot erase the warning or history.
"""

from __future__ import annotations

import json
import re
from contextlib import closing

from project_watch.studio.store import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_dependency_state (
    project_id TEXT PRIMARY KEY REFERENCES projects(id), generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_analysis_dependencies (
    project_id TEXT NOT NULL REFERENCES projects(id), artifact_key TEXT NOT NULL,
    kind TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL,
    record TEXT NOT NULL, PRIMARY KEY(project_id,artifact_key)
);
CREATE TABLE IF NOT EXISTS studio_dependency_links (
    project_id TEXT NOT NULL, artifact_key TEXT NOT NULL,
    evidence_version_id TEXT NOT NULL REFERENCES studio_evidence_versions(id),
    PRIMARY KEY(project_id,artifact_key,evidence_version_id),
    FOREIGN KEY(project_id,artifact_key) REFERENCES studio_analysis_dependencies(project_id,artifact_key)
);
CREATE TABLE IF NOT EXISTS studio_object_dependency_links (
    project_id TEXT NOT NULL, artifact_key TEXT NOT NULL, object_id TEXT NOT NULL,
    object_version INTEGER NOT NULL,
    PRIMARY KEY(project_id,artifact_key,object_id),
    FOREIGN KEY(project_id,artifact_key) REFERENCES studio_analysis_dependencies(project_id,artifact_key)
);
CREATE TABLE IF NOT EXISTS studio_rebuild_requests (
    project_id TEXT NOT NULL, artifact_key TEXT NOT NULL, generation INTEGER NOT NULL,
    status TEXT NOT NULL, PRIMARY KEY(project_id,artifact_key)
);
CREATE TABLE IF NOT EXISTS studio_evidence_lifecycle (
    project_id TEXT NOT NULL REFERENCES projects(id), evidence_version_id TEXT NOT NULL REFERENCES studio_evidence_versions(id),
    status TEXT NOT NULL CHECK(status IN ('current','withdrawn')), reason TEXT NOT NULL, actor TEXT NOT NULL,
    PRIMARY KEY(project_id,evidence_version_id)
);
CREATE TABLE IF NOT EXISTS studio_monitor_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id),
    visibility TEXT NOT NULL, kind TEXT NOT NULL, record TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def semantic(record: dict) -> tuple:
    return (record.get("visibility"), record.get("source_type"), record.get("read_scope"), record.get("origin"),
            record.get("propagation_family"), record.get("family_basis"),
            tuple((p["locator_kind"], re.sub(r"\s+", " ", p["text"]).strip()) for p in record["parts"]))


class DependencyStore:
    def __init__(self, store):
        self.store = store
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)

    @staticmethod
    def _generation(db, project_id):
        row = db.execute("SELECT generation FROM studio_dependency_state WHERE project_id=?", (project_id,)).fetchone()
        return row[0] if row else 0

    def generation(self, project_id: str) -> int:
        with closing(self.store._connect()) as db:
            return self._generation(db, project_id)

    @staticmethod
    def _identity(record):
        from project_watch.studio.monitoring_contracts import ObjectInput

        return {k: record.get(k) for k in ObjectInput.model_fields}

    def on_object_saved(self, record, previous, *, db):
        if previous and self._identity(record) == self._identity(previous):
            return
        project_id = record["project_id"]
        generation = self._bump(db, project_id)
        self._change(db, project_id, record["visibility"], "object_revised" if previous else "object_added", {
            "object_id": record["id"], "object_version": record["version"], "generation": generation,
        })
        rows = db.execute("SELECT d.* FROM studio_analysis_dependencies d JOIN studio_object_dependency_links l "
                          "ON l.project_id=d.project_id AND l.artifact_key=d.artifact_key "
                          "WHERE d.project_id=? AND l.object_id=?", (project_id, record["id"])).fetchall()
        for row in rows:
            data = {**json.loads(row["record"]), "status": "stale", "generation": generation,
                    "stale_reason": "object_revised", "invalidated_at": now()}
            db.execute("UPDATE studio_analysis_dependencies SET status='stale',generation=?,record=? WHERE project_id=? AND artifact_key=?",
                       (generation, json.dumps(data), project_id, row["artifact_key"]))
            db.execute("INSERT INTO studio_rebuild_requests VALUES (?,?,?,'queued') ON CONFLICT(project_id,artifact_key) "
                       "DO UPDATE SET generation=excluded.generation,status='queued'", (project_id, row["artifact_key"], generation))

    def track_objects(self, project_id, artifact_key, object_versions, *, db):
        row = db.execute("SELECT record FROM studio_analysis_dependencies WHERE project_id=? AND artifact_key=?", (project_id, artifact_key)).fetchone()
        if not row:
            raise KeyError("artifact dependency must exist before binding objects")
        stale = False
        for object_id, version in object_versions.items():
            current = db.execute("SELECT record FROM studio_monitor_objects WHERE project_id=? AND id=?", (project_id, object_id)).fetchone()
            historical = db.execute("SELECT h.record FROM studio_object_history h JOIN studio_monitor_objects o ON o.id=h.object_id "
                                    "WHERE o.project_id=? AND h.object_id=? AND h.version=?", (project_id, object_id, version)).fetchone()
            if not current or not historical:
                raise KeyError("object version not found in project")
            stale |= self._identity(json.loads(current[0])) != self._identity(json.loads(historical[0]))
        data = {**json.loads(row[0]), "object_versions": object_versions}
        if stale:
            data.update(status="stale", stale_reason="object_revised")
            db.execute("INSERT INTO studio_rebuild_requests VALUES (?,?,?,'queued') ON CONFLICT(project_id,artifact_key) "
                       "DO UPDATE SET generation=excluded.generation,status='queued'", (project_id, artifact_key, data["generation"]))
        db.execute("UPDATE studio_analysis_dependencies SET status=?,record=? WHERE project_id=? AND artifact_key=?",
                   (data["status"], json.dumps(data), project_id, artifact_key))
        db.execute("DELETE FROM studio_object_dependency_links WHERE project_id=? AND artifact_key=?", (project_id, artifact_key))
        db.executemany("INSERT INTO studio_object_dependency_links VALUES (?,?,?,?)",
                       [(project_id, artifact_key, oid, version) for oid, version in object_versions.items()])
        return data

    def _bump(self, db, project_id):
        db.execute("INSERT INTO studio_dependency_state VALUES (?,1) ON CONFLICT(project_id) "
                   "DO UPDATE SET generation=generation+1", (project_id,))
        return self._generation(db, project_id)

    def on_relationship_corrected(self, project_id, *, db):
        generation = self._bump(db, project_id)
        rows = db.execute("SELECT artifact_key,record FROM studio_analysis_dependencies WHERE project_id=? AND kind IN ('brief','watch')", (project_id,)).fetchall()
        for row in rows:
            data = {**json.loads(row["record"]), "status": "stale", "generation": generation,
                    "stale_reason": "relationship_corrected", "invalidated_at": now()}
            db.execute("UPDATE studio_analysis_dependencies SET status='stale',generation=?,record=? WHERE project_id=? AND artifact_key=?",
                       (generation, json.dumps(data), project_id, row["artifact_key"]))
            db.execute("INSERT INTO studio_rebuild_requests VALUES (?,?,?,'queued') ON CONFLICT(project_id,artifact_key) "
                       "DO UPDATE SET generation=excluded.generation,status='queued'", (project_id, row["artifact_key"], generation))

    @staticmethod
    def _change(db, project_id, visibility, kind, record):
        db.execute("INSERT INTO studio_monitor_changes(project_id,visibility,kind,record,created_at) VALUES (?,?,?,?,?)",
                   (project_id, visibility, kind, json.dumps(record, ensure_ascii=False), now()))

    def _invalidate(self, db, project_id, source_key, generation, reason, *, version_id=None):
        rows = db.execute("SELECT DISTINCT d.* FROM studio_analysis_dependencies d JOIN studio_dependency_links l "
                          "ON l.project_id=d.project_id AND l.artifact_key=d.artifact_key "
                          "JOIN studio_evidence_versions e ON e.id=l.evidence_version_id "
                          "WHERE d.project_id=? AND e.project_id=? AND e.source_key=? AND (? IS NULL OR e.id=?)",
                          (project_id, project_id, source_key, version_id, version_id)).fetchall()
        for row in rows:
            record = {**json.loads(row["record"]), "status": "stale", "generation": generation,
                      "stale_reason": reason, "invalidated_at": now()}
            db.execute("UPDATE studio_analysis_dependencies SET status='stale',generation=?,record=? "
                       "WHERE project_id=? AND artifact_key=?", (generation, json.dumps(record), project_id, row["artifact_key"]))
            db.execute("INSERT INTO studio_rebuild_requests VALUES (?,?,?,'queued') ON CONFLICT(project_id,artifact_key) "
                       "DO UPDATE SET generation=excluded.generation,status='queued'", (project_id, row["artifact_key"], generation))

    def on_saved(self, record: dict, previous: dict | None, *, db) -> None:
        project_id = record["project_id"]
        kind = "evidence_added" if previous is None else "evidence_reformatted" if semantic(previous) == semantic(record) else "evidence_revised"
        generation = self._generation(db, project_id) if kind == "evidence_reformatted" else self._bump(db, project_id)
        self._change(db, project_id, record["visibility"], kind, {
            "evidence_version_id": record["id"], "previous_version_id": previous["id"] if previous else None,
            "generation": generation, "source_key": record["source_key"],
        })
        if kind == "evidence_revised":
            self._invalidate(db, project_id, record["source_key"], generation, "source_revised")
        elif kind == "evidence_added":
            # A new private material joins the profile input set; it does not
            # automatically invalidate unrelated individual facts/relationships.
            context_kind = "profile" if record["visibility"] == "internal" else "public_profile" if record["source_key"].startswith("public-profile:") else None
            if context_kind:
                rows = db.execute("SELECT artifact_key,record FROM studio_analysis_dependencies WHERE project_id=? AND kind=?",
                                  (project_id, context_kind)).fetchall()
                for row in rows:
                    artifact = {**json.loads(row["record"]), "status": "stale", "generation": generation,
                                "stale_reason": "input_set_expanded", "invalidated_at": now()}
                    db.execute("UPDATE studio_analysis_dependencies SET status='stale',generation=?,record=? "
                               "WHERE project_id=? AND artifact_key=?", (generation, json.dumps(artifact), project_id, row["artifact_key"]))
                    db.execute("INSERT INTO studio_rebuild_requests VALUES (?,?,?,'queued') ON CONFLICT(project_id,artifact_key) "
                               "DO UPDATE SET generation=excluded.generation,status='queued'", (project_id, row["artifact_key"], generation))

    def track(self, project_id: str, artifact_key: str, kind: str, evidence_ids: list[str], *,
              expected_generation: int | None = None, db=None) -> dict:
        if not 1 <= len(artifact_key) <= 200 or kind not in ("fact", "profile", "public_profile", "relationship", "brief", "watch"):
            raise ValueError("invalid artifact dependency")
        if len(evidence_ids) > 500:
            raise ValueError("too many evidence dependencies")

        def write(connection):
            stale = False
            for evidence_id in set(evidence_ids):
                row = connection.execute("SELECT record,source_key FROM studio_evidence_versions WHERE project_id=? AND id=?",
                                         (project_id, evidence_id)).fetchone()
                if row is None:
                    raise KeyError("dependency evidence not found in project")
                latest = connection.execute("SELECT e.record FROM studio_evidence_heads h JOIN studio_evidence_versions e "
                                            "ON e.id=h.evidence_version_id WHERE h.project_id=? AND h.source_key=?",
                                            (project_id, row["source_key"])).fetchone()
                withdrawn = connection.execute("SELECT status FROM studio_evidence_lifecycle WHERE project_id=? AND evidence_version_id=?",
                                               (project_id, evidence_id)).fetchone()
                stale |= bool(withdrawn and withdrawn[0] == "withdrawn") or semantic(json.loads(row["record"])) != semantic(json.loads(latest[0]))
            generation = self._generation(connection, project_id)
            if kind == "brief" and expected_generation is not None:
                stale |= expected_generation != generation
            if expected_generation is not None and expected_generation != generation and kind in ("profile", "public_profile"):
                visibility = "internal" if kind == "profile" else "public"
                expanded = connection.execute("SELECT 1 FROM studio_monitor_changes WHERE project_id=? AND visibility=? "
                    "AND kind='evidence_added' AND json_extract(record,'$.generation')>? "
                    "AND (?='profile' OR json_extract(record,'$.source_key') LIKE 'public-profile:%') LIMIT 1",
                    (project_id, visibility, expected_generation, kind)).fetchone()
                stale |= expanded is not None
            record = {"project_id": project_id, "artifact_key": artifact_key, "kind": kind,
                      "status": "stale" if stale else "current", "generation": generation,
                      "input_generation": expected_generation, "evidence_ids": list(dict.fromkeys(evidence_ids)),
                      "stale_reason": "superseded_input" if stale else "", "updated_at": now()}
            connection.execute("INSERT INTO studio_analysis_dependencies VALUES (?,?,?,?,?,?) "
                               "ON CONFLICT(project_id,artifact_key) DO UPDATE SET kind=excluded.kind,status=excluded.status,"
                               "generation=excluded.generation,record=excluded.record",
                               (project_id, artifact_key, kind, record["status"], generation, json.dumps(record)))
            connection.execute("DELETE FROM studio_dependency_links WHERE project_id=? AND artifact_key=?", (project_id, artifact_key))
            connection.executemany("INSERT INTO studio_dependency_links VALUES (?,?,?)",
                                   [(project_id, artifact_key, evidence_id) for evidence_id in set(evidence_ids)])
            if stale:
                connection.execute("INSERT INTO studio_rebuild_requests VALUES (?,?,?,'queued') ON CONFLICT(project_id,artifact_key) "
                                   "DO UPDATE SET generation=excluded.generation,status='queued'", (project_id, artifact_key, generation))
            else:
                connection.execute("DELETE FROM studio_rebuild_requests WHERE project_id=? AND artifact_key=?", (project_id, artifact_key))
            return record
        if db is not None:
            return write(db)
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return write(connection)

    def get(self, project_id: str, artifact_key: str, *, db=None) -> dict | None:
        def read(connection):
            row = connection.execute("SELECT record FROM studio_analysis_dependencies WHERE project_id=? AND artifact_key=?",
                                     (project_id, artifact_key)).fetchone()
            return json.loads(row[0]) if row else None
        if db is not None:
            return read(db)
        with closing(self.store._connect()) as connection:
            return read(connection)

    def pending(self, project_id: str) -> list[dict]:
        with closing(self.store._connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM studio_rebuild_requests WHERE project_id=? "
                                               "ORDER BY artifact_key LIMIT 100", (project_id,))]

    def withdraw(self, project_id: str, version_id: str, reason: str, *, actor: str) -> None:
        if not reason.strip() or len(reason) > 500 or not actor or len(actor) > 100:
            raise ValueError("withdrawal requires bounded explicit reason and actor")
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT source_key,visibility FROM studio_evidence_versions WHERE project_id=? AND id=?",
                             (project_id, version_id)).fetchone()
            if row is None:
                raise KeyError("evidence not found in project")
            previous = db.execute("SELECT status FROM studio_evidence_lifecycle WHERE project_id=? AND evidence_version_id=?",
                                  (project_id, version_id)).fetchone()
            if previous and previous[0] == "withdrawn":
                return
            db.execute("INSERT OR REPLACE INTO studio_evidence_lifecycle VALUES (?,?,'withdrawn',?,?)",
                       (project_id, version_id, reason, actor))
            generation = self._bump(db, project_id)
            self._invalidate(db, project_id, row["source_key"], generation, "source_withdrawn", version_id=version_id)
            # The free-form reason/actor remain internal; public incremental reads
            # expose only the lifecycle event and evidence identity.
            self._change(db, project_id, row["visibility"], "evidence_withdrawn", {
                "evidence_version_id": version_id, "generation": generation,
            })

    def record_read_failure(self, project_id: str, source_key: str, error_code: str, *, db=None) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
            raise ValueError("read failure requires a sanitized error code")

        def write(connection):
            row = connection.execute("SELECT visibility FROM studio_evidence_versions WHERE project_id=? AND source_key=? "
                                     "ORDER BY seq DESC LIMIT 1", (project_id, source_key)).fetchone()
            self._change(connection, project_id, row[0] if row else "internal", "source_read_failure",
                         {"code": error_code, "source_key": source_key})
        if db is not None:
            write(db)
        else:
            with closing(self.store._connect()) as connection, connection:
                write(connection)

    def changes(self, project_id: str, *, after: int = 0, limit: int = 20, include_internal: bool = True,
                through: int | None = None) -> dict:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid bounded change page")
        if through is not None and (type(through) is not int or through < after):
            raise ValueError("invalid change snapshot watermark")
        with closing(self.store._connect()) as db:
            if through is None:
                through = db.execute("SELECT COALESCE(MAX(seq),0) FROM studio_monitor_changes WHERE project_id=? "
                                     "AND (? OR visibility='public')", (project_id, include_internal)).fetchone()[0]
            rows = db.execute("SELECT * FROM studio_monitor_changes WHERE project_id=? AND seq>? AND seq<=? "
                              "AND (? OR visibility='public') ORDER BY seq LIMIT ?",
                              (project_id, after, through, include_internal, limit + 1)).fetchall()
        return {"project_id": project_id, "schema_version": 1, "snapshot_through": through,
                "items": [{"seq": r["seq"], "kind": r["kind"], "created_at": r["created_at"], **json.loads(r["record"])}
                          for r in rows[:limit]],
                "next_after": rows[limit - 1]["seq"] if len(rows) > limit else None}
