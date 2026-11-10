"""Record candidate evidence work in the collection ingest transaction."""
import hashlib
import json

POLICY = 'event-observation-v1'
TYPED_POLICY = 'public-record-candidate-v2'


SCHEMA = """CREATE TABLE IF NOT EXISTS studio_evidence_tasks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    job_id TEXT NOT NULL REFERENCES studio_jobs(id),
    evidence_version_id TEXT NOT NULL REFERENCES studio_evidence_versions(id),
    policy TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','complete','rejected')),
    result TEXT NOT NULL DEFAULT '{}',
    UNIQUE(project_id,evidence_version_id,policy))"""


def admit(db, execution, evidence_id):
    """Caller owns the ingest transaction; material and work commit together."""
    execution._guard(db)
    project_id = execution.job['project_id']
    row = db.execute("SELECT 1 FROM studio_evidence_versions WHERE id=? AND project_id=? AND visibility='public'",
                     (evidence_id, project_id)).fetchone()
    if not row:
        raise ValueError('stage input outside public project')
    owner = execution.read_checkpoint('material_pipeline_v1') or {}
    policy = TYPED_POLICY if owner.get('owner') == 'materials' else POLICY
    key = hashlib.sha256(json.dumps([project_id, 'public', evidence_id, policy]).encode()).hexdigest()
    db.execute("INSERT OR IGNORE INTO studio_evidence_tasks (id,project_id,job_id,evidence_version_id,policy,status) "
               "VALUES (?,?,?,?,?,'pending')", (key, project_id, execution.job['id'], evidence_id, policy))
    return key
