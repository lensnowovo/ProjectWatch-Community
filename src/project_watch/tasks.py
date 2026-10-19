"""Compatibility entry point for bounded proposals and recorded coverage."""
from project_watch.domain import RESEARCH_TOPICS as RESEARCH_TOPICS
from project_watch.evidence_proposals import (
    EvidenceConflictError as EvidenceConflictError,
)
from project_watch.evidence_proposals import (
    TaskService as TaskService,
)
from project_watch.evidence_proposals import (
    coverage_outcome as coverage_outcome,
)
