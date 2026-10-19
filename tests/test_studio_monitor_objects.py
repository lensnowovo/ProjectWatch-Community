import pytest
from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import Alias, FactInput, LocatedRef, ObjectInput
from project_watch.studio.objects import ObjectStore


def test_same_names_not_merged_and_parent_not_fact_subject(tmp_path):
    service, pid = make_service(tmp_path / "objects.sqlite3")
    store = ObjectStore(service.store, service.evidence)
    parent = store.save(pid, ObjectInput(kind="company", name="Twin", visibility="public"), actor="user")
    child = store.save(pid, ObjectInput(kind="company", name="Twin", parent_id=parent["id"]), actor="user")
    assert child["id"] != parent["id"]
    source = service.evidence.save(sample(pid, "子公司 2025 收入 100 万元"))
    text = source["parts"][0]["text"]
    fact = store.save_fact(pid, FactInput(subject_id=child["id"], predicate="revenue", text=text, period="2025",
                                         currency="CNY", unit="万元", basis="actual", consolidation_scope="subsidiary",
                                         refs=[LocatedRef(evidence_version_id=source["id"], part_index=0, start=0,
                                                          end=len(text), quote=text)]))
    assert store.facts(pid, parent["id"]) == []
    assert store.facts(pid, child["id"])[0]["id"] == fact["id"]
    assert fact["consolidation_scope"] == "subsidiary"


def test_user_identity_correction_survives_automatic_refresh_and_history(tmp_path):
    service, pid = make_service(tmp_path / "correction.sqlite3")
    store = ObjectStore(service.store, service.evidence)
    item = store.save(pid, ObjectInput(kind="company", name="Original"), actor="automatic")
    manual = store.save(pid, ObjectInput(kind="company", name="Corrected", website="https://synthetic.example"),
                        actor="user", object_id=item["id"], expected_version=1)
    assert manual["identity_basis"] == "user_specified"
    held = store.save(pid, ObjectInput(kind="company", name="Wrong again"), actor="automatic",
                      object_id=item["id"], expected_version=2)
    assert held["name"] == "Corrected" and held["version"] == 2
    with pytest.raises(ValueError):
        store.save(pid, ObjectInput(kind="company", name="stale"), actor="user", object_id=item["id"], expected_version=1)
    assert len(store.history(pid, item["id"])) == 2
    assert ObjectStore(service.store, service.evidence).get(pid, item["id"])["name"] == "Corrected"


def test_alias_dates_and_cross_project_parent_are_enforced(tmp_path):
    service, pid = make_service(tmp_path / "aliases.sqlite3")
    store = ObjectStore(service.store, service.evidence)
    item = store.save(pid, ObjectInput(kind="company", name="Current", visibility="public", aliases=[
        Alias(name="Historic", valid_from="2020-01-01", valid_to="2021-01-01", visibility="public"),
    ]), actor="user")
    assert "Historic" not in store.public_names(pid, as_of="2026-10-08")
    assert "Historic" in store.public_names(pid, as_of="2020-06-01")
    with pytest.raises(KeyError):
        store.save("another-project", ObjectInput(kind="company", name="Bad", parent_id=item["id"]), actor="user")


def test_code_or_identity_support_must_have_exact_evidence(tmp_path):
    service, pid = make_service(tmp_path / "codes.sqlite3")
    store = ObjectStore(service.store, service.evidence)
    with pytest.raises(ValueError):
        store.save(pid, ObjectInput(kind="product", name="Compound", registration_code="RX-123"), actor="automatic")
    source = service.evidence.save(sample(pid, "Compound 的登记号 RX-123"))
    text = source["parts"][0]["text"]
    good = ObjectInput(kind="product", name="Compound", registration_code="RX-123", refs=[
        LocatedRef(evidence_version_id=source["id"], part_index=0, start=0, end=len(text), quote=text),
    ])
    item = store.save(pid, good, actor="automatic")
    assert item["identity_basis"] == "candidate_with_evidence", "quote validity is not regulatory verification"


def test_public_object_cannot_export_private_evidence(tmp_path):
    service, pid = make_service(tmp_path / "privacy.sqlite3")
    store = ObjectStore(service.store, service.evidence)
    source = service.evidence.save(sample(pid, "Private product P123"))
    text = source["parts"][0]["text"]
    with pytest.raises(ValueError):
        store.save(pid, ObjectInput(kind="product", name="P123", visibility="public", refs=[
            LocatedRef(evidence_version_id=source["id"], part_index=0, start=0, end=len(text), quote=text),
        ]), actor="automatic")
