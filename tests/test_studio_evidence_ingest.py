from contextlib import closing

import pytest
from test_studio_agent_instance import make_service


def item(key, url):
    return {"key": key, "title": "Synthetic report", "url": url, "source": "Synthetic outlet",
            "snippet": "Original search summary", "published_at": "2026-10-01"}


def test_body_snapshot_and_projection_transaction_keep_full_text_and_prior_versions(tmp_path):
    service, pid = make_service(tmp_path / "ingest.sqlite3")
    service.store.add_items(pid, "company", [item("one", "https://synthetic.example/one")])
    row = service.store.untriaged(pid)[0]
    original = "Original body. " * 1500
    service.store.save_body(row["id"], "article_body", original)
    versions = service.evidence.versions(pid, "item:" + row["id"])
    assert len(versions) == 1 and "".join(p["text"] for p in versions[0]["parts"]) == original
    with closing(service.store._connect()) as db:
        assert len(db.execute("SELECT body FROM studio_items WHERE id=?", (row["id"],)).fetchone()[0]) == 12000
    service.store.save_body(row["id"], "search_summary", "Corrected search summary")
    assert service.evidence.versions(pid, "item:" + row["id"])[1]["read_scope"] == "summary"
    assert service.evidence.get(pid, versions[0]["id"])["parts"][0]["text"] == original[:20000]


def test_snapshot_failure_rolls_back_projection_and_donor_not_forged(tmp_path, monkeypatch):
    service, pid = make_service(tmp_path / "atomic.sqlite3")
    service.store.add_items(pid, "company", [item("one", "https://synthetic.example/one")])
    row = service.store.untriaged(pid)[0]
    service.store.save_body(row["id"], "article_body", "donor original text")
    service.store.add_items(pid, "company", [item("two", "https://synthetic.example/two")])
    other = next(r for r in service.store.untriaged(pid) if r["key"] == "two")
    service.store.save_body(other["id"], "twin_body", "donor original text", donor=row)
    twin = service.store.item_evidence(pid, other["id"])
    assert twin["basis"] == "donor_text"
    assert service.evidence.get(pid, twin["evidence_version_id"])["source_key"] == "item:" + row["id"]
    with pytest.raises(ValueError):
        service.store.save_body(other["id"], "twin_body", "forged different text", donor=row)
    assert not service.evidence.versions(pid, "item:" + other["id"])

    def fail(*_args, **_kwargs):
        raise ValueError("synthetic snapshot failure")

    monkeypatch.setattr(service.store.evidence, "save", fail)
    with pytest.raises(ValueError):
        service.store.save_body(row["id"], "article_body", "new text")
    with closing(service.store._connect()) as db:
        assert db.execute("SELECT body FROM studio_items WHERE id=?", (row["id"],)).fetchone()[0] == "donor original text"


def test_public_profile_uses_exact_version_quotes_and_summary_scope(tmp_path):
    from types import SimpleNamespace

    from project_watch.studio.public_profile import build_public_profile

    service, pid = make_service(tmp_path / "profile.sqlite3")
    project = {**service.project(pid), "legal_name": "Synthetic Optics"}

    class Model:
        ready = True

        def json(self, *_args, **_kwargs):
            return {"facts": [{"kind": "product", "label": "lens", "text": "Company develops lenses", "refs": [
                {"ref": "P1", "quote": "develops lenses"},
            ]}]}

    result = build_public_profile(project, SimpleNamespace(has_search=False), Model(), [
        {"url": "https://synthetic.example/report", "title": "Synthetic Optics", "snippet": "Synthetic Optics develops lenses"},
    ], evidence=service.evidence)
    ref = result["facts"][0]["refs"][0]
    record = service.evidence.get(pid, ref["evidence_version_id"])
    assert record["read_scope"] == "summary" and record["source_type"] == "search_summary"
    assert record["parts"][0]["text"][ref["start"]:ref["end"]] == "develops lenses"


def test_internal_fragment_snapshot_retains_normalized_prompt_text(tmp_path):
    from project_watch.domain import FragmentInput
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore

    service, pid = make_service(tmp_path / "internal.sqlite3")
    repo = ProjectRepository(service.store.db_path)
    source = repo.add_source(pid, kind="document", reference="synthetic.txt", content_sha256="a" * 64)
    repo.enqueue_intake(pid, source.id)
    job = repo.claim_next_intake()
    repo.complete_intake(job.id, [FragmentInput(ordinal=1, locator="line 7", text="收入\n 100 万元")])
    pipeline = Pipeline(repo, service.store, SettingsStore(tmp_path / "settings.json"))
    refs, blocks, _ = pipeline._fragments(pid)
    record = service.evidence.get(pid, refs["F1"]["evidence_version_id"])
    assert record["parts"][0]["text"] == "收入 100 万元"
    assert "收入 100 万元" in blocks[0]
    assert record["visibility"] == "internal"
    assert pipeline._fragments(pid)[0]["F1"]["evidence_version_id"] == record["id"]


def test_ambiguous_internal_metric_is_not_attributed_to_project_company(tmp_path):
    from project_watch.domain import FragmentInput
    from project_watch.repository import ProjectRepository
    from project_watch.studio.ingest_facts import ingest_metrics
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore

    service, pid = make_service(tmp_path / "metric-subject.sqlite3")
    repo = ProjectRepository(service.store.db_path)
    source = repo.add_source(pid, kind="document", reference="synthetic.txt", content_sha256="b" * 64)
    repo.enqueue_intake(pid, source.id)
    job = repo.claim_next_intake()
    repo.complete_intake(job.id, [FragmentInput(ordinal=1, locator="line 1", text="客户公司 2025 收入 100 万元")])
    refs, _, _ = Pipeline(repo, service.store, SettingsStore(tmp_path / "settings.json"))._fragments(pid)
    metrics = [{"source_id": source.id, "locator": "line 1", "quote": "客户公司 2025 收入 100 万元",
                "name": "收入", "value": 100, "period": "2025", "unit": "万元", "currency": "CNY", "basis": "actual"}]
    assert ingest_metrics(pid, metrics, refs, service.objects, service.evidence) == 1
    project_subject = service.objects.ensure_subject(pid)
    assert not service.objects.facts(pid, project_subject["id"])
    unassigned = service.objects.unassigned_document_subject(pid, source.id)
    assert service.objects.facts(pid, unassigned["id"])[0]["subject_attribution"] == "unknown"
