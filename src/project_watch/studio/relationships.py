"""Relationship projections and durable scoped user overrides on shared objects.

No network calls or automatic research scheduling here. Candidates never gain
supported status because a user selected another company or a name was mentioned.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import closing, nullcontext
from datetime import date
from types import SimpleNamespace

from project_watch.studio.application import AccessDenied, ProjectMissing, VersionConflict
from project_watch.studio.business_identity import has_current_product_ownership
from project_watch.studio.monitoring_contracts import LocatedRef, ObjectInput
from project_watch.studio.peer_names import clean_plan, company_name
from project_watch.studio.provenance import effective_family
from project_watch.studio.relationship_contracts import (
    PeerSelection,
    RelationshipDecision,
    RelationshipScope,
)
from project_watch.studio.store import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_relationship_candidates (
    project_id TEXT NOT NULL REFERENCES projects(id), scope_key TEXT NOT NULL,
    version INTEGER NOT NULL, visibility TEXT NOT NULL, record TEXT NOT NULL,
    PRIMARY KEY(project_id,scope_key)
);
CREATE TABLE IF NOT EXISTS studio_relationship_overrides (
    project_id TEXT NOT NULL REFERENCES projects(id), scope_key TEXT NOT NULL,
    version INTEGER NOT NULL, visibility TEXT NOT NULL, record TEXT NOT NULL,
    PRIMARY KEY(project_id,scope_key)
);
CREATE TABLE IF NOT EXISTS studio_relationship_history (
    project_id TEXT NOT NULL REFERENCES projects(id), scope_key TEXT NOT NULL,
    origin TEXT NOT NULL, version INTEGER NOT NULL, visibility TEXT NOT NULL,
    record TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,scope_key,origin,version)
);
CREATE TRIGGER IF NOT EXISTS studio_relationship_history_immutable_update BEFORE UPDATE ON studio_relationship_history
BEGIN SELECT RAISE(ABORT,'immutable relationship history'); END;
CREATE TRIGGER IF NOT EXISTS studio_relationship_history_immutable_delete BEFORE DELETE ON studio_relationship_history
BEGIN SELECT RAISE(ABORT,'immutable relationship history'); END;
"""


def scope_fields(record):
    return {k: record.get(k) for k in RelationshipScope.model_fields}


def scope_key(scope):
    return hashlib.sha256(json.dumps(scope_fields(scope), sort_keys=True).encode()).hexdigest()


