import pytest
from pydantic import ValidationError
from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.evidence_store import EvidenceStore
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart, LocatedRef


def test_quotes_are_exact_and_project_bound(tmp_path):
    service, pid = make_service(tmp_path / "quote.sqlite3")
    evidence = EvidenceStore(service.store)
    saved = evidence.save(sample(pid, "收入 100 万元"))
    ref = LocatedRef(evidence_version_id=saved["id"], part_index=0, start=3, end=6, quote="100")
    assert evidence.resolve(pid, ref)["locator"] == "original fragment 1"
    with pytest.raises(ValueError):
        evidence.resolve(pid, ref.model_copy(update={"quote": "200"}))
    with pytest.raises(KeyError):
        evidence.resolve("other-project", ref)
    with pytest.raises(ValueError):
        evidence.resolve(pid, ref.model_copy(update={"end": 999}))


def test_summary_cannot_claim_full_or_pdf_coordinates():
    with pytest.raises(ValidationError):
        EvidenceInput(project_id="p", source_key="summary", extraction_version="search-v1", origin="media",
                      read_scope="full", source_type="search_summary", parts=[
                          EvidencePart(locator_kind="stored_text", locator="chars 0:3", text="abc"),
                      ])
    with pytest.raises(ValidationError):
        EvidenceInput(project_id="p", source_key="summary", extraction_version="search-v1", origin="media",
                      read_scope="summary", source_type="search_summary", parts=[
                          EvidencePart(locator_kind="pdf_page", locator="page 3", text="abc"),
                      ])


def test_twin_reads_use_donor_version_not_original_link(tmp_path):
    service, pid = make_service(tmp_path / "twin.sqlite3")
    evidence = EvidenceStore(service.store)
    donor = evidence.save(sample(pid, "Original donor text"))
    twin = evidence.link_twin(pid, "another-public-link", donor["id"])
    assert twin["evidence_version_id"] == donor["id"]
    assert twin["basis"] == "donor_text"
    assert twin["source_key"] == "another-public-link"
    with pytest.raises(KeyError):
        evidence.link_twin("foreign-project", "link", donor["id"])


def test_table_locator_retains_scope_period_and_currency_as_metadata(tmp_path):
    service, pid = make_service(tmp_path / "table.sqlite3")
    evidence = EvidenceStore(service.store)
    saved = evidence.save(EvidenceInput(project_id=pid, source_key="table", extraction_version="xlsx-v1",
                                        origin="company_self", read_scope="fragmented", parts=[
        EvidencePart(locator_kind="cell_range", locator="Sheet 财务!B7:C7", text="2025 实际收入 100 万元 CNY 合并"),
    ]))
    ref = LocatedRef(evidence_version_id=saved["id"], part_index=0, start=0,
                     end=len(saved["parts"][0]["text"]), quote=saved["parts"][0]["text"])
    resolved = evidence.resolve(pid, ref)
    assert resolved["locator"] == "Sheet 财务!B7:C7"
    assert "2025" in resolved["quote"] and "CNY 合并" in resolved["quote"]
