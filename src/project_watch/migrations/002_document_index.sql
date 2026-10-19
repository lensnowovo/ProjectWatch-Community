-- Rebuildable classification hints only; original sources and confirmed facts stay unchanged.
CREATE TABLE document_index (
    source_id text PRIMARY KEY,
    project_id text NOT NULL,
    topic text NOT NULL CHECK (topic IN (
        '公司与团队', '产品与技术', '经营情况', '财务表现',
        '行业与竞争', '风险与合规', '未分类'
    )),
    basis text NOT NULL CHECK (basis IN ('filename', 'content', 'filename_and_content', 'none')),
    rule_version integer NOT NULL CHECK (rule_version > 0),
    created_at text NOT NULL,
    FOREIGN KEY (source_id, project_id) REFERENCES sources(id, project_id)
);
CREATE INDEX document_index_project_topic ON document_index (project_id, topic);
