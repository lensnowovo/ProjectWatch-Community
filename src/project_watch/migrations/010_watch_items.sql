-- Source-bound, reviewed project follow-ups. Business status never changes without review.
CREATE TABLE watch_items (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    observation_id text NOT NULL,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    title_key text NOT NULL,
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 1000),
    owner text CHECK (length(owner) BETWEEN 1 AND 128),
    due_on text,
    status text NOT NULL CHECK (status IN ('candidate', 'watching', 'action_required', 'rejected')),
    version integer NOT NULL CHECK (version > 0),
    proposed_by text,
    reviewed_by text,
    review_note text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    UNIQUE (project_id, observation_id, title_key),
    UNIQUE (id, project_id),
    FOREIGN KEY (observation_id, project_id) REFERENCES observations(id, project_id)
);
CREATE INDEX watch_items_project_status ON watch_items (project_id, status, due_on);
CREATE TABLE watch_item_events (
    id text PRIMARY KEY,
    watch_item_id text NOT NULL,
    project_id text NOT NULL REFERENCES projects(id),
    version integer NOT NULL,
    from_status text,
    to_status text NOT NULL,
    actor_ref text,
    note text,
    created_at text NOT NULL,
    UNIQUE (watch_item_id, version),
    FOREIGN KEY (watch_item_id, project_id) REFERENCES watch_items(id, project_id)
);
