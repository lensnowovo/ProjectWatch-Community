from dataclasses import replace
from pathlib import Path

import pytest

from project_watch.analysis.financial_mapping import map_financial_fragments
from project_watch.analysis.financial_projection import screen_confirmed_financials
from project_watch.domain import FragmentInput
from project_watch.native_intake import NativeCandidate
from project_watch.repository import IntakeConflictError, ProjectRepository


def _source(repository: ProjectRepository, name: str, digest: str):
    project = repository.create_project(name)
    source = repository.add_source(
        project.id, kind="document", reference="synthetic.txt", content_sha256=digest
    )
    return project, source


def test_enqueue_claim_and_complete_located_fragments(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project, source = _source(repository, "Synthetic Project", "a" * 64)

    first = repository.enqueue_intake(project.id, source.id)
    repeated = repository.enqueue_intake(project.id, source.id)
    claimed = repository.claim_next_intake()

    assert repeated.id == first.id
    assert claimed.id == first.id
    assert claimed.status == "processing"
    assert claimed.attempts == 1
    assert repository.claim_next_intake() is None

    done = repository.complete_intake(
        claimed.id, [FragmentInput(ordinal=1, locator="line 1", text="Synthetic revenue")]
    )

    assert done.status == "completed"
    assert repository.list_intake(project.id) == [done]
    assert [(row.locator, row.text) for row in repository.list_fragments(source.id)] == [
        ("line 1", "Synthetic revenue")
    ]
    assert repository.list_observations(project.id, status="confirmed") == []


def test_failed_and_needs_ocr_are_distinct_states(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project, first_source = _source(repository, "Synthetic Project", "a" * 64)
    second_source = repository.add_source(
        project.id, kind="document", reference="scan.pdf", content_sha256="b" * 64
    )
    first = repository.enqueue_intake(project.id, first_source.id)
    second = repository.enqueue_intake(project.id, second_source.id)

    assert repository.claim_next_intake().id == first.id
    failed = repository.fail_intake(first.id, error_code="invalid_document")
    assert failed.status == "failed"
    assert failed.error_code == "invalid_document"
    assert repository.claim_next_intake().id == second.id
    ocr = repository.complete_intake(second.id, [], needs_ocr=True)
    assert ocr.status == "needs_ocr"
    assert ocr.error_code is None


def test_restart_recovers_processing_job(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    repository = ProjectRepository(path)
    project, source = _source(repository, "Synthetic Project", "a" * 64)
    queued = repository.enqueue_intake(project.id, source.id)
    assert repository.claim_next_intake().status == "processing"

    reopened = ProjectRepository(path)
    assert reopened.recover_interrupted_jobs() == 1
    claimed_again = reopened.claim_next_intake()

    assert claimed_again.id == queued.id
    assert claimed_again.attempts == 2


def test_failed_job_can_be_retried_without_losing_history(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project, source = _source(repository, "Synthetic Project", "a" * 64)
    repository.enqueue_intake(project.id, source.id)
    claimed = repository.claim_next_intake()
    repository.fail_intake(claimed.id, error_code="parse_failed")

    retried = repository.retry_failed_intake(claimed.id)

    assert retried.status == "queued"
    assert retried.attempts == 1
    assert repository.claim_next_intake().attempts == 2


def test_cross_project_source_cannot_be_enqueued(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    _first, source = _source(repository, "First", "a" * 64)
    other = repository.create_project("Second")

    with pytest.raises(ValueError, match="another project"):
        repository.enqueue_intake(other.id, source.id)
    assert repository.list_intake(other.id) == []


def test_completion_requires_processing_job_and_bounded_fragment(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project, source = _source(repository, "Synthetic Project", "a" * 64)
    job = repository.enqueue_intake(project.id, source.id)

    with pytest.raises(IntakeConflictError):
        repository.complete_intake(job.id, [FragmentInput(1, "line 1", "text")])
    repository.claim_next_intake()
    with pytest.raises(ValueError, match="fragment text"):
        repository.complete_intake(job.id, [FragmentInput(1, "line 1", "x" * 20001)])
    assert repository.list_fragments(source.id) == []


def test_invalid_native_candidate_rolls_back_fragments_and_completion(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project, source = _source(repository, "Synthetic Project", "a" * 64)
    repository.enqueue_intake(project.id, source.id)
    job = repository.claim_next_intake()

    with pytest.raises(ValueError, match="source excerpt"):
        repository.complete_intake(
            job.id,
            [FragmentInput(1, "line 1", "营业收入仍需核实")],
            native_candidates=(NativeCandidate("财务表现", "收入已核实", "line 1"),),
        )

    assert repository.list_fragments(source.id) == []
    assert repository.list_observations(project.id) == []
    assert repository.list_intake(project.id)[0].status == "processing"


def test_financial_sheet_creates_reviewable_numeric_candidate_only(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Finance")
    source = repository.add_source(
        project.id, kind="document", reference="synthetic.xlsx", content_sha256="f" * 64
    )
    fragments = [
        FragmentInput(1, "sheet 合并利润表 row 1", "A1=单位：万元"),
        FragmentInput(
            2,
            "sheet 合并利润表 row 2",
            "A2=营业收入 | B2[header:2025年实际]=120",
        ),
    ]
    suggestion = map_financial_fragments(source.reference, fragments).candidates[0]
    repository.enqueue_intake(project.id, source.id)
    job = repository.claim_next_intake()
    repository.complete_intake(job.id, fragments, native_financial_candidates=(suggestion,))
    candidate = repository.list_observations(project.id)[0]
    assert (candidate.statement, candidate.number, candidate.period, candidate.basis) == (
        "营业收入",
        120,
        "2025",
        "actual",
    )
    assert candidate.status == "candidate"
    assert screen_confirmed_financials([candidate], [source])["accepted_count"] == 0
    confirmed = repository.confirm_observation(
        candidate.id, reviewer="synthetic-reviewer", expected_version=1
    )
    assert screen_confirmed_financials([confirmed], [source])["accepted_count"] == 1


def test_side_by_side_balance_sheet_persists_separate_unconfirmed_candidates(
    tmp_path: Path,
) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Balance Sheet")
    source = repository.add_source(
        project.id, kind="document", reference="synthetic.xlsx", content_sha256="b" * 64
    )
    fragments = [
        FragmentInput(1, "sheet 合并资产负债表 row 1", "A1=单位：万元"),
        FragmentInput(
            2,
            "sheet 合并资产负债表 row 2",
            "A2=货币资金 | B2[header:2025年实际]=100 | D2=短期借款 | E2[header:2025年实际]=50",
        ),
    ]
    suggestions = map_financial_fragments(source.reference, fragments).candidates
    repository.enqueue_intake(project.id, source.id)
    job = repository.claim_next_intake()

    repository.complete_intake(job.id, fragments, native_financial_candidates=suggestions)

    observations = repository.list_observations(project.id)
    assert {(item.statement, item.number, item.source_locator) for item in observations} == {
        ("货币资金", 100, "sheet 合并资产负债表 row 2"),
        ("短期借款", 50, "sheet 合并资产负债表 row 2"),
    }
    assert all(item.status == "candidate" for item in observations)
    assert screen_confirmed_financials(observations, [source])["accepted_count"] == 0


def test_trial_balance_persists_only_closing_balance_as_unconfirmed_candidate(
    tmp_path: Path,
) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Trial Balance")
    source = repository.add_source(
        project.id, kind="document", reference="synthetic-trial.xlsx", content_sha256="c" * 64
    )
    fragments = [
        FragmentInput(1, "sheet 合并科目余额表 row 1", "A1=单位：元"),
        FragmentInput(
            2,
            "sheet 合并科目余额表 row 2",
            "A2=应收账款 | B2[header:2025年期初余额]=80"
            " | C2[header:2025年期末余额]=120"
            " | D2[header:2025年借方发生额]=90",
        ),
    ]
    suggestions = map_financial_fragments(source.reference, fragments).candidates
    repository.enqueue_intake(project.id, source.id)
    job = repository.claim_next_intake()

    repository.complete_intake(job.id, fragments, native_financial_candidates=suggestions)

    observations = repository.list_observations(project.id)
    assert [
        (item.statement, item.number, item.source_locator, item.status) for item in observations
    ] == [("应收账款", 120, "sheet 合并科目余额表 row 2", "candidate")]
    assert screen_confirmed_financials(observations, [source])["accepted_count"] == 0


def test_mixed_new_material_flags_each_located_topic_once(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Mixed Update")
    existing: dict[str, str] = {}
    for topic, digest, statement in (
        ("财务表现", "a" * 64, "旧收入判断"),
        ("产品与技术", "b" * 64, "旧产品判断"),
    ):
        source = repository.add_source(
            project.id, kind="document", reference=f"{topic}.txt", content_sha256=digest
        )
        repository.enqueue_intake(project.id, source.id)
        job = repository.claim_next_intake()
        repository.complete_intake(
            job.id,
            [FragmentInput(1, "line 1", statement)],
            native_topic=topic,
            native_topic_basis="content",
        )
        observation = repository.add_observation(
            project.id,
            source_id=source.id,
            subject=topic,
            statement=statement,
            source_locator="line 1",
        )
        repository.confirm_observation(
            observation.id, reviewer="synthetic-reviewer", expected_version=1
        )
        existing[topic] = observation.id

    updated = repository.add_source(
        project.id, kind="document", reference="mixed-update.txt", content_sha256="c" * 64
    )
    fragments = [
        FragmentInput(1, "line 1", "营业收入有变化。"),
        FragmentInput(2, "line 2", "产品路线有变化。"),
    ]
    repository.enqueue_intake(project.id, updated.id)
    job = repository.claim_next_intake()
    repository.complete_intake(
        job.id,
        fragments,
        native_topic="财务表现",
        native_topic_basis="content",
        native_candidates=(
            NativeCandidate("财务表现", fragments[0].text, fragments[0].locator),
            NativeCandidate("产品与技术", fragments[1].text, fragments[1].locator),
        ),
    )

    impacts = repository.list_review_impacts(project.id)
    assert {impact.observation_id for impact in impacts} == set(existing.values())
    assert len(impacts) == 2
    assert all(impact.trigger_source_id == updated.id for impact in impacts)


def test_forged_financial_candidate_rolls_back_intake(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Finance")
    source = repository.add_source(
        project.id, kind="document", reference="synthetic.xlsx", content_sha256="f" * 64
    )
    fragments = [
        FragmentInput(1, "sheet 合并利润表 row 1", "A1=单位：万元"),
        FragmentInput(
            2,
            "sheet 合并利润表 row 2",
            "A2=营业收入 | B2[header:2025年实际]=120",
        ),
    ]
    valid = map_financial_fragments(source.reference, fragments).candidates[0]
    repository.enqueue_intake(project.id, source.id)
    job = repository.claim_next_intake()
    with pytest.raises(ValueError, match="financial suggestion"):
        repository.complete_intake(
            job.id, fragments, native_financial_candidates=(replace(valid, number=130),)
        )
    assert repository.list_fragments(source.id) == []
    assert repository.list_observations(project.id) == []


def test_material_index_is_scoped_and_committed_with_intake(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project, source = _source(repository, "Synthetic Project", "a" * 64)
    other, _ = _source(repository, "Another Project", "b" * 64)
    repository.enqueue_intake(project.id, source.id)
    job = repository.claim_next_intake()

    assert repository.list_sources(project.id)[0].topic is None
    assert repository.list_sources(other.id)[0].topic is None
    with pytest.raises(ValueError, match="index"):
        repository.complete_intake(
            job.id,
            [FragmentInput(1, "line 1", "财务报表")],
            native_topic="投资结论",
            native_topic_basis="filename",
        )
    assert repository.list_fragments(source.id) == []

    repository.complete_intake(
        job.id,
        [FragmentInput(1, "line 1", "财务报表")],
        native_topic="财务表现",
        native_topic_basis="content",
    )
    assert repository.list_sources(project.id)[0].topic == "财务表现"
    assert repository.list_sources(project.id)[0].topic_basis == "content"
    assert repository.list_sources(other.id)[0].topic is None
    reopened = ProjectRepository(tmp_path / "watch.sqlite3")
    assert reopened.list_sources(project.id)[0].topic == "财务表现"


def test_new_material_flags_only_prior_confirmed_observations_in_same_topic(
    tmp_path: Path,
) -> None:
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project, first = _source(repo, "Incremental Review", "a" * 64)
    other, other_source = _source(repo, "Other Project", "f" * 64)

    repo.enqueue_intake(project.id, first.id)
    first_job = repo.claim_next_intake()
    repo.complete_intake(
        first_job.id,
        [FragmentInput(1, "line 1", "营业收入有待核对")],
        native_topic="财务表现",
        native_topic_basis="content",
    )
    observation = repo.add_observation(
        project.id,
        source_id=first.id,
        subject="收入判断",
        statement="收入仍需核对",
        source_locator="line 1",
    )
    repo.confirm_observation(observation.id, reviewer="synthetic-reviewer", expected_version=1)
    unconfirmed = repo.add_observation(
        project.id,
        source_id=first.id,
        subject="财务表现",
        statement="未经确认的候选",
        source_locator="line 1",
    )
    assert unconfirmed.status == "candidate"

    newer = repo.add_source(
        project.id, kind="document", reference="新财务.txt", content_sha256="b" * 64
    )
    repo.enqueue_intake(project.id, newer.id)
    newer_job = repo.claim_next_intake()
    repo.complete_intake(
        newer_job.id,
        [FragmentInput(1, "line 1", "营业收入变化")],
        native_topic="财务表现",
        native_topic_basis="filename_and_content",
    )
    impacts = repo.list_review_impacts(project.id)
    assert len(impacts) == 1
    assert impacts[0].trigger_source_id == newer.id
    assert impacts[0].observation_id == observation.id
    assert impacts[0].status == "pending"
    assert impacts[0].topic == "财务表现"
    assert repo.list_review_impacts(other.id) == []
    assert repo.list_sources(other.id)[0].id == other_source.id

    product = repo.add_source(
        project.id, kind="document", reference="新产品.txt", content_sha256="c" * 64
    )
    repo.enqueue_intake(project.id, product.id)
    product_job = repo.claim_next_intake()
    repo.complete_intake(
        product_job.id,
        [FragmentInput(1, "line 1", "产品升级")],
        native_topic="产品与技术",
        native_topic_basis="filename_and_content",
    )
    assert len(repo.list_review_impacts(project.id)) == 1
    scan = repo.add_source(
        project.id, kind="document", reference="财务扫描.pdf", content_sha256="d" * 64
    )
    repo.enqueue_intake(project.id, scan.id)
    scan_job = repo.claim_next_intake()
    repo.complete_intake(
        scan_job.id,
        [],
        needs_ocr=True,
        native_topic="财务表现",
        native_topic_basis="filename",
    )
    assert len(repo.list_review_impacts(project.id)) == 1
    reviewed = repo.resolve_review_impact(
        project.id,
        impacts[0].id,
        resolution="follow_up",
        note="已核对新材料，订单口径仍需访谈。",
        reviewer="synthetic-reviewer",
    )
    assert reviewed.status == "reviewed"
    assert reviewed.resolution == "follow_up"
    assert reviewed.note == "已核对新材料，订单口径仍需访谈。"
    assert repo.get_observation(observation.id).status == "confirmed"
    assert any(event.action == "impact.reviewed" for event in repo.list_audit_events(project.id))
    with pytest.raises(Exception, match="already reviewed"):
        repo.resolve_review_impact(
            project.id,
            impacts[0].id,
            resolution="unchanged",
            note="重复提交",
            reviewer="synthetic-reviewer",
        )
