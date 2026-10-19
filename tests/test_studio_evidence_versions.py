from test_studio_agent_instance import make_service

from project_watch.studio.evidence_store import EvidenceStore
from project_watch.studio.monitoring_contracts import EvidenceInput, EvidencePart


def sample(project_id, text="2025 年收入 100 万元", extraction="parser-v1"):
    return EvidenceInput(project_id=project_id, source_key="synthetic-source", extraction_version=extraction,
                         read_scope="partial", origin="company_self", parts=[
                             EvidencePart(locator_kind="fragment", locator="original fragment 1", text=text),
                         ])


def test_evidence_idempotency_history_and_method_version(tmp_path):
    service, pid = make_service(tmp_path / "evidence.sqlite3")
    evidence = EvidenceStore(service.store)
    first = evidence.save(sample(pid))
    assert evidence.save(sample(pid))["id"] == first["id"]
    revised = evidence.save(sample(pid, "2025 年收入 80 万元"))
    assert revised["id"] != first["id"]
    rerun = evidence.save(sample(pid, extraction="ocr-v2"))
    assert rerun["id"] != first["id"]
    assert evidence.get(pid, first["id"])["parts"][0]["text"] == "2025 年收入 100 万元"
    assert len(evidence.versions(pid, "synthetic-source")) == 3


def test_excerpt_keyset_snapshot_keeps_new_versions_out_of_current_page(tmp_path):
    service, pid = make_service(tmp_path / "pages.sqlite3")
    evidence = EvidenceStore(service.store)
    first = evidence.save(sample(pid, "first"))
    second = evidence.save(sample(pid, "second"))
    page = evidence.page(pid, limit=1)
    assert page["items"][0]["id"] == first["id"]
    evidence.save(sample(pid, "third"))
    final = evidence.page(pid, limit=1, cursor=page["next_cursor"])
    assert final["items"][0]["id"] == second["id"]
    assert final["next_cursor"] is None


def test_core_fragments_adapter_retains_real_locator_and_source(tmp_path):
    from project_watch.domain import FragmentInput

    service, pid = make_service(tmp_path / "fragments.sqlite3")
    from project_watch.repository import ProjectRepository

    repo = ProjectRepository(service.store.db_path)
    source = repo.add_source(pid, kind="document", reference="synthetic.txt", content_sha256="a" * 64)
    repo.enqueue_intake(pid, source.id)
    job = repo.claim_next_intake()
    repo.complete_intake(job.id, [FragmentInput(ordinal=1, locator="line 7", text="合并收入 100 万元")])
    saved = EvidenceStore(service.store).from_fragments(repo, pid, source.id, extraction_version="parser-v1")
    assert saved["read_scope"] == "fragmented"
    assert saved["internal_source_id"] == source.id
    assert saved["parts"][0]["locator"] == "line 7"
    assert saved["parts"][0]["locator_kind"] == "fragment"


def test_cursor_rejects_tampering_cross_project_or_expanded_disclosure(tmp_path):
    import pytest

    service, pid = make_service(tmp_path / "cursor.sqlite3")
    evidence = EvidenceStore(service.store)
    evidence.save(sample(pid, "public one").model_copy(update={"visibility": "public"}))
    evidence.save(sample(pid, "public two").model_copy(update={"visibility": "public"}))
    cursor = evidence.page(pid, limit=1, include_internal=False)["next_cursor"]
    for project, value, internal in [(pid, cursor[:-1] + "x", False), ("foreign", cursor, False), (pid, cursor, True)]:
        with pytest.raises(ValueError):
            evidence.page(project, cursor=value, include_internal=internal)


def test_immutable_storage_and_metadata_only_list(tmp_path):
    import sqlite3
    from contextlib import closing

    import pytest

    service, pid = make_service(tmp_path / "immutable.sqlite3")
    evidence = EvidenceStore(service.store)
    record = evidence.save(sample(pid))
    assert "parts" not in evidence.page(pid)["items"][0]
    with closing(service.store._connect()) as db, db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE studio_evidence_versions SET record='{}' WHERE id=?", (record["id"],))
