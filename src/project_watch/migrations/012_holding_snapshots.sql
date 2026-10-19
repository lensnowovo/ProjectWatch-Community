-- Fund-specific position proposals. Existing company-financing facts are not backfilled.
CREATE TABLE holding_snapshots (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    fund_name text NOT NULL CHECK (length(fund_name) BETWEEN 1 AND 200),
    as_of text NOT NULL,
    source_id text NOT NULL,
    source_locator text NOT NULL,
    invested_cost_wan text,
    ownership_pct text,
    current_value_wan text,
    status text NOT NULL CHECK (status IN ('candidate', 'confirmed', 'rejected')),
    version integer NOT NULL CHECK (version > 0),
    proposed_by text,
    reviewed_by text,
    review_note text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    CHECK (invested_cost_wan IS NOT NULL OR ownership_pct IS NOT NULL
           OR current_value_wan IS NOT NULL),
    FOREIGN KEY (source_id, project_id) REFERENCES sources(id, project_id)
);
CREATE INDEX holding_snapshots_project_fund_date
    ON holding_snapshots (project_id, fund_name, as_of DESC, id);