class RelationshipStore:
    def __init__(self, store, objects, *, assessment_validator=None):
        self.store, self.objects = store, objects
        self.assessment_validator = assessment_validator
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)

    def validate_scope(self, project_id, scope, *, include_internal=True, db=None):
        scope = RelationshipScope.model_validate(scope_fields(scope)).model_dump()
        if scope["subject_id"] == scope["peer_id"]:
            raise ValueError("a company cannot be its own competitor")
        records = {}
        for key in ("subject_id", "peer_id", "subject_product_id", "peer_product_id"):
            if scope[key]:
                records[key] = self.objects.get(project_id, scope[key], include_internal=include_internal, db=db)
        if any(records[key]["kind"] != "company" for key in ("subject_id", "peer_id")):
            raise ValueError("relationship subjects must be companies")
        for product, company in (("subject_product_id", "subject_id"), ("peer_product_id", "peer_id")):
            if product in records and (records[product]["kind"] != "product" or records[product]["parent_id"] != scope[company]):
                raise ValueError("product scope must belong to its exact company")
        return ("internal" if any(r["visibility"] == "internal" for r in records.values()) else "public",
                {r["id"]: r["version"] for r in records.values()})

    def save_candidate(self, project_id, scope, *, relation_type="unknown", rationale=""):
        if relation_type not in ("direct", "substitute", "adjacent", "unknown", "unrelated") or len(rationale) > 1000:
            raise ValueError("invalid relationship candidate")
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            visibility, versions = self.validate_scope(project_id, scope, db=db)
            key = scope_key(scope)
            old = db.execute("SELECT version FROM studio_relationship_candidates WHERE project_id=? AND scope_key=?", (project_id, key)).fetchone()
            version = old[0] + 1 if old else 1
            record = {**scope_fields(scope), "scope_key": key, "project_id": project_id, "schema_version": 1,
                      "origin": "auto_research", "support": "pending", "relation_type": relation_type,
                      "rationale": rationale, "missing": ["bilateral_business_and_buyer_evidence"],
                      "visibility": visibility, "object_versions": versions, "version": version, "created_at": now()}
            db.execute("INSERT INTO studio_relationship_candidates VALUES (?,?,?,?,?) ON CONFLICT(project_id,scope_key) "
                       "DO UPDATE SET version=excluded.version,visibility=excluded.visibility,record=excluded.record",
                       (project_id, key, version, visibility, json.dumps(record)))
            self._history(db, record)
            self.objects.evidence.dependencies.track(project_id, "relationship:" + key, "relationship", [], db=db)
            self.objects.evidence.dependencies.track_objects(project_id, "relationship:" + key, versions, db=db)
            return record

    def save_assessment(self, project_id, assessment, *, guard=None):
        """Revalidate stored fact references and reject results from old inputs.

        This internal service does not accept an arbitrary supported flag as
        proof. The provider review is retained as an assertion, not verification.
        Manual overrides remain separate and take precedence in the projection.
        """
        if self.assessment_validator is None:
            raise ValueError('insight_unavailable: bilateral assessment requires an installed validator')

        if assessment.get("project_id") != project_id:
            raise ValueError("assessment belongs to another project")
        if self.objects.evidence.dependencies.generation(project_id) != assessment.get("input_generation"):
            raise VersionConflict("assessment inputs changed; reassess current facts")
        service = SimpleNamespace(relationships=self, objects=self.objects, evidence=self.objects.evidence)
        review = (lambda _context: assessment["review"]) if assessment.get("review") else None
        checked = self.assessment_validator(service, project_id, assessment, assessment["relation_type"], review=review)
        if any(assessment.get(k) != checked[k] for k in ("support", "object_versions", "fact_ids", "evidence_ids", "visibility")):
            raise ValueError("assessment does not match current bilateral evidence gates")
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if guard:
                guard(db)
            if self.objects.evidence.dependencies._generation(db, project_id) != checked["input_generation"]:
                raise VersionConflict("assessment inputs changed before save")
            key = scope_key(checked)
            old = db.execute("SELECT version FROM studio_relationship_candidates WHERE project_id=? AND scope_key=?", (project_id, key)).fetchone()
            record = {**checked, "scope_key": key, "origin": "auto_research", "version": old[0] + 1 if old else 1,
                      "rationale": checked["review"]["reason"] if checked["review"] else "",
                      "created_at": now(), "assessment_version": "bilateral-v2"}
            db.execute("INSERT INTO studio_relationship_candidates VALUES (?,?,?,?,?) ON CONFLICT(project_id,scope_key) "
                       "DO UPDATE SET version=excluded.version,visibility=excluded.visibility,record=excluded.record",
                       (project_id, key, record["version"], record["visibility"], json.dumps(record)))
            self._history(db, record)
            self.objects.evidence.dependencies.track(project_id, "relationship:" + key, "relationship", checked["evidence_ids"], db=db)
            self.objects.evidence.dependencies.track_objects(project_id, "relationship:" + key, checked["object_versions"], db=db)
            return record

    def _history(self, db, record):
        db.execute("INSERT INTO studio_relationship_history VALUES (?,?,?,?,?,?,?)",
                   (record["project_id"], record["scope_key"], record["origin"], record["version"],
                    record["visibility"], json.dumps(record), now()))
        if record['origin'] == 'auto_research':
            # A changed automatic relationship also changes a judgment's
            # interpretation context. Existing rebuild rows coalesce the work.
            self.objects.evidence.dependencies.on_relationship_corrected(record['project_id'], db=db)

    def _rows(self, table, project_id, include_internal=True, *, db=None):
        # Table is internal and fixed by the methods below, never a request parameter.
        with nullcontext(db) if db is not None else closing(self.store._connect()) as connection:
            return [json.loads(r[0]) for r in connection.execute(f"SELECT record FROM {table} WHERE project_id=? "
                "AND (? OR visibility='public') ORDER BY scope_key LIMIT 500", (project_id, include_internal))]

    def overrides(self, project_id, *, include_internal=True, db=None):
        rows = self._rows("studio_relationship_overrides", project_id, include_internal, db=db)
        if include_internal:
            return rows
        permitted = []
        for row in rows:
            try:
                self.validate_scope(project_id, row, include_internal=False, db=db)
                permitted.append(row)
            except (KeyError, ValueError):
                continue
        return permitted

    def history(self, project_id):
        with closing(self.store._connect()) as db:
            return [json.loads(r[0]) for r in db.execute("SELECT record FROM studio_relationship_history WHERE project_id=? "
                                                       "ORDER BY created_at,scope_key,version LIMIT 500", (project_id,))]

    def _basis_outside_validity(self, project_id, row, include_internal, *, db=None):
        # Read-time date checks must not overwrite history or enqueue paid work.
        ids = list(dict.fromkeys(row.get('fact_ids', [])))
        if not ids:
            return False
        if len(ids) > 100:
            return True
        marks = ','.join('?' for _ in ids)
        with nullcontext(db) if db is not None else closing(self.store._connect()) as connection:
            if db is None:
                connection.execute('BEGIN')
            facts = [json.loads(r[0]) for r in connection.execute(
                f"SELECT record FROM studio_fact_records WHERE project_id=? AND id IN ({marks}) "
                "AND (? OR visibility='public')", (project_id, *ids, include_internal))]
            today = date.today().isoformat()
            if len(facts) != len(ids) or any(
                (f.get('valid_from') and f['valid_from'] > today)
                or (f.get('valid_to') and f['valid_to'] < today) for f in facts):
                return True
            if any(not f.get('valid_from') and not f.get('valid_to') for f in facts):
                # Pre-v4 facts are immutable and may have omitted alias dates.
                # Reuse current scoped ownership, never guess/backfill dates.
                for product_key, company_key in (('subject_product_id', 'subject_id'),
                                                  ('peer_product_id', 'peer_id')):
                    if not row.get(product_key):
                        return True
                    try:
                        product = self.objects.get(project_id, row[product_key],
                                                   include_internal=include_internal, db=connection)
                        company = self.objects.get(project_id, row[company_key],
                                                   include_internal=include_internal, db=connection)
                    except KeyError:
                        return True
                    if not has_current_product_ownership(self.store, project_id, company, product,
                                                         include_internal=include_internal, db=connection):
                        return True
        return False

    def _shared_family(self, project_id, row, include_internal, *, db=None):
        ids = list(dict.fromkeys(row.get('fact_ids', [])))[:100]
        if not ids:
            return False
        with nullcontext(db) if db is not None else closing(self.store._connect()) as connection:
            if db is None:
                connection.execute('BEGIN')
            marks = ','.join('?' for _ in ids)
            facts = [json.loads(r[0]) for r in connection.execute(
                f"SELECT record FROM studio_fact_records WHERE project_id=? AND id IN ({marks}) "
                "AND (? OR visibility='public')", (project_id, *ids, include_internal))]
            labels, sides = {}, [set(), set()]
            for index, product_key in enumerate(('subject_product_id', 'peer_product_id')):
                for fact in facts:
                    if fact['subject_id'] != row.get(product_key):
                        continue
                    for ref in fact['refs']:
                        eid = ref['evidence_version_id']
                        if eid not in labels:
                            try:
                                item = self.objects.evidence.get(project_id, eid,
                                    include_internal=include_internal, db=connection)
                            except KeyError:
                                continue
                            labels[eid] = effective_family(item)['propagation_family']
                        if labels[eid]:
                            sides[index].add(labels[eid])
            return bool(sides[0] & sides[1])

    def page(self, project_id, *, include_internal=True, db=None):
        candidates = {r["scope_key"]: r for r in self._rows("studio_relationship_candidates", project_id, include_internal, db=db)}
        overrides = {r["scope_key"]: r for r in self.overrides(project_id, include_internal=include_internal, db=db)}
        # A reset removes user-selected status, not the AI safety gate.
        for key, row in overrides.items():
            if row["decision"] == "include":
                candidates[key] = row
            elif row["decision"] == "exclude":
                candidates.pop(key, None)
        result = []
        for row in candidates.values():
            company_key = scope_key({**scope_fields(row), "subject_product_id": None, "peer_product_id": None})
            if overrides.get(company_key, {}).get("decision") == "exclude":
                continue
            try:
                _, current = self.validate_scope(project_id, row, include_internal=include_internal, db=db)
            except (KeyError, ValueError):
                continue
            stale = current != row["object_versions"]
            dependency = self.objects.evidence.dependencies.get(project_id, "relationship:" + row["scope_key"], db=db) if row["origin"] == "auto_research" else None
            status = dependency["status"] if dependency else "current"
            outside_validity = row['origin'] == 'auto_research' and row['support'] == 'supported' and self._basis_outside_validity(project_id, row, include_internal, db=db)
            shared_family = row['origin'] == 'auto_research' and row['support'] == 'supported' and self._shared_family(project_id, row, include_internal, db=db)
            if outside_validity or shared_family:
                status = 'stale'
            projected = {**row, "status": status, "scope_identity_changed": stale}
            if outside_validity:
                projected['validity_reason'] = 'basis_outside_validity'
            elif shared_family:
                projected['validity_reason'] = 'shared_propagation_family'
            if row["origin"] == "auto_research" and status == "stale":
                projected.update(support="pending", previous_support=row["support"])
            result.append(projected)
        return result

    def view(self, project_id, *, include_internal=True):
        def labels(row):
            result = dict(row)
            for key in RelationshipScope.model_fields:
                identity = row.get(key)
                result[key.removesuffix("_id") + "_name"] = self.objects.get(project_id, identity, include_internal=include_internal)["name"] if identity else "公司整体"
            result["company_wide"] = not row.get("subject_product_id") and not row.get("peer_product_id")
            peer = self.objects.get(project_id, row["peer_id"], include_internal=include_internal)
            result["peer_names"] = [peer["name"], *(a["name"] for a in peer["aliases"])]
            fact_ids = list(dict.fromkeys(row.get('fact_ids', [])))[:100]
            result['basis_facts'] = []
            if fact_ids:
                marks = ','.join('?' for _ in fact_ids)
                with closing(self.store._connect()) as db:
                    facts = db.execute(f"SELECT record FROM studio_fact_records WHERE project_id=? AND id IN ({marks}) "
                                       "AND (? OR visibility='public') ORDER BY id LIMIT 21", (project_id, *fact_ids, include_internal)).fetchall()
                result['basis_facts_truncated'] = len(facts) > 20 or len(row.get('fact_ids', [])) > 100
                for record in facts[:20]:
                    fact = json.loads(record[0])
                    if fact['subject_id'] not in (row.get('subject_product_id'), row.get('peer_product_id')):
                        continue
                    refs = []
                    for ref in fact['refs']:
                        try:
                            self.objects.evidence.resolve(project_id, LocatedRef.model_validate(ref), include_internal=include_internal)
                        except (KeyError, ValueError):
                            continue
                        refs.append({k: v for k, v in ref.items() if k != 'quote'})
                    if refs:
                        result['basis_facts'].append({'fact_id': fact['id'], 'subject_id': fact['subject_id'],
                            'predicate': fact['predicate'], 'value': fact['value'], 'basis': fact['basis'], 'refs': refs})
            return result
        items = [labels(r) for r in self.page(project_id, include_internal=include_internal)]
        exclusions = []
        for row in self.overrides(project_id, include_internal=include_internal):
            if row["decision"] != "exclude":
                continue
            try:
                self.validate_scope(project_id, row, include_internal=include_internal)
                exclusions.append(labels(row))
            except (KeyError, ValueError):
                continue
        return {"schema_version": 1, "items": items, "exclusions": exclusions}

    def public_plan(self, project_id, plan):
        """Only scoped public names; full-company exclusions do not leak private reasons."""
        plan = clean_plan(plan)
        view = self.view(project_id, include_internal=False)
        excluded = {name.casefold() for r in view["exclusions"] if r["company_wide"] for name in r["peer_names"]}
        peers = [r for r in plan.get("competitors", []) if str(r.get("name") or "").casefold() not in excluded]
        for row in view["items"]:
            if row["origin"] != "user_override" or row["relation_type"] == "unrelated" or not company_name(row['peer_name']):
                continue
            if row["peer_name"].casefold() not in excluded and not any(p["name"] == row["peer_name"] for p in peers):
                peers.append({"name": row["peer_name"], "reason": "用户确认跟踪"})
        return {**plan, "competitors": peers[:30]}

    def _named_change(self, project_id, body, db):
        if body.decision == 'include' and not company_name(body.name):
            raise ValueError('请输入公司名称')
        subject = self.objects.ensure_subject(project_id, db=db)
        names = {subject["name"].casefold(), *(a["name"].casefold() for a in subject["aliases"])}
        project = db.execute("SELECT p.name,s.legal_name FROM projects p JOIN studio_projects s ON s.project_id=p.id "
                             "WHERE p.id=?", (project_id,)).fetchone()
        names.update(str(n or "").casefold() for n in project)
        if body.name.casefold() in names:
            raise ValueError("a company cannot be its own competitor")
        if body.peer_id:
            peer = self.objects.get(project_id, body.peer_id, include_internal=False, db=db)
            if peer["kind"] != "company" or body.name.casefold() not in {
                peer["name"].casefold(), *(a["name"].casefold() for a in peer["aliases"])
            }:
                raise ValueError("company name does not match selected identity")
        else:
            peers = [json.loads(r[0]) for r in db.execute(
                "SELECT record FROM studio_monitor_objects WHERE project_id=? AND visibility='public'", (project_id,))]
            matched = [r for r in peers if r["kind"] == "company" and r["name"].casefold() == body.name.casefold()]
            if len(matched) > 1:
                raise ValueError("multiple identities have this name; select a company identity")
            peer = matched[0] if matched else self.objects.save(project_id, ObjectInput(
                kind="company", name=body.name, purpose="related", visibility="public"), actor="user", db=db)
        return RelationshipDecision(subject_id=subject["id"], peer_id=peer["id"], decision=body.decision,
                                    reason={"include": "用户确认跟踪", "exclude": "用户移除", "reset": "用户撤销移除"}[body.decision])

    def correct(self, service, project_id, body, *, token=None):
        prefix = "peer_selection:" if isinstance(body, PeerSelection) else "relationship_correction:"
        payload = hashlib.sha256((prefix + body.model_dump_json()).encode()).hexdigest()
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            grant = service._authorize(db, token, project_id, "write") if token else None
            actor, internal = (grant["id"], grant["allow_internal"]) if grant else ("local_web", True)
            old = db.execute("SELECT payload_hash,receipt FROM studio_operations WHERE instance_id=? AND project_id=? "
                             "AND actor=? AND idempotency_key=?", (service.instance_id, project_id, actor, body.idempotency_key)).fetchone()
            if old:
                if old[0] != payload:
                    raise VersionConflict("idempotency key already used with another payload")
                return json.loads(old[1])
            project = db.execute("SELECT config_version FROM studio_projects WHERE project_id=?", (project_id,)).fetchone()
            if not project:
                raise ProjectMissing("Studio project not found")
            if project[0] != body.expected_version:
                raise VersionConflict("project configuration has changed; read latest version")
            keys = set()
            changes = []
            decisions = (self._named_change(project_id, body, db),) if isinstance(body, PeerSelection) else body.changes
            for change in decisions:
                scope = scope_fields(change.model_dump())
                visibility, versions = self.validate_scope(project_id, scope, include_internal=internal, db=db)
                if visibility == "internal" and not internal:
                    raise AccessDenied("private relationship requires internal write grant")
                key = scope_key(scope)
                if key in keys:
                    raise ValueError("one correction cannot repeat the same relationship scope")
                keys.add(key)
                previous = db.execute("SELECT version FROM studio_relationship_overrides WHERE project_id=? AND scope_key=?", (project_id, key)).fetchone()
                version = previous[0] + 1 if previous else 1
                record = {**change.model_dump(), "scope_key": key, "project_id": project_id, "schema_version": 1,
                          "origin": "user_override", "support": "user_specified", "visibility": visibility,
                          "object_versions": versions, "version": version, "actor": actor, "created_at": now()}
                db.execute("INSERT INTO studio_relationship_overrides VALUES (?,?,?,?,?) ON CONFLICT(project_id,scope_key) "
                           "DO UPDATE SET version=excluded.version,visibility=excluded.visibility,record=excluded.record",
                           (project_id, key, version, visibility, json.dumps(record)))
                self._history(db, record)
                changes.append({"scope_key": key, "decision": change.decision, "version": version})
                service.evidence.dependencies._change(db, project_id, visibility, "relationship_corrected", {
                    **scope, "scope_key": key, "decision": change.decision, "override_version": version,
                })
            service.evidence.dependencies.on_relationship_corrected(project_id, db=db)
            db.execute("UPDATE studio_projects SET config_version=config_version+1 WHERE project_id=?", (project_id,))
            receipt = {"instance_id": service.instance_id, "schema_version": 1, "project_id": project_id,
                       "operation_id": str(uuid.uuid4()), "version": project[0] + 1, "saved": True,
                       "operation_status": "completed", "job_status": "not_requested", "job_id": None,
                       "actor": actor, "changes": changes}
            db.execute("INSERT INTO studio_operations VALUES (?,?,?,?,?,?,?,?)",
                       (receipt["operation_id"], project_id, service.instance_id, actor, body.idempotency_key, payload, json.dumps(receipt), now()))
            return receipt
