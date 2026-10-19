"""Studio application boundary. No second repository/worker and no network I/O.

Construct once during app startup using the already loaded StudioStore. In
particular an Agent must not construct StudioStore against a live database:
adapters use the existing instance's HTTP boundary, never a second worker/store.
"""

from __future__ import annotations

import hashlib
import json
import platform
import secrets
import time
import uuid
from contextlib import closing

from project_watch.studio.store import StudioStore, now

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_instance (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    instance_id TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_agent_grants (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    permission TEXT NOT NULL CHECK(permission IN ('read','write')),
    created_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS studio_operations (
    operation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    instance_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    receipt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(instance_id,project_id,actor,idempotency_key)
);
"""


class AccessDenied(ValueError):
    """Expired/revoked credentials, insufficient scope or wrong runtime."""


class VersionConflict(ValueError):
    """Optimistic version or idempotency payload mismatch."""


class ProjectMissing(KeyError):
    """Project is not a Studio project in this instance."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def machine_identity() -> str:
    """Used only inside a hash, never exported as an Agent-readable identifier."""
    if platform.system() == "Windows":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except (OSError, ImportError):
            pass
    else:
        from pathlib import Path

        try:
            return Path("/etc/machine-id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            pass
    return platform.node() + ":" + str(uuid.getnode())


class StudioApplication:
    def __init__(self, store: StudioStore) -> None:
        self.store = store
        # Only a fingerprint is persisted; never disclose machine paths. A moved
        # or copied DB gets a new ID and its inherited grants are revoked.
        binding = _hash(machine_identity() + ":" + str(store.db_path.resolve()).casefold())
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)
            columns = {r[1] for r in db.execute("PRAGMA table_info(studio_agent_grants)")}
            if "allow_internal" not in columns:
                db.execute("ALTER TABLE studio_agent_grants ADD COLUMN allow_internal INTEGER NOT NULL DEFAULT 0")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM studio_instance WHERE singleton=1").fetchone()
            if row is None or row["binding_hash"] != binding:
                self.instance_id = str(uuid.uuid4())
                db.execute("UPDATE studio_agent_grants SET revoked_at=? WHERE revoked_at IS NULL", (now(),))
                db.execute(
                    "INSERT OR REPLACE INTO studio_instance VALUES (1,?,?,?)",
                    (self.instance_id, binding, now()),
                )
            else:
                self.instance_id = row["instance_id"]
        self.evidence = store.evidence
        from project_watch.studio.objects import ObjectStore

        self.objects = ObjectStore(store, self.evidence)
        from project_watch.studio.relationships import RelationshipStore

        self.relationships = RelationshipStore(store, self.objects)
        from project_watch.studio.health import HealthStore

        self.health = HealthStore(store)
        from project_watch.studio.concerns import ConcernStore

        self.concerns = ConcernStore(self)
        from project_watch.studio.runtime_manifest import snapshot

        self._runtime_snapshot = {**snapshot(), 'captured_at': now()}

    def manifest(self) -> dict:
        return {
            "instance_id": self.instance_id, "schema_version": SCHEMA_VERSION,
            "mode": "studio_local", "capabilities": ["project.read", "monitoring.status.write", "operation.read",
                                                       "evidence.read", "excerpt.resolve", "objects.read", "objects.write",
                                                       "changes.read", "relationships.read", "relationships.correct", "health.read", "materials.package.read", "observations.read", "event_states.read", "milestones.read", "concerns.read", "concerns.feedback", "questions.read", "questions.assess"]
                                                       + (["sources.submit", "sources.read"] if hasattr(self, "materials") else []),
            "network_on_read": False, "runtime": dict(self._runtime_snapshot),
        }

    def check_instance(self, instance_id: str) -> None:
        if instance_id != self.instance_id:
            raise AccessDenied("instance mismatch")

    def material_package(self, project_id: str, *, include_internal: bool = False) -> dict:
        from project_watch.studio.material_package import compile_package

        return compile_package(self, project_id, include_internal=include_internal)

    def project(self, project_id: str) -> dict:
        project = self.store.get_project(project_id)
        if not project or project.get("status") is None:
            raise ProjectMissing("Studio project not found")
        # Deliberately omit internal notes/profiles, paths, provider config and
        # raw job errors. Further evidence tools need their own disclosure scope.
        result = {key: project[key] for key in (
            "id", "name", "legal_name", "short_name", "industry", "website",
            "status", "activated", "config_version",
        )}
        return {**result, "instance_id": self.instance_id, "schema_version": SCHEMA_VERSION}

    def search(self, query: str, allowed_projects: list[str]) -> dict:
        if len(query) > 200 or len(allowed_projects) > 50:
            raise ValueError("search exceeds bounded scope")
        items = []
        for project_id in dict.fromkeys(allowed_projects):
            project = self.project(project_id)
            if query.casefold() in (project["name"] + " " + project["legal_name"]).casefold():
                items.append(project)
        return {"instance_id": self.instance_id, "schema_version": SCHEMA_VERSION,
                "items": items, "next_cursor": None, "truncated": False}

    def issue_grant(self, project_id: str, permission: str, label: str, *, hours: int = 24,
                    allow_internal: bool = False) -> dict:
        self.project(project_id)
        if (permission not in ("read", "write") or not 1 <= hours <= 720 or not 1 <= len(label) <= 100
                or type(allow_internal) is not bool):
            raise ValueError("invalid project grant")
        token = "pwa_" + secrets.token_urlsafe(32)
        grant_id = str(uuid.uuid4())
        expires = int(time.time()) + hours * 3600
        with closing(self.store._connect()) as db, db:
            db.execute("INSERT INTO studio_agent_grants "
                       "(id,instance_id,project_id,token_hash,label,permission,created_at,expires_at,allow_internal) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (grant_id, self.instance_id, project_id, _hash(token), label, permission, now(), expires,
                        int(allow_internal)))
        return {"id": grant_id, "instance_id": self.instance_id, "project_id": project_id,
                "permission": permission, "expires_at": expires, "allow_internal": allow_internal, "token": token}

    def _authorize(self, db, token: str, project_id: str, permission: str) -> dict:
        if not token.startswith("pwa_") or len(token) > 200:
            raise AccessDenied("invalid project grant")
        row = db.execute("SELECT * FROM studio_agent_grants WHERE token_hash=?", (_hash(token),)).fetchone()
        if (not row or row["instance_id"] != self.instance_id or row["project_id"] != project_id
                or row["revoked_at"] or row["expires_at"] <= int(time.time())
                or (permission == "write" and row["permission"] != "write")):
            raise AccessDenied("project grant expired, revoked or outside scope")
        return {**{key: row[key] for key in ("id", "project_id", "permission", "label", "expires_at")},
                "allow_internal": bool(row["allow_internal"])}

    def authorize(self, token: str, project_id: str, permission: str) -> dict:
        with closing(self.store._connect()) as db:
            return self._authorize(db, token, project_id, permission)

    def list_grants(self, project_id: str) -> list[dict]:
        self.project(project_id)
        with closing(self.store._connect()) as db:
            return [dict(r) for r in db.execute(
                "SELECT id,project_id,label,permission,expires_at,revoked_at,allow_internal FROM studio_agent_grants "
                "WHERE project_id=? AND instance_id=? ORDER BY created_at,id", (project_id, self.instance_id))]

    def revoke_grant(self, project_id: str, grant_id: str) -> None:
        with closing(self.store._connect()) as db, db:
            if not db.execute("UPDATE studio_agent_grants SET revoked_at=COALESCE(revoked_at,?) "
                              "WHERE id=? AND project_id=? AND instance_id=?",
                              (now(), grant_id, project_id, self.instance_id)).rowcount:
                raise ProjectMissing("grant not found in project")

    def change_status(self, project_id: str, status: str, *, expected_version: int | None,
                      idempotency_key: str, actor: str, token: str | None = None) -> dict:
        if status not in ("active", "paused") or not 1 <= len(idempotency_key) <= 100:
            raise ValueError("invalid status change")
        if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
            raise ValueError("expected_version must be a positive integer")
        payload = _hash(json.dumps({"status": status, "expected_version": expected_version}, sort_keys=True))
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            # Recheck write scope/revocation within the write transaction. Actor
            # comes from the validated grant, never from an Agent's request body.
            if token is not None:
                if expected_version is None:
                    raise ValueError("Agent writes require expected_version")
                actor = self._authorize(db, token, project_id, "write")["id"]
            previous = db.execute("SELECT * FROM studio_operations WHERE instance_id=? AND project_id=? "
                                  "AND actor=? AND idempotency_key=?",
                                  (self.instance_id, project_id, actor, idempotency_key)).fetchone()
            if previous:
                if previous["payload_hash"] != payload:
                    raise VersionConflict("idempotency key already used with another payload")
                return json.loads(previous["receipt"])
            row = db.execute("SELECT status,config_version FROM studio_projects WHERE project_id=?",
                             (project_id,)).fetchone()
            if row is None:
                raise ProjectMissing("Studio project not found")
            if expected_version is not None and row["config_version"] != expected_version:
                raise VersionConflict("project configuration has changed; read latest version")
            db.execute("UPDATE studio_projects SET status=?,updated_at=? WHERE project_id=?",
                       (status, now(), project_id))
            version = db.execute("SELECT config_version FROM studio_projects WHERE project_id=?",
                                 (project_id,)).fetchone()[0]
            running = [r[0] for r in db.execute("SELECT id FROM studio_jobs WHERE project_id=? "
                                               "AND status='running' ORDER BY id", (project_id,))]
            receipt = {
                "operation_id": str(uuid.uuid4()), "instance_id": self.instance_id,
                "project_id": project_id, "schema_version": SCHEMA_VERSION,
                "saved": True, "operation_status": "completed", "version": version,
                "status": status, "job_status": "not_requested", "job_id": None,
                "in_flight_job_ids": running, "actor": actor,
            }
            db.execute("INSERT INTO studio_operations VALUES (?,?,?,?,?,?,?,?)",
                       (receipt["operation_id"], project_id, self.instance_id, actor,
                        idempotency_key, payload, json.dumps(receipt), now()))
            return receipt

    def receipt(self, project_id: str, operation_id: str) -> dict:
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT receipt FROM studio_operations WHERE project_id=? AND operation_id=? "
                             "AND instance_id=?", (project_id, operation_id, self.instance_id)).fetchone()
        if row is None:
            raise ProjectMissing("operation not found in project")
        return json.loads(row[0])

    def maintain_object(self, project_id: str, body, *, token: str | None = None) -> dict:
        payload = _hash(body.model_dump_json())
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            grant = self._authorize(db, token, project_id, "write") if token else None
            actor = grant["id"] if grant else "local_web"
            internal = grant["allow_internal"] if grant else True
            if (body.object.visibility == "internal" or any(a.visibility == "internal" for a in body.object.aliases)) and not internal:
                raise AccessDenied("internal object maintenance requires explicit internal grant")
            if body.object_id:
                self.objects.get(project_id, body.object_id, include_internal=internal, db=db)
            for ref in body.object.refs:
                self.evidence.resolve(project_id, ref, include_internal=internal)
            old = db.execute("SELECT payload_hash,receipt FROM studio_operations WHERE instance_id=? "
                             "AND project_id=? AND actor=? AND idempotency_key=?",
                             (self.instance_id, project_id, actor, body.idempotency_key)).fetchone()
            if old:
                if old["payload_hash"] != payload:
                    raise VersionConflict("idempotency key already used with another payload")
                return json.loads(old["receipt"])
            project = db.execute("SELECT config_version FROM studio_projects WHERE project_id=?", (project_id,)).fetchone()
            if project is None:
                raise ProjectMissing("Studio project not found")
            if project[0] != body.expected_project_version:
                raise VersionConflict("project configuration has changed; read latest version")
            result = self.objects.save(project_id, body.object, actor="user", object_id=body.object_id,
                                       expected_version=body.expected_object_version, db=db)
            db.execute("UPDATE studio_projects SET config_version=config_version+1 WHERE project_id=?", (project_id,))
            receipt = {"operation_id": str(uuid.uuid4()), "instance_id": self.instance_id, "schema_version": 1,
                       "project_id": project_id, "saved": True, "operation_status": "completed",
                       "version": project[0] + 1, "object_id": result["id"], "object_version": result["version"],
                       "job_status": "not_requested", "job_id": None, "actor": actor}
            db.execute("INSERT INTO studio_operations VALUES (?,?,?,?,?,?,?,?)",
                       (receipt["operation_id"], project_id, self.instance_id, actor, body.idempotency_key, payload,
                        json.dumps(receipt), now()))
            return receipt
