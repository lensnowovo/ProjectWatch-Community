"""Located operating-table suggestions never become facts without review."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from project_watch.analysis.operating_mapping import (
    map_operating_fragments,
    validate_operating_suggestions,
)
from project_watch.analysis.operating_projection import screen_operating_metrics
from project_watch.api import create_app
from project_watch.domain import FragmentInput
from project_watch.extractors import _located_sheet_cells, extract_document
from project_watch.repository import ProjectRepository


def _rows(rows: list[list[object]]) -> list[FragmentInput]:
    headers: dict[int, str] = {}
    return [
        FragmentInput(
            index,
            f"sheet 产能统计 row {index}",
            " | ".join(_located_sheet_cells(values, index, headers)),
        )
        for index, values in enumerate(rows, start=1)
    ]


def test_explicit_operating_table_proposes_actual_and_forecast_separately() -> None:
    fragments = _rows(
        [
            ["产品", "指标", "单位", "2025年实际", "2026年预测"],
            ["连接器A", "设计产能", "万件", 120, 150],
            ["连接器A", "实际产量", "万件", 80, None],
            ["连接器A", "产能利用率", "%", 66.7, None],
        ]
    )
    mapped = map_operating_fragments("synthetic.xlsx", fragments)
    assert mapped.issues == ()
    assert [(item.metric, item.number, item.period, item.basis) for item in mapped.candidates] == [
        ("设计产能", Decimal("120"), "2025", "actual"),
        ("设计产能", Decimal("150"), "2026", "forecast"),
        ("实际产量", Decimal("80"), "2025", "actual"),
        ("产能利用率", Decimal("66.7"), "2025", "actual"),
    ]
    assert all(item.scope == "连接器A" for item in mapped.candidates)
    assert mapped.candidates[0].source_locator == fragments[1].locator


def test_missing_scope_unit_period_and_basis_are_exposed_not_guessed() -> None:
    fragments = _rows(
        [
            ["产品", "指标", "单位", "2025年实际", "2026年"],
            ["", "设计产能", "万件", 120, None],
            ["连接器A", "设计产能", "", 120, None],
            ["连接器A", "设计产能", "万件", None, 150],
        ]
    )
    mapped = map_operating_fragments("synthetic.xlsx", fragments)
    assert mapped.candidates == ()
    assert {issue.reason for issue in mapped.issues} == {
        "scope_missing",
        "unit_missing",
        "basis_missing",
    }


def test_forecast_output_and_invalid_utilization_are_not_candidates() -> None:
    mapped = map_operating_fragments(
        "synthetic.xlsx",
        _rows(
            [
                ["产品", "指标", "单位", "2025年实际", "2026年预测"],
                ["连接器A", "实际产量", "万件", None, 90],
                ["连接器A", "产能利用率", "%", 130, None],
            ]
        ),
    )
    assert mapped.candidates == ()
    assert {issue.reason for issue in mapped.issues} == {"output_not_actual", "invalid_utilization"}


def test_excel_percent_fraction_is_quarantined_without_cell_format() -> None:
    mapped = map_operating_fragments(
        "synthetic.xlsx",
        _rows(
            [
                ["产品", "指标", "单位", "2025年实际"],
                ["连接器A", "产能利用率", "%", 0.7],
            ]
        ),
    )
    assert mapped.candidates == ()
    assert [item.reason for item in mapped.issues] == ["percent_scale_ambiguous"]


def test_xlsx_explicit_percent_display_maps_fraction_to_reviewable_percent(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "产能统计"
    sheet.append(["产品", "指标", "单位", "2025年实际"])
    sheet.append(["连接器A", "产能利用率", "%", 0.7])
    sheet["D2"].number_format = "0.0%"
    workbook.save(path)
    extracted = extract_document(path)
    mapped = map_operating_fragments(path.name, list(extracted.fragments))
    assert "[format:percent]=0.7" in extracted.fragments[1].text
    assert [(item.metric, item.number, item.unit, item.period) for item in mapped.candidates] == [
        ("产能利用率", Decimal("70.0"), "%", "2025")
    ]
    assert mapped.issues == ()


def test_literal_percent_or_percentage_on_quantity_does_not_create_fact(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "产能统计"
    sheet.append(["产品", "指标", "单位", "2025年实际"])
    sheet.append(["连接器A", "产能利用率", "%", 0.7])
    sheet["D2"].number_format = r"0.0\%"
    sheet.append(["连接器A", "设计产能", "万件", 0.7])
    sheet["D3"].number_format = "0.0%"
    workbook.save(path)
    extracted = extract_document(path)
    mapped = map_operating_fragments(path.name, list(extracted.fragments))
    assert mapped.candidates == ()
    assert {item.reason for item in mapped.issues} == {
        "percent_scale_ambiguous",
        "format_unit_conflict",
    }


def test_other_documents_and_unlabeled_rows_do_not_create_candidates() -> None:
    fragments = _rows([["连接器A", "设计产能", "万件", 120]])
    assert map_operating_fragments("synthetic.xlsx", fragments).candidates == ()
    assert map_operating_fragments("synthetic.docx", fragments).candidates == ()


def test_intake_persists_unconfirmed_candidate_and_exposes_mapping(tmp_path: Path) -> None:
    path = tmp_path / "watch.sqlite3"
    repository = ProjectRepository(path)
    project = repository.create_project("Synthetic Operating Project")
    source = repository.add_source(
        project.id, kind="document", reference="synthetic.xlsx", content_sha256="a" * 64
    )
    fragments = _rows(
        [
            ["产品", "指标", "单位", "2025年实际"],
            ["连接器A", "设计产能", "万件", 120],
            ["连接器A", "实际产量", "", 80],
        ]
    )
    candidates = map_operating_fragments(source.reference, fragments).candidates
    repository.enqueue_intake(project.id, source.id)
    job = repository.claim_next_intake()
    repository.complete_intake(job.id, fragments, native_operating_candidates=candidates)
    observations = repository.list_observations(project.id)
    assert len(observations) == 1
    assert (observations[0].statement, observations[0].status, observations[0].scope) == (
        "设计产能",
        "candidate",
        "连接器A",
    )
    assert screen_operating_metrics(observations, {source.id: source})["actual_count"] == 0
    confirmed = repository.confirm_observation(
        observations[0].id, reviewer="synthetic-reviewer", expected_version=1
    )
    assert screen_operating_metrics([confirmed], {source.id: source})["actual_count"] == 1
    client = TestClient(create_app(path))
    result = client.get(f"/api/projects/{project.id}/sources/{source.id}/operating-mapping")
    assert result.status_code == 200
    assert result.json() == {
        "candidate_count": 1,
        "truncated_count": 0,
        "issues": [{"source_locator": fragments[2].locator, "reason": "unit_missing"}],
    }


def test_tampered_operating_candidate_rolls_back_atomic_intake(tmp_path: Path) -> None:
    repository = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repository.create_project("Synthetic Operating Rollback")
    source = repository.add_source(
        project.id, kind="document", reference="synthetic.xlsx", content_sha256="b" * 64
    )
    fragments = _rows(
        [
            ["产品", "指标", "单位", "2025年实际"],
            ["连接器A", "设计产能", "万件", 120],
        ]
    )
    suggestion = map_operating_fragments(source.reference, fragments).candidates[0]
    with pytest.raises(ValueError, match="operating suggestion"):
        validate_operating_suggestions(
            source.reference, fragments, (replace(suggestion, number=Decimal("999")),)
        )
    repository.enqueue_intake(project.id, source.id)
    job = repository.claim_next_intake()
    with pytest.raises(ValueError, match="operating suggestion"):
        repository.complete_intake(
            job.id,
            fragments,
            native_operating_candidates=(replace(suggestion, number=Decimal("999")),),
        )
    assert repository.list_fragments(source.id) == []
    assert repository.list_observations(project.id) == []
