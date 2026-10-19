-- Standalone Project Watch schema. IDs and UTC ISO-8601 timestamps deliberately
-- match the local repository's wire format; this is not a Greenpine DB migration.
CREATE TABLE projects (
    id text PRIMARY KEY,
    name text NOT NULL,
    name_key text NOT NULL UNIQUE,
    identity_status text NOT NULL CHECK (identity_status IN ('unverified', 'verified')),
    created_at text NOT NULL
);
CREATE INDEX projects_created_order ON projects (created_at, id);

CREATE TABLE sources (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    kind text NOT NULL,
    reference text NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at text NOT NULL,
    UNIQUE (project_id, content_sha256),
    UNIQUE (id, project_id)
);
CREATE INDEX sources_project_order ON sources (project_id, created_at, id);

CREATE TABLE observations (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    source_id text NOT NULL,
    subject text NOT NULL,
    statement text NOT NULL,
    number text,
    unit text,
    period text,
    scope text,
    source_locator text NOT NULL,
    status text NOT NULL CHECK (status IN ('candidate', 'confirmed', 'rejected')),
    version integer NOT NULL CHECK (version > 0),
    created_at text NOT NULL,
    fingerprint text NOT NULL,
    UNIQUE (project_id, fingerprint),
    UNIQUE (id, project_id),
    FOREIGN KEY (source_id, project_id) REFERENCES sources(id, project_id)
);
CREATE INDEX observations_project_order ON observations (project_id, created_at, id);
CREATE INDEX observations_source ON observations (source_id, project_id);

CREATE TABLE review_decisions (
    id text PRIMARY KEY,
    observation_id text NOT NULL REFERENCES observations(id),
    reviewer text NOT NULL,
    action text NOT NULL CHECK (action IN ('confirm', 'reject')),
    expected_version integer NOT NULL,
    decided_at text NOT NULL
);
CREATE INDEX review_decisions_observation ON review_decisions (observation_id, decided_at, id);

CREATE TABLE audit_events (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    action text NOT NULL,
    actor_kind text NOT NULL,
    actor_ref text,
    channel text NOT NULL,
    subject_id text NOT NULL,
    source_id text,
    source_sha256 text,
    source_locator text,
    created_at text NOT NULL,
    FOREIGN KEY (source_id, project_id) REFERENCES sources(id, project_id)
);
CREATE INDEX audit_events_project_order ON audit_events (project_id, created_at, id);
CREATE INDEX audit_events_subject ON audit_events (project_id, subject_id, action);

CREATE TABLE intake_jobs (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    source_id text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('queued', 'processing', 'completed', 'needs_ocr', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error_code text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (source_id, project_id) REFERENCES sources(id, project_id)
);
CREATE INDEX intake_jobs_queue ON intake_jobs (status, created_at, id);

CREATE TABLE document_fragments (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    source_id text NOT NULL,
    job_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    locator text NOT NULL,
    text text NOT NULL,
    UNIQUE (source_id, ordinal),
    FOREIGN KEY (source_id, project_id) REFERENCES sources(id, project_id),
    FOREIGN KEY (job_id, project_id) REFERENCES intake_jobs(id, project_id)
);
CREATE INDEX document_fragments_job ON document_fragments (job_id, ordinal);

CREATE TABLE monitoring_plans (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    kind text NOT NULL,
    target text NOT NULL,
    cadence_minutes integer NOT NULL CHECK (cadence_minutes > 0),
    enabled integer NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    created_at text NOT NULL,
    UNIQUE (project_id, kind, target),
    UNIQUE (id, project_id)
);
CREATE INDEX monitoring_plans_project ON monitoring_plans (project_id, created_at, id);

CREATE TABLE source_runs (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    plan_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('completed', 'no_results', 'unreachable', 'not_run')),
    error_code text,
    started_at text NOT NULL,
    finished_at text NOT NULL,
    idempotency_key text,
    item_count integer NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    new_count integer NOT NULL DEFAULT 0 CHECK (new_count >= 0),
    UNIQUE (id, project_id),
    UNIQUE (plan_id, idempotency_key),
    FOREIGN KEY (plan_id, project_id) REFERENCES monitoring_plans(id, project_id)
);
CREATE INDEX source_runs_plan_order ON source_runs (plan_id, started_at, id);

