-- Derived review prompts; confirmed observations remain immutable in meaning/status.
CREATE TABLE review_impacts (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    trigger_source_id text NOT NULL,
    observation_id text NOT NULL,
    topic text NOT NULL CHECK (topic IN (
        '公司与团队', '产品与技术', '经营情况', '财务表现',
        '行业与竞争', '风险与合规'
    )),
    status text NOT NULL CHECK (status IN ('pending', 'reviewed')),
    resolution text CHECK (resolution IN ('unchanged', 'follow_up')),
    note text CHECK (length(note) BETWEEN 1 AND 500),
    reviewer_ref text,
    created_at text NOT NULL,
    reviewed_at text,
    UNIQUE (trigger_source_id, observation_id),
    FOREIGN KEY (trigger_source_id, project_id) REFERENCES sources(id, project_id),
    FOREIGN KEY (observation_id, project_id) REFERENCES observations(id, project_id)
);
CREATE INDEX review_impacts_project_status ON review_impacts (project_id, status, created_at, id);
