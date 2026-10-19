"""Identity review is an append-only human decision, never inferred from a name."""

from pathlib import Path

import pytest

from project_watch.domain import FragmentInput
from project_watch.repository import IdentityConflictError, ProjectRepository


def test_identity_review_requires_located_same_project_evidence_and_preserves_history(
    tmp_path: Path,
) -> None:
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Example Company")
    foreign = repo.create_project("Another Company")
    foreign_source = repo.add_source(
        foreign.id, kind="document", reference="foreign.txt", content_sha256="1" * 64
    )
    with pytest.raises(ValueError, match="selected project"):
        repo.verify_project_identity(
            project.id,
            legal_name="Example Company Limited",
            registration_code=None,
            evidence_source_id=foreign_source.id,
            evidence_locator="page:1",
            evidence_url=None,
            note="Checked the company title",
            expected_version=0,
            actor_user_id="reviewer-1",
        )
    assert repo.get_project(project.id).identity_status == "unverified"

    source = repo.add_source(
        project.id, kind="document", reference="company.txt", content_sha256="2" * 64
    )
    job = repo.enqueue_intake(project.id, source.id)
    claimed = repo.claim_next_intake()
    assert claimed is not None and claimed.id == job.id
    repo.complete_intake(
        job.id,
        fragments=[FragmentInput(1, "page:1", "Example Company Limited")],
    )
    reviewed = repo.verify_project_identity(
        project.id,
        legal_name="Example Company Limited",
        registration_code="91300000123456789X",
        evidence_source_id=source.id,
        evidence_locator="page:1",
        evidence_url=None,
        note="Legal name appears on the uploaded company profile",
        expected_version=0,
        actor_user_id="reviewer-1",
    )
    assert reviewed.version == 1
    assert reviewed.action == "verify"
    assert repo.get_project(project.id).identity_status == "verified"
    assert repo.get_identity_review(project.id) == reviewed
    assert [row.action for row in repo.list_identity_reviews(project.id)] == ["verify"]

    with pytest.raises(IdentityConflictError):
        repo.reopen_project_identity(
            project.id,
            note="The matching evidence needs another look",
            expected_version=0,
            actor_user_id="reviewer-2",
        )
    reopened = repo.reopen_project_identity(
        project.id,
        note="The matching evidence needs another look",
        expected_version=1,
        actor_user_id="reviewer-2",
    )
    assert reopened.version == 2
    assert repo.get_project(project.id).identity_status == "unverified"
    assert [row.action for row in repo.list_identity_reviews(project.id)] == ["reopen", "verify"]
    events = [
        row for row in repo.list_audit_events(project.id) if row.action.startswith("identity.")
    ]
    assert [row.action for row in events] == ["identity.verified", "identity.reopened"]
    assert [row.actor_ref for row in events] == ["reviewer-1", "reviewer-2"]


@pytest.mark.parametrize(
    "url",
    ["http://example.org/company", "https://localhost/", "https://user:pw@example.org/"],
)
def test_identity_review_rejects_unsafe_public_citations(tmp_path: Path, url: str) -> None:
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Example")
    with pytest.raises(ValueError, match="HTTPS"):
        repo.verify_project_identity(
            project.id,
            legal_name="Example Company Limited",
            registration_code=None,
            evidence_source_id=None,
            evidence_locator=None,
            evidence_url=url,
            note="Checked the legal name against the public page",
            expected_version=0,
            actor_user_id="reviewer-1",
        )
