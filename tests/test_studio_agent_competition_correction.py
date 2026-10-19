from contextlib import closing

from test_studio_application_parity import headers, setup_app

from project_watch.studio.monitoring_contracts import Alias, ObjectInput


def prepare(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    service = app.state.studio_application
    companies = [service.objects.save(pid, ObjectInput(kind="company", name=name, visibility="public"), actor="user")
                 for name in ("Project Co", "Peer A", "Peer B")]
    products = [service.objects.save(pid, ObjectInput(kind="product", name=f"Product {n}", visibility="public", parent_id=company["id"]), actor="user")
                for n, company in enumerate(companies)]
    return app, web, pid, grant, companies, products


def scope(companies, products, peer):
    return {"subject_id": companies[0]["id"], "peer_id": companies[peer]["id"],
            "subject_product_id": products[0]["id"], "peer_product_id": products[peer]["id"]}


def correction(companies, products, version=1, key="correct-peers"):
    return {"expected_version": version, "idempotency_key": key, "changes": [
        {**scope(companies, products, 1), "decision": "exclude", "reason": "different buying need"},
        {**scope(companies, products, 2), "decision": "include", "relation_type": "substitute", "reason": "user-selected alternative"},
    ]}


def test_atomic_scoped_correction_survives_research_aliases_and_restart(tmp_path):
    app, web, pid, grant, companies, products = prepare(tmp_path)
    service = app.state.studio_application
    path = f"/api/studio/agent/projects/{pid}/relationships/corrections"
    response = web.post(path, json=correction(companies, products), headers=headers(grant))
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["saved"] and receipt["version"] == 2 and receipt["job_status"] == "not_requested"
    assert web.post(path, json=correction(companies, products), headers=headers(grant)).json() == receipt
    assert web.post(path, json=correction(companies, products, key="stale"), headers=headers(grant)).status_code == 409
    assert len(service.relationships.history(pid)) == 2
    rows = service.relationships.page(pid)
    assert {r["peer_id"] for r in rows} == {companies[2]["id"]}
    assert rows[0]["origin"] == "user_override" and rows[0]["support"] == "user_specified"
    displayed = web.get(f"/api/studio/projects/{pid}").json()["relationships"]
    assert displayed["items"][0]["peer_name"] == "Peer B"
    assert displayed["exclusions"][0]["peer_name"] == "Peer A"
    peer = companies[1]
    service.objects.save(pid, ObjectInput(kind="company", name="Peer A renamed", visibility="public",
                                         aliases=(Alias(name="Peer A", visibility="public"),)), actor="user", object_id=peer["id"], expected_version=1)
    from project_watch.studio.relationships import RelationshipStore
    restarted = RelationshipStore(service.store, service.objects)
    restarted.save_candidate(pid, scope(companies, products, 1), relation_type="direct", rationale="new AI lead")
    assert {r["peer_id"] for r in restarted.page(pid)} == {companies[2]["id"]}
    assert restarted.overrides(pid)[0]["decision"] in ("include", "exclude")
    assert not service.store.jobs(pid)


def test_rejects_cross_project_or_private_identity_and_rolls_back_entire_set(tmp_path):
    app, web, pid, grant, companies, products = prepare(tmp_path)
    service = app.state.studio_application
    body = correction(companies, products)
    body["changes"][1]["peer_id"] = "foreign-object"
    response = web.post(f"/api/studio/agent/projects/{pid}/relationships/corrections", json=body, headers=headers(grant))
    assert response.status_code == 404
    assert not service.relationships.overrides(pid)
    assert service.project(pid)["config_version"] == 1
    private = service.objects.save(pid, ObjectInput(kind="company", name="Private supplier"), actor="user")
    body["changes"][1].update(peer_id=private["id"], peer_product_id=None)
    assert web.post(f"/api/studio/agent/projects/{pid}/relationships/corrections", json=body, headers=headers(grant)).status_code == 404
    assert not service.relationships.overrides(pid)
    with closing(service.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_operations").fetchone()[0] == 0


def test_scope_exclusion_and_reset_preserve_history_without_restoring_user_addition(tmp_path):
    app, web, pid, _grant, companies, products = prepare(tmp_path)
    service = app.state.studio_application
    root = f"/api/studio/projects/{pid}/relationships/corrections"
    receipt = web.post(root, json=correction(companies, products)).json()
    # A product-specific rejection must not erase another product relationship.
    other = {"subject_id": companies[0]["id"], "peer_id": companies[1]["id"], "subject_product_id": None, "peer_product_id": None}
    service.relationships.save_candidate(pid, other, relation_type="adjacent", rationale="company-level candidate")
    assert any(r["peer_id"] == companies[1]["id"] for r in service.relationships.page(pid))
    reset = {"expected_version": receipt["version"], "idempotency_key": "reset-B", "changes": [
        {**scope(companies, products, 2), "decision": "reset", "reason": "restore automatic research"}]}
    assert web.post(root, json=reset).status_code == 200
    assert all(r["peer_id"] != companies[2]["id"] for r in service.relationships.page(pid))
    assert sum(r["origin"] == "user_override" for r in service.relationships.history(pid)) == 3


def test_candidate_never_becomes_supported_direct_without_bilateral_fact_evidence(tmp_path):
    app, _, pid, _, companies, products = prepare(tmp_path)
    service = app.state.studio_application
    candidate = service.relationships.save_candidate(pid, scope(companies, products, 1), relation_type="direct", rationale="same AI sector")
    assert candidate["support"] == "pending" and candidate["origin"] == "auto_research"
    assert service.relationships.page(pid)[0]["support"] == "pending"


def test_company_wide_exclusion_affects_public_plan_but_not_other_companies(tmp_path):
    app, web, pid, grant, companies, products = prepare(tmp_path)
    body = correction(companies, products)
    body["changes"][0].update(subject_product_id=None, peer_product_id=None)
    response = web.post(f"/api/studio/agent/projects/{pid}/relationships/corrections", json=body, headers=headers(grant))
    assert response.status_code == 200
    result = app.state.studio_application.relationships.public_plan(pid, {"competitors": [{"name": "Peer A"}, {"name": "Unrelated C"}]})
    assert [p["name"] for p in result["competitors"]] == ["Unrelated C", "Peer B"]
    assert "different buying need" not in str(result)
