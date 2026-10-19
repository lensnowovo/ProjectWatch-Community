"""Append-only extracted evidence and exact version-bound quotes; no I/O providers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from contextlib import closing

from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart, LocatedRef
from project_watch.studio.store import StudioStore, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_evidence_versions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(id),
    source_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extraction_version TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
    record TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id,source_key,content_hash,extraction_version)
);
CREATE INDEX IF NOT EXISTS studio_evidence_project ON studio_evidence_versions(project_id,seq);
CREATE TABLE IF NOT EXISTS studio_evidence_heads (
    project_id TEXT NOT NULL REFERENCES projects(id), source_key TEXT NOT NULL,
    evidence_version_id TEXT NOT NULL REFERENCES studio_evidence_versions(id),
    PRIMARY KEY(project_id,source_key)
);
CREATE TRIGGER IF NOT EXISTS studio_evidence_immutable_update
BEFORE UPDATE ON studio_evidence_versions BEGIN SELECT RAISE(ABORT,'immutable evidence version'); END;
CREATE TRIGGER IF NOT EXISTS studio_evidence_immutable_delete
BEFORE DELETE ON studio_evidence_versions BEGIN SELECT RAISE(ABORT,'immutable evidence version'); END;
CREATE TABLE IF NOT EXISTS studio_evidence_links (
    project_id TEXT NOT NULL REFERENCES projects(id),
    source_key TEXT NOT NULL,
    evidence_version_id TEXT NOT NULL REFERENCES studio_evidence_versions(id),
    basis TEXT NOT NULL CHECK(basis='donor_text'),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,source_key,evidence_version_id)
);
CREATE TABLE IF NOT EXISTS studio_evidence_cursor (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), secret TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_item_evidence (
    item_id TEXT PRIMARY KEY REFERENCES studio_items(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    evidence_version_id TEXT REFERENCES studio_evidence_versions(id),
    basis TEXT NOT NULL, read_status TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- Background input checks join from an evidence version back to its head/items.
-- The existing primary keys point in the opposite direction; without these
-- indexes the correlated company filter scans all item links for every version.
CREATE INDEX IF NOT EXISTS studio_evidence_head_version ON studio_evidence_heads(evidence_version_id);
CREATE INDEX IF NOT EXISTS studio_item_evidence_version ON studio_item_evidence(project_id,evidence_version_id,item_id);
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EvidenceStore:
    def __init__(self, store: StudioStore):
        self.store = store
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO studio_evidence_heads SELECT e.project_id,e.source_key,e.id "
                       "FROM studio_evidence_versions e WHERE e.seq=(SELECT MAX(v.seq) FROM studio_evidence_versions v "
                       "WHERE v.project_id=e.project_id AND v.source_key=e.source_key)")
            db.execute("INSERT OR IGNORE INTO studio_evidence_cursor VALUES (1,?)", (secrets.token_hex(32),))
            self._cursor_secret = db.execute("SELECT secret FROM studio_evidence_cursor").fetchone()[0].encode()
        from project_watch.studio.dependencies import DependencyStore

        self.dependencies = DependencyStore(store)

    def save(self, evidence: EvidenceInput, *, db=None) -> dict:
        # Hash stored text/coordinates and disclosure/provenance together. A new
        # OCR method or changed metadata never silently mutates an old quote.
        record = evidence.model_dump(mode="json")
        digest = hashlib.sha256(_json({k: v for k, v in record.items()
                                      if k not in ("project_id", "source_key", "extraction_version")}).encode()).hexdigest()
        def write(connection):
            previous = connection.execute("SELECT e.record FROM studio_evidence_heads h JOIN studio_evidence_versions e "
                                          "ON e.id=h.evidence_version_id WHERE h.project_id=? AND h.source_key=?",
                                          (evidence.project_id, evidence.source_key)).fetchone()
            row = connection.execute("SELECT record FROM studio_evidence_versions WHERE project_id=? AND source_key=? "
                             "AND content_hash=? AND extraction_version=?",
                             (evidence.project_id, evidence.source_key, digest, evidence.extraction_version)).fetchone()
            if row:
                stored = json.loads(row[0])
                if previous is None or json.loads(previous[0])["id"] != stored["id"]:
                    self._head(connection, stored)
                    self.dependencies.on_saved(stored, json.loads(previous[0]) if previous else None, db=connection)
                return stored
            record.update(id=str(uuid.uuid4()), content_hash=digest, created_at=now(), schema_version=1)
            connection.execute("INSERT INTO studio_evidence_versions "
                       "(id,project_id,source_key,content_hash,extraction_version,visibility,record,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (record["id"], evidence.project_id, evidence.source_key, digest, evidence.extraction_version,
                        evidence.visibility, _json(record), record["created_at"]))
            self._head(connection, record)
            self.dependencies.on_saved(record, json.loads(previous[0]) if previous else None, db=connection)
            return record
        if db is not None:
            return write(db)
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return write(connection)

    @staticmethod
    def _head(db, record):
        db.execute("INSERT INTO studio_evidence_heads VALUES (?,?,?) ON CONFLICT(project_id,source_key) "
                   "DO UPDATE SET evidence_version_id=excluded.evidence_version_id",
                   (record["project_id"], record["source_key"], record["id"]))

    def get(self, project_id: str, version_id: str, *, include_internal: bool = True, db=None) -> dict:
        def read(connection):
            row = connection.execute("SELECT record FROM studio_evidence_versions WHERE project_id=? AND id=? "
                             "AND (? OR visibility='public')", (project_id, version_id, include_internal)).fetchone()
            if row is None:
                raise KeyError("evidence not found in permitted project scope")
            return json.loads(row[0])
        if db is not None:
            return read(db)
        with closing(self.store._connect()) as connection:
            return read(connection)

    def versions(self, project_id: str, source_key: str, *, include_internal: bool = True) -> list[dict]:
        with closing(self.store._connect()) as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT record FROM studio_evidence_versions WHERE project_id=? AND source_key=? "
                "AND (? OR visibility='public') ORDER BY seq", (project_id, source_key, include_internal))]

    def _encode_cursor(self, data: dict) -> str:
        payload = base64.urlsafe_b64encode(_json(data).encode()).decode().rstrip("=")
        return payload + "." + hmac.new(self._cursor_secret, payload.encode(), hashlib.sha256).hexdigest()

    def _decode_cursor(self, cursor: str, project_id: str, include_internal: bool) -> dict:
        try:
            if len(cursor) > 1000:
                raise ValueError
            payload, signature = cursor.split(".")
            if not hmac.compare_digest(signature, hmac.new(self._cursor_secret, payload.encode(), hashlib.sha256).hexdigest()):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if (data["project"] != project_id or data["internal"] is not include_internal
                    or type(data["after"]) is not int or type(data["through"]) is not int
                    or not 0 <= data["after"] <= data["through"]):
                raise ValueError
            return data
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("invalid evidence snapshot cursor") from exc

    def page(self, project_id: str, *, limit: int = 20, cursor: str | None = None,
             include_internal: bool = True) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("page limit must be between 1 and 100")
        with closing(self.store._connect()) as db:
            if cursor:
                bound = self._decode_cursor(cursor, project_id, include_internal)
            else:
                high = db.execute("SELECT COALESCE(MAX(seq),0) FROM studio_evidence_versions WHERE project_id=? "
                                  "AND (? OR visibility='public')", (project_id, include_internal)).fetchone()[0]
                bound = {"project": project_id, "internal": include_internal, "after": 0, "through": high}
            rows = db.execute("SELECT seq,json_remove(record,'$.parts') AS record,"
                              "json_array_length(record,'$.parts') AS part_count "
                              "FROM studio_evidence_versions WHERE project_id=? "
                              "AND seq>? AND seq<=? AND (? OR visibility='public') ORDER BY seq LIMIT ?",
                              (project_id, bound["after"], bound["through"], include_internal, limit + 1)).fetchall()
        next_cursor = None
        if len(rows) > limit:
            next_cursor = self._encode_cursor({**bound, "after": rows[limit - 1]["seq"]})
        # Bounded metadata list. Fetch one exact part/excerpt instead of returning
        # every private document's entire body in a project listing.
        items = []
        for row in rows[:limit]:
            record = json.loads(row["record"])
            record["part_count"] = row["part_count"]
            items.append(record)
        return {"schema_version": 1, "project_id": project_id, "items": items, "next_cursor": next_cursor}

    def resolve(self, project_id: str, ref: LocatedRef, *, include_internal: bool = True) -> dict:
        record = self.get(project_id, ref.evidence_version_id, include_internal=include_internal)
        if ref.part_index >= len(record["parts"]):
            raise ValueError("part is outside evidence version")
        part = record["parts"][ref.part_index]
        if ref.start < 0 or ref.end <= ref.start or ref.end > len(part["text"]) or part["text"][ref.start:ref.end] != ref.quote:
            raise ValueError("quote does not match exact stored version offsets")
        return {**ref.model_dump(), "project_id": project_id, "locator_kind": part["locator_kind"],
                "locator": part["locator"], "read_scope": record["read_scope"], "origin": record["origin"],
                "visibility": record["visibility"], "schema_version": 1}

    def link_twin(self, project_id: str, source_key: str, donor_version_id: str, *, db=None) -> dict:
        self.get(project_id, donor_version_id, db=db)
        if not 1 <= len(source_key) <= 500:
            raise ValueError("invalid twin source key")
        def write(connection):
            connection.execute("INSERT OR IGNORE INTO studio_evidence_links VALUES (?,?,?,'donor_text',?)",
                       (project_id, source_key, donor_version_id, now()))
        if db is not None:
            write(db)
        else:
            with closing(self.store._connect()) as connection, connection:
                write(connection)
        return {"source_key": source_key, "evidence_version_id": donor_version_id, "basis": "donor_text"}

    def from_fragments(self, repo, project_id: str, source_id: str, *, extraction_version: str,
                       normalize_whitespace: bool = False) -> dict:
        source = repo.get_source(source_id)
        fragments = repo.list_fragments(source_id)
        if source is None or source.project_id != project_id or any(f.project_id != project_id for f in fragments):
            raise KeyError("source not found in project")
        parts = []
        for fragment in fragments:
            locator = fragment.locator
            kind = "fragment"
            # These coordinates originate in the parser; no inferred page number.
            if re.fullmatch(r"page \d+( OCR)?( part \d+)?", locator):
                kind = "pdf_page"
            elif re.fullmatch(r"slide \d+( part \d+)?", locator):
                kind = "slide"
            text = " ".join(fragment.text.split()) if normalize_whitespace else fragment.text
            if text:
                parts.append(EvidencePart(locator_kind=kind, locator=locator, text=text))
        return self.save(EvidenceInput(project_id=project_id, source_key="internal:" + source_id,
                                      internal_source_id=source_id, raw_content_hash=source.content_sha256,
                                      extraction_version=extraction_version, read_scope="fragmented", parts=parts))
