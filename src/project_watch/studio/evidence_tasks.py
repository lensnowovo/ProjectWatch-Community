"""Durable material-stage work under its existing parent job and call budget.

These are candidate observations, never admitted news or semantic approval.
No independent scheduler, lease, transport or automatic retry is introduced.
"""
import hashlib
import json
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

POLICY = 'event-observation-v1'
TYPED_POLICY = 'public-record-candidate-v2'
DEFERRED = 'extract_deferred:'


class MaterialDeferred(Exception):
    """Preserve evidence after a local limit or settled incomplete response."""

    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(receipt['code'])


SCHEMA = """CREATE TABLE IF NOT EXISTS studio_evidence_tasks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    job_id TEXT NOT NULL REFERENCES studio_jobs(id),
    evidence_version_id TEXT NOT NULL REFERENCES studio_evidence_versions(id),
    policy TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','complete','rejected')),
    result TEXT NOT NULL DEFAULT '{}',
    UNIQUE(project_id,evidence_version_id,policy))"""


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=3000)
    event_at: str | None = Field(default=None, min_length=10, max_length=10)
    refs: list[Annotated[str, Field(min_length=64, max_length=64)]] = Field(min_length=1, max_length=20)

    @field_validator('event_at')
    @classmethod
    def explicit_date(cls, value):
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError('explicit ISO date required')
        return value


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    observations: list[Observation] = Field(max_length=50)


class RecordCandidate(Observation):
    importance: Literal['major', 'notable', 'routine'] | None = None
    importance_reason: str = Field(default='', max_length=600)
    kind: Literal['business', 'product', 'person', 'founded', 'location', 'financing', 'milestone',
                  'company', 'competitor', 'industry', 'policy', 'peer', 'relation', 'paper', 'patent']
    research_relation: Literal['author_affiliation', 'applicant', 'technical_relevance', 'unverified'] | None = None
    label: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=600)
    refs: list[Annotated[str, Field(min_length=64, max_length=64)]] = Field(min_length=1, max_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    reason: str = Field(default='', max_length=600)
    axis: str = Field(default='', max_length=30)
    query: str = Field(default='', max_length=300)
    relation_type: str = Field(default='', max_length=30)
    relation_target: str = Field(default='', max_length=120)
    relation_aliases: list[str] = Field(default_factory=list, max_length=8)

    @field_validator('subject')
    @classmethod
    def explicit_subject(cls, value):
        if not value.strip():
            raise ValueError('explicit subject required')
        return value.strip()


class RecordProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    observations: list[RecordCandidate] = Field(max_length=50)


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
