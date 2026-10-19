"""Only synthetic fake gateways exercise the institutional exchange contract."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from project_watch.governed_bridge import (
    ExternalContextClaim,
    ExternalProjectContext,
    GovernanceBoundaryError,
    GovernedBridge,
    PendingProposal,
    PendingReceipt,
    SourceMapping,
)
from project_watch.repository import ProjectRepository


class FakeGateway:
    def __init__(self) -> None:
        self.submissions: dict[str, PendingProposal] = {}

    def read_context(self, external_project_id: str) -> ExternalProjectContext:
        return ExternalProjectContext(
            external_project_id=external_project_id,
            display_name="Synthetic Institutional Project",
            claims=(
                ExternalContextClaim(
                    subject="经营情况",
                    summary="Synthetic revenue requires human validation.",
                    source_ref="synthetic-report",
                    source_version="v1",
                    locator="page 2",
                ),
            ),
            fetched_at=datetime.now(UTC),
        )

    def submit_pending(self, proposal: PendingProposal) -> PendingReceipt:
        self.submissions.setdefault(proposal.idempotency_key, proposal)
        return PendingReceipt(
            target_system=proposal.target_system,
            external_proposal_id="synthetic-pending-001",
            idempotency_key=proposal.idempotency_key,
            status="pending_review",
        )


def _fixture(tmp_path: Path):
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Synthetic Watch")
    source = repo.add_source(
        project.id, kind="document", reference="synthetic-bp.txt", content_sha256="a" * 64
    )
    mapping = SourceMapping(
        local_source_id=source.id,
        expected_sha256=source.content_sha256,
        external_source_id="synthetic-document-id",
        external_source_version="v1",
    )
    gateway = FakeGateway()
    return (
        repo,
        project,
        source,
        mapping,
        gateway,
        GovernedBridge(repo, gateway, target_system="synthetic-institution-db"),
    )


def test_read_only_context_does_not_create_local_facts(tmp_path: Path) -> None:
    repo, project, _, _, gateway, bridge = _fixture(tmp_path)
    context = bridge.read_context(project.id, external_project_id="external-project-001")
    assert context.claims[0].locator == "page 2"
    assert repo.list_observations(project.id) == []
    assert gateway.submissions == {}
    with pytest.raises(GovernanceBoundaryError, match="local project"):
        bridge.read_context("not-local", external_project_id="external-project-001")


def test_pending_export_requires_verified_review_and_explicit_source_version(
    tmp_path: Path,
) -> None:
    repo, project, source, mapping, gateway, bridge = _fixture(tmp_path)
    candidate = repo.add_observation(
        project.id,
        source_id=source.id,
        subject="经营情况",
        statement="Synthetic revenue claim; verify before use.",
        source_locator="page 2",
    )
    with pytest.raises(GovernanceBoundaryError, match="locally confirmed"):
        bridge.prepare_proposal(
            project.id,
            candidate.id,
            external_project_id="external-project-001",
            source_mapping=mapping,
        )
    repo.confirm_observation(candidate.id, reviewer="claimed-reviewer", expected_version=1)
    with pytest.raises(GovernanceBoundaryError, match="verified human review"):
        bridge.prepare_proposal(
            project.id,
            candidate.id,
            external_project_id="external-project-001",
            source_mapping=mapping,
        )
    verified = repo.add_observation(
        project.id,
        source_id=source.id,
        subject="产品情况",
        statement="Synthetic product claim; source and reviewer recorded.",
        source_locator="page 3",
    )
    repo.confirm_observation(
        verified.id, reviewer="synthetic-user-id", expected_version=1, reviewer_verified=True
    )
    wrong_mapping = mapping.model_copy(update={"expected_sha256": "b" * 64})
    with pytest.raises(GovernanceBoundaryError, match="source mapping"):
        bridge.prepare_proposal(
            project.id,
            verified.id,
            external_project_id="external-project-001",
            source_mapping=wrong_mapping,
        )
    proposal = bridge.prepare_proposal(
        project.id,
        verified.id,
        external_project_id="external-project-001",
        source_mapping=mapping,
    )
    forged = proposal.model_copy(update={"statement": "Forged assertion"})
    with pytest.raises(GovernanceBoundaryError, match="does not match"):
        bridge.submit_pending(forged)
    assert gateway.submissions == {}
    assert proposal.reviewer_user_id == "synthetic-user-id"
    assert proposal.source_sha256 == source.content_sha256
    assert proposal.source_locator == "page 3"
    assert proposal.target_status == "pending_review"
    assert (
        proposal.idempotency_key
        == bridge.prepare_proposal(
            project.id,
            verified.id,
            external_project_id="external-project-001",
            source_mapping=mapping,
        ).idempotency_key
    )
    first = bridge.submit_pending(proposal)
    second = bridge.submit_pending(proposal)
    assert first == second
    assert len(gateway.submissions) == 1
    assert repo.list_observations(project.id, status="confirmed")


def test_wrong_project_and_invalid_gateway_receipt_fail_closed(tmp_path: Path) -> None:
    repo, project, source, mapping, gateway, bridge = _fixture(tmp_path)
    other = repo.create_project("Other Local Project")
    confirmed = repo.add_observation(
        project.id,
        source_id=source.id,
        subject="项目",
        statement="Synthetic claim",
        source_locator="p1",
    )
    repo.confirm_observation(
        confirmed.id, reviewer="synthetic-user-id", expected_version=1, reviewer_verified=True
    )
    with pytest.raises(GovernanceBoundaryError, match="observation not found"):
        bridge.prepare_proposal(
            other.id,
            confirmed.id,
            external_project_id="external-project-001",
            source_mapping=mapping,
        )
    proposal = bridge.prepare_proposal(
        project.id,
        confirmed.id,
        external_project_id="external-project-001",
        source_mapping=mapping,
    )

    def invalid_receipt(_proposal: PendingProposal) -> PendingReceipt:
        return PendingReceipt(
            target_system=proposal.target_system,
            external_proposal_id="wrong-key",
            idempotency_key="not-the-submitted-key",
            status="pending_review",
        )

    gateway.submit_pending = invalid_receipt
    with pytest.raises(GovernanceBoundaryError, match="invalid pending receipt"):
        bridge.submit_pending(proposal)
