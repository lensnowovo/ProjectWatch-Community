-- Append-only human entity decisions. No project or company facts are seeded.
CREATE TABLE project_identity_reviews (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    version integer NOT NULL CHECK (version > 0),
    action text NOT NULL CHECK (action IN ('verify', 'reopen')),
    legal_name text,
    registration_code text,
    evidence_source_id text,
    evidence_locator text,
    evidence_url text,
    note text NOT NULL,
    actor_ref text NOT NULL,
    created_at text NOT NULL,
    UNIQUE (project_id, version),
    FOREIGN KEY (evidence_source_id, project_id) REFERENCES sources(id, project_id)
);
CREATE INDEX project_identity_reviews_order
ON project_identity_reviews (project_id, version DESC);
