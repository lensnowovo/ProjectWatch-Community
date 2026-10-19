"""Shared bounded intake. Agent-internal submissions are parse-only by default."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import closing

from project_watch.studio.application import AccessDenied, ProjectMissing, VersionConflict
from project_watch.studio.store import now


def model_binding(model) -> str:
    if not model.ready:
        return ""
    # No key or endpoint is exported. Permission is for this provider/protocol/model,
    # not an API credential: key rotation does not broaden the destination.
    return hashlib.sha256(json.dumps([model.protocol, model.base_url.rstrip("/"), model.model]).encode()).hexdigest()


class ModelView:
    """Model input projection only; ordinary owner reads retain historical reports."""
    def __init__(self, store, binding):
        self.store, self.binding = store, binding

    def __getattr__(self, name):
        return getattr(self.store, name)

    def get_profile(self, project_id):
        profile = self.store.get_profile(project_id)
        def source_ids(value):
            if isinstance(value, dict):
                if value.get("source_id"):
                    yield value["source_id"]
                for child in value.values():
                    yield from source_ids(child)
            elif isinstance(value, list):
                for child in value:
                    yield from source_ids(child)
        if any(not self.store.source_analysis_allowed(project_id, sid, self.binding) for sid in set(source_ids(profile))):
            return None
        return profile

    def list_metrics(self, project_id):
        return [r for r in self.store.list_metrics(project_id)
                if self.store.source_analysis_allowed(project_id, r["source_id"], self.binding)]

    def get_plan(self, project_id):
        # Plans may contain concepts derived from internal documents. Without a
        # cited plan-level input map, conservatively regenerate from public context.
        analyzed = self.store.analyzed_source_ids(project_id)
        if any(not self.store.source_analysis_allowed(project_id, sid, self.binding) for sid in analyzed):
            return None
        return self.store.get_plan(project_id)


class MaterialService:
    def __init__(self, service, repository, blob_store, settings_store):
        if repository.db_path.resolve() != service.store.db_path.resolve():
            raise ValueError("intake must share the loaded Studio database")
        self.service, self.repository = service, repository
        self.blob_store, self.settings_store = blob_store, settings_store

    def _begin(self, db, project_id, expected_version, key, token, payload):
        if not 1 <= len(key) <= 100 or (expected_version is not None and (type(expected_version) is not int or expected_version < 1)):
            raise ValueError("invalid version or idempotency key")
        grant = self.service._authorize(db, token, project_id, "write") if token else None
        if grant and not grant["allow_internal"]:
            raise AccessDenied("material submission requires an explicit internal write grant")
        if grant and expected_version is None:
            raise ValueError("Agent submission requires expected_version")
        actor = grant["id"] if grant else "local_web"
        previous = db.execute("SELECT payload_hash,receipt FROM studio_operations WHERE instance_id=? AND project_id=? "
                              "AND actor=? AND idempotency_key=?", (self.service.instance_id, project_id, actor, key)).fetchone()
        if previous:
            if previous[0] != payload:
                raise VersionConflict("idempotency key already used with another payload")
            return actor, json.loads(previous[1]), None
        project = db.execute("SELECT config_version,consent_at FROM studio_projects WHERE project_id=?", (project_id,)).fetchone()
        if project is None:
            raise ProjectMissing("Studio project not found")
        if expected_version is not None and project[0] != expected_version:
            raise VersionConflict("project configuration has changed; read latest version")
        return actor, None, project

    def _receipt(self, db, project_id, actor, key, payload, version, **extra):
        db.execute("UPDATE studio_projects SET config_version=config_version+1 WHERE project_id=?", (project_id,))
        receipt = {"schema_version": 1, "instance_id": self.service.instance_id, "project_id": project_id,
                   "operation_id": str(uuid.uuid4()), "saved": True, "operation_status": "completed",
                   "version": version + 1, "actor": actor, **extra}
        db.execute("INSERT INTO studio_operations VALUES (?,?,?,?,?,?,?,?)",
                   (receipt["operation_id"], project_id, self.service.instance_id, actor, key, payload, json.dumps(receipt), now()))
        return receipt

    def submit(self, project_id, filename, stream, *, expected_version=None, idempotency_key=None, token=None):
        # Check scope before any blob write; recheck inside the write transaction.
        self.service.project(project_id)
        if token and not self.service.authorize(token, project_id, "write")["allow_internal"]:
            raise AccessDenied("material submission requires an explicit internal write grant")
        stored = self.blob_store.put_stream(filename, stream)
        key = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
        payload = hashlib.sha256(json.dumps(["submit_document", stored.sha256, stored.display_name, expected_version]).encode()).hexdigest()
        with closing(self.service.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            actor, previous, project = self._begin(db, project_id, expected_version, key, token, payload)
            if previous:
                return previous
            existing = db.execute("SELECT id FROM sources WHERE project_id=? AND content_sha256=?", (project_id, stored.sha256)).fetchone()
            source = self.repository.add_source(project_id, kind="document", reference=stored.display_name, content_sha256=stored.sha256, db=db)
            job = self.repository.enqueue_intake(project_id, source.id, db=db)
            if token and existing is None:
                db.execute("INSERT INTO studio_source_permissions VALUES (?,?,?,?,?)", (source.id, project_id, "", actor, now()))
            # Re-upload by an Agent cannot revoke/expand existing owner consent.
            if not token and project[1]:
                binding = model_binding(self.settings_store.load().model)
                if binding:
                    db.execute("UPDATE studio_source_permissions SET model_binding=?,actor=?,updated_at=? WHERE source_id=? AND project_id=?",
                               (binding, actor, now(), source.id, project_id))
            permission = db.execute("SELECT model_binding FROM studio_source_permissions WHERE source_id=?", (source.id,)).fetchone()
            authorized = permission is None or bool(permission[0] and permission[0] == model_binding(self.settings_store.load().model))
            return self._receipt(db, project_id, actor, key, payload, project[0], source_id=source.id, name=source.reference,
                                 status=job.status, job_id=job.id, job_status=job.status,
                                 analysis_status="eligible_when_project_authorized" if authorized else "owner_authorization_required")

    def authorize_analysis(self, project_id, source_id, expected_version, idempotency_key):
        binding = model_binding(self.settings_store.load().model)
        if not binding:
            raise ValueError("configure a model before authorizing this material")
        payload = hashlib.sha256(json.dumps(["authorize_analysis", source_id, binding, expected_version]).encode()).hexdigest()
        with closing(self.service.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            actor, previous, project = self._begin(db, project_id, expected_version, idempotency_key, None, payload)
            if previous:
                return previous
            if not project[1]:
                raise AccessDenied("project model consent is required")
            if not db.execute("SELECT 1 FROM sources WHERE id=? AND project_id=? AND kind='document'", (source_id, project_id)).fetchone():
                raise ProjectMissing("source not found in project")
            db.execute("INSERT INTO studio_source_permissions VALUES (?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                       "model_binding=excluded.model_binding,actor=excluded.actor,updated_at=excluded.updated_at",
                       (source_id, project_id, binding, actor, now()))
            return self._receipt(db, project_id, actor, idempotency_key, payload, project[0], source_id=source_id,
                                 job_id=None, job_status="not_requested", analysis_status="authorized_for_current_model")

    def status(self, project_id, source_id):
        self.service.project(project_id)
        with closing(self.service.store._connect()) as db:
            source = db.execute("SELECT 1 FROM sources WHERE id=? AND project_id=?", (source_id, project_id)).fetchone()
            if source is None:
                raise ProjectMissing("source not found in project")
            job = db.execute("SELECT id,status FROM intake_jobs WHERE source_id=? AND project_id=?", (source_id, project_id)).fetchone()
        allowed = self.service.store.source_analysis_allowed(project_id, source_id, model_binding(self.settings_store.load().model))
        return {"instance_id": self.service.instance_id, "schema_version": 1, "project_id": project_id, "source_id": source_id,
                "job_id": job[0] if job else None, "parser_status": job[1] if job else "not_requested",
                "analysis_status": "eligible_when_project_authorized" if allowed else "owner_authorization_required"}
