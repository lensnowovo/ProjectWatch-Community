"""Scoped identities, immutable fact records and public-only query inputs.

Name matching does not merge identities. Human identity correction is a durable
override, not an assertion that a registration has been independently verified.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import closing
from datetime import date
from urllib.parse import urlsplit

from project_watch.studio.application import VersionConflict
from project_watch.studio.evidence_store import EvidenceStore, _json
from project_watch.studio.monitoring_contracts import Alias, FactInput, ObjectInput
from project_watch.studio.peer_names import clean_plan
from project_watch.studio.store import StudioStore, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_monitor_objects (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    version INTEGER NOT NULL, visibility TEXT NOT NULL, record TEXT NOT NULL,
    user_override INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS studio_objects_project ON studio_monitor_objects(project_id,id);
CREATE TABLE IF NOT EXISTS studio_primary_objects (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    object_id TEXT NOT NULL UNIQUE REFERENCES studio_monitor_objects(id)
);
CREATE TABLE IF NOT EXISTS studio_document_subjects (
    project_id TEXT NOT NULL REFERENCES projects(id), source_id TEXT NOT NULL REFERENCES sources(id),
    object_id TEXT NOT NULL UNIQUE REFERENCES studio_monitor_objects(id), PRIMARY KEY(project_id,source_id)
);
CREATE TABLE IF NOT EXISTS studio_object_history (
    object_id TEXT NOT NULL REFERENCES studio_monitor_objects(id), version INTEGER NOT NULL,
    record TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(object_id,version)
);
CREATE TABLE IF NOT EXISTS studio_fact_records (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    subject_id TEXT NOT NULL REFERENCES studio_monitor_objects(id),
    visibility TEXT NOT NULL, content_hash TEXT NOT NULL, record TEXT NOT NULL,
    UNIQUE(project_id,subject_id,content_hash)
);
CREATE TRIGGER IF NOT EXISTS studio_facts_immutable_update BEFORE UPDATE ON studio_fact_records
BEGIN SELECT RAISE(ABORT,'immutable fact record'); END;
CREATE TRIGGER IF NOT EXISTS studio_facts_immutable_delete BEFORE DELETE ON studio_fact_records
BEGIN SELECT RAISE(ABORT,'immutable fact record'); END;
"""