CREATE TABLE signals (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    plan_id text NOT NULL,
    run_id text NOT NULL,
    external_id text NOT NULL,
    title text NOT NULL,
    url text NOT NULL,
    summary text NOT NULL,
    published_at text,
    first_seen_at text NOT NULL,
    content_sha256 text NOT NULL,
    freshness text NOT NULL,
    status text NOT NULL,
    UNIQUE (plan_id, external_id, content_sha256),
    FOREIGN KEY (plan_id, project_id) REFERENCES monitoring_plans(id, project_id),
    FOREIGN KEY (run_id, project_id) REFERENCES source_runs(id, project_id)
);
CREATE INDEX signals_project_order ON signals (project_id, first_seen_at DESC, id DESC);

CREATE TABLE monitoring_jobs (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    plan_id text NOT NULL,
    idempotency_key text NOT NULL,
    trigger text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'processing', 'done', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_until text,
    run_id text,
    error_code text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    UNIQUE (plan_id, idempotency_key),
    FOREIGN KEY (plan_id, project_id) REFERENCES monitoring_plans(id, project_id),
    FOREIGN KEY (run_id, project_id) REFERENCES source_runs(id, project_id)
);
CREATE INDEX monitoring_jobs_queue ON monitoring_jobs (status, lease_until, created_at, id);
CREATE INDEX monitoring_jobs_project ON monitoring_jobs (project_id, created_at, id);

CREATE TABLE execution_tasks (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    contract_version integer NOT NULL CHECK (contract_version > 0),
    kind text NOT NULL,
    mode text NOT NULL,
    status text NOT NULL,
    source_ids_json text NOT NULL,
    outcome_json text NOT NULL,
    idempotency_key text NOT NULL,
    created_at text NOT NULL,
    UNIQUE (project_id, idempotency_key)
);
CREATE INDEX execution_tasks_project_order ON execution_tasks (project_id, created_at DESC, id DESC);

CREATE TABLE team_users (
    id text PRIMARY KEY,
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    global_admin integer NOT NULL DEFAULT 0 CHECK (global_admin IN (0, 1)),
    active integer NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    failed_attempts integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_until text,
    created_at text NOT NULL
);
CREATE TABLE team_memberships (
    project_id text NOT NULL REFERENCES projects(id),
    user_id text NOT NULL REFERENCES team_users(id),
    role text NOT NULL CHECK (role IN ('viewer', 'analyst', 'reviewer', 'admin')),
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX team_memberships_user ON team_memberships (user_id, project_id);
CREATE TABLE team_sessions (
    token_sha256 text PRIMARY KEY,
    user_id text NOT NULL REFERENCES team_users(id),
    expires_at text NOT NULL,
    revoked_at text,
    created_at text NOT NULL
);
CREATE INDEX team_sessions_user ON team_sessions (user_id, expires_at);
CREATE TABLE team_security_events (
    id text PRIMARY KEY,
    actor_user_id text,
    action text NOT NULL,
    target_user_id text,
    project_id text,
    subject_id text,
    previous_role text,
    new_role text,
    created_at text NOT NULL
);
CREATE INDEX team_security_events_order ON team_security_events (created_at, id);
CREATE TABLE team_executor_tokens (
    id text PRIMARY KEY,
    token_sha256 text NOT NULL UNIQUE,
    project_id text NOT NULL REFERENCES projects(id),
    issued_by text NOT NULL REFERENCES team_users(id),
    label text NOT NULL,
    permission text NOT NULL CHECK (permission IN ('read', 'propose')),
    expires_at text NOT NULL,
    revoked_at text,
    created_at text NOT NULL
);
CREATE INDEX team_executor_tokens_project ON team_executor_tokens (project_id, expires_at);
