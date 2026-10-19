"""Governed, gateway-neutral context and proposal contract.

No production Greenpine endpoint is configured here. An integration supplies a
gateway with a read-only context call and a pending-proposal call; it never gets
direct repository or database write authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from project_watch.repository import ProjectRepository


class BridgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalContextClaim(BridgeModel):
    subject: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=1200)
    source_ref: str = Field(min_length=1, max_length=160)
    source_version: str = Field(min_length=1, max_length=120)
    locator: str = Field(min_length=1, max_length=200)


class ExternalProjectContext(BridgeModel):
    external_project_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    claims: tuple[ExternalContextClaim, ...] = Field(max_length=50)
    fetched_at: datetime


class SourceMapping(BridgeModel):
    local_source_id: str = Field(min_length=1, max_length=128)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    external_source_id: str = Field(min_length=1, max_length=128)
    external_source_version: str = Field(min_length=1, max_length=120)


class PendingProposal(BridgeModel):
    contract_version: Literal[1] = 1
    target_system: str = Field(min_length=1, max_length=64)
    target_status: Literal["pending_review"] = "pending_review"
    external_project_id: str
    local_project_id: str
    local_observation_id: str
    local_observation_version: int
    local_source_id: str
    external_source_id: str
    external_source_version: str
    source_sha256: str
    source_locator: str
    subject: str
    statement: str
    reviewer_user_id: str
    idempotency_key: str


class PendingReceipt(BridgeModel):
    target_system: str
    external_proposal_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str
    status: Literal["pending_review"]


class GovernedGateway(Protocol):
    def read_context(self, external_project_id: str) -> ExternalProjectContext: ...

    def submit_pending(self, proposal: PendingProposal) -> PendingReceipt: ...


class GovernanceBoundaryError(RuntimeError):
    """The requested exchange lacks enough verified evidence or scope."""


class GovernedBridge:
    def __init__(
        self, repository: ProjectRepository, gateway: GovernedGateway, *, target_system: str
    ) -> None:
        if not 1 <= len(target_system) <= 64:
            raise ValueError("target system name must contain 1-64 characters")
        self.repository = repository
        self.gateway = gateway
        self.target_system = target_system

    def read_context(
        self, local_project_id: str, *, external_project_id: str
    ) -> ExternalProjectContext:
        """Return bounded external context without persisting or confirming it locally."""
        if self.repository.get_project(local_project_id) is None:
            raise GovernanceBoundaryError("local project not found")
        context = self.gateway.read_context(external_project_id)
        if context.external_project_id != external_project_id:
            raise GovernanceBoundaryError("gateway returned a different project")
        return context

    def prepare_proposal(
        self,
        local_project_id: str,
        observation_id: str,
        *,
        external_project_id: str,
        source_mapping: SourceMapping,
    ) -> PendingProposal:
        """Require verified local human review and an explicit source/version mapping."""
        observation = self.repository.get_observation(observation_id)
        if observation is None or observation.project_id != local_project_id:
            raise GovernanceBoundaryError("observation not found in project")
        if observation.status != "confirmed":
            raise GovernanceBoundaryError("only locally confirmed observations can be exported")
        review = self.repository.find_audit_event(
            local_project_id, subject_id=observation_id, action="candidate.confirmed"
        )
        if review is None or review.actor_kind != "verified_user" or not review.actor_ref:
            raise GovernanceBoundaryError("verified human review is required")
        source = self.repository.get_source(observation.source_id)
        if source is None or source.project_id != local_project_id:
            raise GovernanceBoundaryError("source not found in project")
        if (
            source.id != source_mapping.local_source_id
            or source.content_sha256 != source_mapping.expected_sha256
        ):
            raise GovernanceBoundaryError("source mapping or version has changed")
        if not external_project_id or len(external_project_id) > 128:
            raise GovernanceBoundaryError("external project mapping is required")
        fingerprint = json.dumps(
            [
                self.target_system,
                local_project_id,
                observation.id,
                observation.version,
                external_project_id,
                source_mapping.external_source_id,
                source_mapping.external_source_version,
                source.content_sha256,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return PendingProposal(
            target_system=self.target_system,
            external_project_id=external_project_id,
            local_project_id=local_project_id,
            local_observation_id=observation.id,
            local_observation_version=observation.version,
            local_source_id=source.id,
            external_source_id=source_mapping.external_source_id,
            external_source_version=source_mapping.external_source_version,
            source_sha256=source.content_sha256,
            source_locator=observation.source_locator,
            subject=observation.subject,
            statement=observation.statement,
            reviewer_user_id=review.actor_ref,
            idempotency_key=hashlib.sha256(fingerprint.encode("utf-8")).hexdigest(),
        )

    def submit_pending(self, proposal: PendingProposal) -> PendingReceipt:
        """Send a pending proposal; the target's human review remains mandatory."""
        if proposal.target_system != self.target_system:
            raise GovernanceBoundaryError("proposal targets another gateway")
        expected = self.prepare_proposal(
            proposal.local_project_id,
            proposal.local_observation_id,
            external_project_id=proposal.external_project_id,
            source_mapping=SourceMapping(
                local_source_id=proposal.local_source_id,
                expected_sha256=proposal.source_sha256,
                external_source_id=proposal.external_source_id,
                external_source_version=proposal.external_source_version,
            ),
        )
        if proposal != expected:
            raise GovernanceBoundaryError("proposal does not match reviewed local evidence")
        receipt = self.gateway.submit_pending(proposal)
        if (
            receipt.target_system != proposal.target_system
            or receipt.idempotency_key != proposal.idempotency_key
            or receipt.status != "pending_review"
        ):
            raise GovernanceBoundaryError("gateway returned an invalid pending receipt")
        return receipt
