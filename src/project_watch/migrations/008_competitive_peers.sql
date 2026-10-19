-- Reviewed, project-scoped peer set. Names may be tracked without a linked project.
CREATE TABLE competitive_peers (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    name text NOT NULL,
    name_key text NOT NULL,
    linked_project_id text REFERENCES projects(id),
    status text NOT NULL CHECK (status IN ('candidate', 'confirmed', 'rejected')),
    version integer NOT NULL CHECK (version > 0),
    proposed_by text,
    reviewed_by text,
    note text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    UNIQUE (project_id, name_key),
    CHECK (linked_project_id IS NULL OR linked_project_id <> project_id)
);
CREATE INDEX competitive_peers_project ON competitive_peers (project_id, created_at, id);
