-- Reviewed amendments preserve the original watch item while enabling follow-up work.
ALTER TABLE watch_items ADD COLUMN current_observation_id text;
ALTER TABLE watch_items ADD COLUMN initial_question text;
ALTER TABLE watch_items ADD COLUMN initial_owner text;
ALTER TABLE watch_items ADD COLUMN initial_due_on text;
ALTER TABLE watch_items ADD COLUMN resolved_at text;
UPDATE watch_items SET
    current_observation_id = observation_id,
    initial_question = question,
    initial_owner = owner,
    initial_due_on = due_on;
ALTER TABLE watch_items ALTER COLUMN current_observation_id SET NOT NULL;
ALTER TABLE watch_items ALTER COLUMN initial_question SET NOT NULL;
ALTER TABLE watch_items ADD CONSTRAINT watch_items_current_observation_fk
    FOREIGN KEY (current_observation_id, project_id) REFERENCES observations(id, project_id);

CREATE TABLE watch_item_changes (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id),
    watch_item_id text NOT NULL,
    base_version integer NOT NULL CHECK (base_version > 0),
    target_status text NOT NULL CHECK (target_status IN ('watching', 'action_required', 'resolved')),
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 1000),
    owner text CHECK (length(owner) BETWEEN 1 AND 128),
    due_on text,
    evidence_observation_id text,
    reason text NOT NULL CHECK (length(reason) BETWEEN 5 AND 500),
    status text NOT NULL CHECK (status IN ('candidate', 'accepted', 'rejected')),
    proposed_by text,
    reviewed_by text,
    review_note text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    FOREIGN KEY (watch_item_id, project_id) REFERENCES watch_items(id, project_id),
    FOREIGN KEY (evidence_observation_id, project_id) REFERENCES observations(id, project_id)
);
CREATE UNIQUE INDEX watch_item_changes_one_pending
    ON watch_item_changes (watch_item_id) WHERE status = 'candidate';
