from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import FactInput, LocatedRef, ObjectInput


def test_identity_revision_invalidates_only_its_fact_and_keeps_original(tmp_path):
    service, pid = make_service(tmp_path / "identity-deps.sqlite3")
    text = "Subject Co has a screening product"
    evidence = service.evidence.save(sample(pid, text))
    ref = LocatedRef(evidence_version_id=evidence["id"], part_index=0, start=0, end=len(text), quote=text)
    subject = service.objects.save(pid, ObjectInput(kind="company", name="Subject Co"), actor="user")
    other = service.objects.save(pid, ObjectInput(kind="company", name="Other Co"), actor="user")
    fact = service.objects.save_fact(pid, FactInput(subject_id=subject["id"], predicate="business", text=text, refs=(ref,)))
    deps = service.evidence.dependencies
    assert deps.get(pid, "fact:" + fact["id"])["object_versions"] == {subject["id"]: 1}
    service.objects.save(pid, ObjectInput(kind="company", name="Other renamed"), actor="user", object_id=other["id"], expected_version=1)
    assert deps.get(pid, "fact:" + fact["id"])["status"] == "current"
    service.objects.save(pid, ObjectInput(kind="company", name="Subject corrected"), actor="user", object_id=subject["id"], expected_version=1)
    assert deps.get(pid, "fact:" + fact["id"])["status"] == "stale"
    assert service.objects.facts(pid, subject["id"])[0]["subject_version"] == 1
    assert deps.pending(pid)[0]["artifact_key"] == "fact:" + fact["id"]


def test_timestamp_only_object_save_does_not_invalidate_relationship(tmp_path):
    service, pid = make_service(tmp_path / "unchanged-deps.sqlite3")
    first = service.objects.save(pid, ObjectInput(kind="company", name="First", visibility="public"), actor="user")
    second = service.objects.save(pid, ObjectInput(kind="company", name="Second", visibility="public"), actor="user")
    relation = service.relationships.save_candidate(pid, {"subject_id": first["id"], "peer_id": second["id"]})
    artifact = "relationship:" + relation["scope_key"]
    service.objects.save(pid, ObjectInput(kind="company", name="First", visibility="public"), actor="user", object_id=first["id"], expected_version=1)
    assert service.evidence.dependencies.get(pid, artifact)["status"] == "current"
    assert not service.evidence.dependencies.pending(pid)