class ObjectStore:
    def __init__(self, store: StudioStore, evidence: EvidenceStore):
        self.store = store
        self.evidence = evidence
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)

    @staticmethod
    def _disclose(record: dict, include_internal: bool) -> dict:
        if not include_internal:
            record["aliases"] = [a for a in record["aliases"] if a["visibility"] == "public"]
        return record

    def get(self, project_id: str, object_id: str, *, include_internal: bool = True, db=None) -> dict:
        def read(connection):
            row = connection.execute("SELECT record FROM studio_monitor_objects WHERE project_id=? AND id=? "
                                     "AND (? OR visibility='public')", (project_id, object_id, include_internal)).fetchone()
            if row is None:
                raise KeyError("object not found in permitted project")
            return self._disclose(json.loads(row[0]), include_internal)
        if db is not None:
            return read(db)
        with closing(self.store._connect()) as connection:
            return read(connection)

    def page(self, project_id: str, *, include_internal: bool = True, limit: int = 500, after: str = "",
             current_aliases_only: bool = False) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 500 or len(after) > 100:
            raise ValueError("invalid object page")
        with closing(self.store._connect()) as db:
            db.execute('BEGIN')
            rows = [self._disclose(json.loads(r[0]), include_internal) for r in db.execute(
                "SELECT record FROM studio_monitor_objects WHERE project_id=? AND id>? AND (? OR visibility='public') "
                "ORDER BY id LIMIT ?", (project_id, after, include_internal, limit))]
            if current_aliases_only:
                for row in rows:
                    if row['user_override'] or not row['aliases']:
                        continue
                    current = bool(row['refs'])
                    for ref in row['refs']:
                        version = self.evidence.get(project_id, ref['evidence_version_id'], db=db)
                        head = db.execute('SELECT evidence_version_id FROM studio_evidence_heads WHERE project_id=? AND source_key=?',
                                          (project_id, version['source_key'])).fetchone()
                        withdrawn = db.execute('SELECT status FROM studio_evidence_lifecycle WHERE project_id=? AND evidence_version_id=?',
                                               (project_id, version['id'])).fetchone()
                        current &= bool(head and head[0] == version['id']) and not (withdrawn and withdrawn[0] == 'withdrawn')
                    if not current:
                        row['aliases'] = [a for a in row['aliases'] if a['visibility'] != 'public']
            return rows

    def validate(self, project_id: str, item: ObjectInput | FactInput, actor: str = "automatic") -> None:
        for ref in item.refs:
            resolved = self.evidence.resolve(project_id, ref)
            if item.visibility == "public" and resolved["visibility"] != "public":
                raise ValueError("public objects/facts cannot expose private evidence")
        if isinstance(item, ObjectInput):
            if actor not in ("user", "automatic"):
                raise ValueError("invalid identity actor")
            if item.website:
                try:
                    parsed = urlsplit(item.website)
                    safe = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
                except ValueError:
                    safe = False
                if not safe:
                    raise ValueError("invalid object website")
            if actor == "automatic" and (item.registry_id or item.registration_code or item.parent_id) and not item.refs:
                raise ValueError("automatic identity/code/ownership association needs located evidence")
            if actor == "automatic" and any(code and not any(code in ref.quote for ref in item.refs)
                                            for code in (item.registry_id, item.registration_code)):
                raise ValueError("identity/registration code must occur in its evidence quote")
            if item.visibility == "internal" and any(a.visibility == "public" for a in item.aliases):
                raise ValueError("private identity cannot export public aliases")

    def save(self, project_id: str, item: ObjectInput, *, actor: str, object_id: str | None = None,
             expected_version: int | None = None, db=None) -> dict:
        self.validate(project_id, item, actor)

        def write(connection):
            if item.parent_id:
                parent = self.get(project_id, item.parent_id, db=connection)
                if item.parent_id == object_id or parent["kind"] != "company":
                    raise ValueError("invalid parent identity")
                if item.visibility == "public" and parent["visibility"] != "public":
                    raise ValueError("public ownership cannot reveal private parent")
                ancestor = parent
                seen = {object_id}
                while ancestor.get("parent_id"):
                    if ancestor["id"] in seen or ancestor["parent_id"] in seen:
                        raise ValueError("ownership cycle")
                    seen.add(ancestor["id"])
                    ancestor = self.get(project_id, ancestor["parent_id"], db=connection)
            old = self.get(project_id, object_id, db=connection) if object_id else None
            if old:
                if type(expected_version) is not int or expected_version != old["version"]:
                    raise VersionConflict("object version changed; read latest object")
                if old["kind"] != item.kind:
                    raise ValueError("object kind cannot be changed")
                if actor == "automatic" and old["user_override"]:
                    return old
                if old["visibility"] == "public" and item.visibility == "internal":
                    # Withdrawing public identity must not leave old public facts
                    # or children accessible. Explicit dependency withdrawal is A4.
                    linked = connection.execute("SELECT 1 FROM studio_fact_records WHERE subject_id=? AND visibility='public'",
                                                (object_id,)).fetchone()
                    if linked:
                        raise ValueError("withdraw linked public facts before hiding their subject")
            version = old["version"] + 1 if old else 1
            identity = object_id or str(uuid.uuid4())
            record = {**item.model_dump(mode="json"), "id": identity, "project_id": project_id, "version": version,
                      "identity_basis": "user_specified" if actor == "user" else (
                          "candidate_with_evidence" if item.refs else "candidate"),
                      "user_override": actor == "user", "updated_at": now(), "schema_version": 1}
            connection.execute("INSERT INTO studio_monitor_objects VALUES (?,?,?,?,?,?) "
                               "ON CONFLICT(id) DO UPDATE SET version=excluded.version,visibility=excluded.visibility,"
                               "record=excluded.record,user_override=excluded.user_override",
                               (identity, project_id, version, item.visibility, _json(record), actor == "user"))
            connection.execute("INSERT INTO studio_object_history VALUES (?,?,?,?,?)",
                               (identity, version, _json(record), actor, now()))
            self.evidence.dependencies.on_object_saved(record, old, db=connection)
            return record
        if db is not None:
            return write(db)
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return write(connection)

    def history(self, project_id: str, object_id: str) -> list[dict]:
        self.get(project_id, object_id)
        with closing(self.store._connect()) as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT record FROM studio_object_history WHERE object_id=? ORDER BY version", (object_id,))]

    def save_fact(self, project_id: str, item: FactInput, *, db=None) -> dict:
        self.validate(project_id, item)
        def write(connection):
            subject = self.get(project_id, item.subject_id, db=connection)
            if item.visibility == "public" and subject["visibility"] != "public":
                raise ValueError("public facts cannot expose private subject")
            versions = {subject['id']: subject['version']}
            parent = None
            if item.attributes and subject['kind'] == 'product' and subject['parent_id']:
                parent = self.get(project_id, subject['parent_id'], db=connection)
                versions[parent['id']] = parent['version']
            if item.attributes and item.attributes.kind == 'business_relation':
                peer = self.get(project_id, item.attributes.peer_id, db=connection)
                if peer['kind'] != 'company' or peer['id'] == subject['id'] or (parent and peer['id'] == parent['id']):
                    raise ValueError('business relation requires a distinct scoped company')
                if item.visibility == 'public' and peer['visibility'] != 'public':
                    raise ValueError('public relation cannot expose private peer')
                versions[peer['id']] = peer['version']
            record = item.model_dump(mode="json")
            if record.get('attributes') is None:
                record.pop('attributes', None)  # Preserve old no-attribute fingerprints.
            digest = hashlib.sha256(_json(record).encode()).hexdigest()
            old = connection.execute("SELECT record FROM studio_fact_records WHERE project_id=? AND subject_id=? AND content_hash=?",
                             (project_id, item.subject_id, digest)).fetchone()
            if old:
                return json.loads(old[0])
            record.update(id=str(uuid.uuid4()), project_id=project_id, subject_version=subject["version"],
                          content_hash=digest, discovered_at=now(), schema_version=1,
                          assertion_basis="source_assertion_not_independent_verification")
            connection.execute("INSERT INTO studio_fact_records VALUES (?,?,?,?,?,?)",
                       (record["id"], project_id, item.subject_id, item.visibility, digest, _json(record)))
            self.evidence.dependencies.track(project_id, "fact:" + record["id"], "fact",
                                             [r.evidence_version_id for r in item.refs], db=connection)
            self.evidence.dependencies.track_objects(project_id, "fact:" + record["id"], versions, db=connection)
            self.evidence.dependencies._change(connection, project_id, item.visibility, 'fact_added', {
                'fact_id': record['id'], 'subject_id': item.subject_id, 'predicate': item.predicate})
            return record
        if db is not None:
            return write(db)
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return write(connection)

    def facts(self, project_id: str, subject_id: str, *, include_internal: bool = True) -> list[dict]:
        self.get(project_id, subject_id, include_internal=include_internal)
        with closing(self.store._connect()) as db:
            # One live, project-scoped read, not one connection per fact. LEFT
            # JOIN retains facts whose dependency is missing; do not cache this
            # across requests because withdrawal/identity edits invalidate it.
            records = [{**json.loads(r[0]), 'dependency': json.loads(r[1]) if r[1] else None}
                       for r in db.execute(
                "SELECT f.record,d.record FROM studio_fact_records f "
                "LEFT JOIN studio_analysis_dependencies d ON d.project_id=f.project_id "
                "AND d.artifact_key='fact:' || f.id WHERE f.project_id=? AND f.subject_id=? "
                "AND (? OR f.visibility='public') ORDER BY f.id LIMIT 500", (project_id, subject_id, include_internal))]
        visible = []
        for record in records:
            peer_id = (record.get('attributes') or {}).get('peer_id')
            if peer_id and not include_internal:
                try:
                    self.get(project_id, peer_id, include_internal=False)
                except KeyError:
                    continue
            visible.append(record)
        return visible

    def public_names(self, project_id: str, *, as_of: str | None = None, kind: str | None = None) -> list[str]:
        day = date.fromisoformat(as_of) if as_of else date.today()
        names = []
        for item in self.page(project_id, include_internal=False, current_aliases_only=True):
            if kind and item["kind"] != kind:
                continue
            names.append(item["name"])
            for alias in item["aliases"]:
                if (alias["visibility"] == "public"
                        and (not alias["valid_from"] or date.fromisoformat(alias["valid_from"]) <= day)
                        and (not alias["valid_to"] or day <= date.fromisoformat(alias["valid_to"]))):
                    names.append(alias["name"])
        return list(dict.fromkeys(names))

    def query_inputs(self, project_id: str) -> dict:
        items = self.page(project_id, include_internal=False, current_aliases_only=True)
        subjects = [r for r in items if r.get("purpose") == "subject" and r["kind"] == "company"]
        companies = []
        day = date.today().isoformat()
        for row in subjects:
            companies.append(row["name"])
            companies.extend(a["name"] for a in row["aliases"] if a["visibility"] == "public"
                             and (not a["valid_from"] or a["valid_from"] <= day)
                             and (not a["valid_to"] or day <= a["valid_to"]))
        return {"company_names": list(dict.fromkeys(companies)),
                "product_names": self.public_names(project_id, kind="product"),
                "people": self.public_names(project_id, kind="person"),
                "versions": {r["id"]: r["version"] for r in items}, "schema_version": 1}

    def confirm_subject_identity(self, project_id: str, name: str, names: list[str] | None, *, db) -> dict:
        """Explicit user company names, never a resolver's mixed search aliases.

        Caller owns the write transaction shared with the project profile. None
        means no alias-edit intent; [] explicitly removes public user mappings.
        """
        from project_watch.studio.business_identity import generic_name

        name = name.strip()
        if not name or generic_name(name):
            raise ValueError('invalid company name')
        if names is not None and (len(names) > 12 or any(
                not isinstance(n, str) or not n.strip() or len(n) > 200 or generic_name(n) for n in names)):
            raise ValueError('invalid confirmed company names')
        mapped = db.execute('SELECT object_id FROM studio_primary_objects WHERE project_id=?', (project_id,)).fetchone()
        old = self.get(project_id, mapped[0], db=db) if mapped else None
        item = ObjectInput.model_validate({k: old[k] for k in ObjectInput.model_fields}) if old else ObjectInput(
            kind='company', name=name, purpose='subject', visibility='public')
        if item.kind != 'company' or item.visibility != 'public':
            raise ValueError('company confirmation requires a public company subject')
        private = [a for a in item.aliases if a.visibility == 'internal']
        public = {a.name: a for a in item.aliases if a.visibility == 'public'}
        if names is None:
            # Canonical confirmation must not turn unsupported automatic aliases
            # into human assertions just by changing user_override.
            keep = public if old and old['user_override'] else {}
            selected = [a for a in item.aliases if a.visibility == 'public' and a.name in keep]
        else:
            selected = [public.get(n, Alias(name=n, visibility='public'))
                        for n in dict.fromkeys(n.strip() for n in names) if n != name]
        # Retain existing order for stable identical confirmations and private
        # names, so a no-op does not invalidate dependent work.
        chosen = {(a.visibility, a.name): a for a in [*private, *selected]}
        aliases = [chosen.pop((a.visibility, a.name)) for a in item.aliases if (a.visibility, a.name) in chosen]
        aliases.extend(chosen.values())
        updated = ObjectInput.model_validate({**item.model_dump(), 'name': name, 'aliases': aliases})
        if old and old['user_override'] and updated == item:
            return old
        record = self.save(project_id, updated, actor='user', object_id=old['id'] if old else None,
                           expected_version=old['version'] if old else None, db=db)
        if old is None:
            db.execute('INSERT INTO studio_primary_objects VALUES (?,?)', (project_id, record['id']))
        return record

    def ensure_subject(self, project_id: str, *, db=None) -> dict:
        def read_or_create(db):
            mapped = db.execute("SELECT object_id FROM studio_primary_objects WHERE project_id=?", (project_id,)).fetchone()
            if mapped:
                return self.get(project_id, mapped[0], db=db)
            project = db.execute("SELECT p.name,s.legal_name FROM projects p JOIN studio_projects s ON s.project_id=p.id "
                                 "WHERE p.id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError("Studio project not found")
            record = self.save(project_id, ObjectInput(kind="company", name=project["legal_name"] or project["name"],
                                                       purpose="subject", visibility="public"), actor="automatic", db=db)
            db.execute("INSERT INTO studio_primary_objects VALUES (?,?)", (project_id, record["id"]))
            return record
        if db is not None:
            return read_or_create(db)
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return read_or_create(connection)

    def unassigned_document_subject(self, project_id: str, source_id: str) -> dict:
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT object_id FROM studio_document_subjects WHERE project_id=? AND source_id=?",
                             (project_id, source_id)).fetchone()
            if row:
                return self.get(project_id, row[0], db=db)
            source = db.execute("SELECT id FROM sources WHERE project_id=? AND id=?", (project_id, source_id)).fetchone()
            if not source:
                raise KeyError("source not found in project")
            record = self.save(project_id, ObjectInput(kind="company", name="资料主体未确认"), actor="automatic", db=db)
            db.execute("INSERT INTO studio_document_subjects VALUES (?,?,?)", (project_id, source_id, record["id"]))
            return record

    def scoped_plan(self, project_id: str, plan: dict) -> dict:
        plan = clean_plan(plan)
        rows = self.page(project_id)
        if not rows:
            # Preserve established identity/news fallback until A3 intake builds objects; cited dossier people lead.
            plan["people"] = list(dict.fromkeys([*(plan.get("dossier_people") or []), *(plan.get("people") or [])]))[:3]
            return plan
        inputs = self.query_inputs(project_id)
        hidden = [r["name"].casefold() for r in rows if r["visibility"] == "internal"]
        hidden += [a["name"].casefold() for r in rows for a in r["aliases"] if a["visibility"] == "internal"]
        allowed = {n.casefold() for key in ("company_names", "product_names", "people") for n in inputs[key]}
        hidden = [n for n in hidden if n not in allowed]
        result = dict(plan)
        for key, value in result.items():
            if not isinstance(value, list):
                continue
            result[key] = [entry for entry in value if not any(n in _json(entry).casefold() for n in hidden)]
        if inputs["company_names"]:
            result["company_queries"] = inputs["company_names"][:4]
            result["affiliation_names"] = inputs["company_names"][:2]
            result["patent_assignees"] = inputs["company_names"][:2]
            result["trial_sponsors"] = inputs["company_names"][:3]
        # Confirmed people first, then people named with sources in the public dossier (never model-only names).
        dossier = [n for n in plan.get("dossier_people") or [] if isinstance(n, str) and n.casefold() not in hidden]
        result["people"] = list(dict.fromkeys([*inputs["people"], *dossier]))[:3]
        if not result.get("company_queries"):
            result["people"] = []
        result["object_versions"] = inputs["versions"]
        return result
