from test_studio_agent_instance import make_service
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import FactInput, LocatedRef, ObjectInput


def setup_pair(tmp_path, buyers=("pharma", "pharma"), deliveries=("software", "software")):
    from project_watch.studio.competition import assess_relation

    service, pid = make_service(tmp_path / "bilateral.sqlite3")
    service.relationships.assessment_validator = assess_relation
    companies, products, facts = [], [], []
    for i, name in enumerate(("Alpha", "Beta")):
        product = name + " organ"
        text = f"{name} builds {product} for drug screening; buyers are {buyers[i]}; delivery is {deliveries[i]}."
        evidence = service.evidence.save(sample(pid, text).model_copy(update={"source_key": name}))
        ref = LocatedRef(evidence_version_id=evidence["id"], part_index=0, start=0, end=len(text), quote=text)
        company = service.objects.save(pid, ObjectInput(kind="company", name=name, refs=(ref,)), actor="user")
        item = service.objects.save(pid, ObjectInput(kind="product", name=product, parent_id=company["id"], refs=(ref,)), actor="user")
        rows = [service.objects.save_fact(pid, FactInput(subject_id=item["id"], predicate=predicate, value=value,
                 text=value, basis="actual", subject_attribution="explicit", refs=(ref,))) for predicate, value in
                (("use_case", "drug screening"), ("buyer_type", buyers[i]), ("delivery", deliveries[i]))]
        companies.append(company)
        products.append(item)
        facts.append(rows)
    scope = {"subject_id": companies[0]["id"], "peer_id": companies[1]["id"],
             "subject_product_id": products[0]["id"], "peer_product_id": products[1]["id"]}
    return service, pid, scope, facts


def reviewer(context):
    return {"verdict": "supported", "reason": "Synthetic semantic review", "fact_ids": [f["id"] for f in context["facts"]]}


def test_bilateral_shared_use_and_buyer_type_needs_semantic_review(tmp_path):
    from project_watch.studio.competition import assess_relation

    service, pid, scope, _ = setup_pair(tmp_path)
    assert assess_relation(service, pid, scope, "direct")["support"] == "pending"
    result = assess_relation(service, pid, scope, "direct", review=reviewer)
    assert result["support"] == "supported"
    assert result["overlap_kind"] == "buyer_type_not_same_customer"
    assert result["assertion_basis"] == "bilateral_source_assertions_not_independent_fact_verification"


def test_different_buyers_cannot_be_direct_even_if_review_agrees(tmp_path):
    from project_watch.studio.competition import assess_relation

    service, pid, scope, _ = setup_pair(tmp_path, buyers=("pharma", "universities"))
    result = assess_relation(service, pid, scope, "direct", review=reviewer)
    assert result["support"] == "pending" and "buyer_overlap" in result["missing"]


def test_different_delivery_can_be_substitute_but_not_direct(tmp_path):
    from project_watch.studio.competition import assess_relation

    service, pid, scope, _ = setup_pair(tmp_path, deliveries=("software", "wet-lab"))
    assert assess_relation(service, pid, scope, "direct", review=reviewer)["support"] == "pending"
    assert assess_relation(service, pid, scope, "substitute", review=reviewer)["support"] == "supported"


def test_conflicting_or_withdrawn_evidence_cannot_be_supported(tmp_path):
    from project_watch.studio.competition import assess_relation

    service, pid, scope, facts = setup_pair(tmp_path)
    source = facts[0][0]["refs"][0]["evidence_version_id"]
    service.evidence.dependencies.withdraw(pid, source, "explicit correction", actor="synthetic-user")
    result = assess_relation(service, pid, scope, "direct", review=reviewer)
    assert result["support"] == "pending" and "current_bilateral_facts" in result["missing"]


def test_review_cannot_cite_foreign_facts_or_promote_changed_inputs(tmp_path):
    from project_watch.studio.competition import assess_relation

    service, pid, scope, facts = setup_pair(tmp_path)
    invalid = assess_relation(service, pid, scope, "direct", review=lambda c: {
        **reviewer(c), "fact_ids": ["not-a-supplied-fact"]})
    assert invalid["support"] == "pending" and "valid_semantic_review" in invalid["missing"]

    def changed(context):
        service.evidence.dependencies.withdraw(pid, facts[0][0]["refs"][0]["evidence_version_id"], "correction", actor="test")
        return reviewer(context)

    assert "inputs_changed_during_review" in assess_relation(service, pid, scope, "direct", review=changed)["missing"]


def test_additional_current_buyers_are_retained_for_semantic_review(tmp_path):
    from project_watch.studio.competition import assess_relation

    service, pid, scope, _ = setup_pair(tmp_path)
    text = "Alpha organ serves universities."
    evidence = service.evidence.save(sample(pid, text).model_copy(update={"source_key": "conflict"}))
    ref = LocatedRef(evidence_version_id=evidence["id"], part_index=0, start=0, end=len(text), quote=text)
    service.objects.save_fact(pid, FactInput(subject_id=scope["subject_product_id"], predicate="buyer_type",
        value="universities", text="universities", basis="actual", subject_attribution="explicit", refs=(ref,)))
    seen = []
    result = assess_relation(service, pid, scope, "direct", review=lambda c: seen.append(c) or {
        **reviewer(c), "verdict": "disputed", "reason": "Synthetic contradictory buyer statement"})
    assert len(seen[0]['facts']) == 7
    assert any(f['value'] == 'universities' for f in seen[0]['facts'])
    assert result['support'] == 'disputed'


def test_supported_record_is_persisted_but_withdrawal_downgrades_current_view(tmp_path):
    from project_watch.studio.competition import assess_relation

    service, pid, scope, facts = setup_pair(tmp_path)
    assessment = assess_relation(service, pid, scope, "direct", review=reviewer)
    saved = service.relationships.save_assessment(pid, assessment)
    assert saved["support"] == "supported"
    assert service.relationships.page(pid)[0]["support"] == "supported"
    view = service.relationships.view(pid)['items'][0]
    assert len(view['basis_facts']) == 6
    assert all('quote' not in ref for fact in view['basis_facts'] for ref in fact['refs'])
    assert service.relationships.view(pid, include_internal=False)['items'] == []
    service.evidence.dependencies.withdraw(pid, facts[0][0]["refs"][0]["evidence_version_id"], "correction", actor="test")
    current = service.relationships.page(pid)[0]
    assert current["status"] == "stale" and current["support"] == "pending"
    assert current["previous_support"] == "supported"
    assert service.relationships.history(pid)[0]["support"] == "supported"


def test_untrusted_or_late_assessment_cannot_be_saved_as_supported(tmp_path):
    import pytest

    from project_watch.studio.application import VersionConflict
    from project_watch.studio.competition import assess_relation

    service, pid, scope, facts = setup_pair(tmp_path)
    unreviewed = assess_relation(service, pid, scope, "direct")
    with pytest.raises(ValueError):
        service.relationships.save_assessment(pid, {**unreviewed, "support": "supported"})
    assessed = assess_relation(service, pid, scope, "direct", review=reviewer)
    service.evidence.dependencies.withdraw(pid, facts[0][0]["refs"][0]["evidence_version_id"], "correction", actor="test")
    with pytest.raises(VersionConflict):
        service.relationships.save_assessment(pid, assessed)
    assert service.relationships.page(pid) == []
