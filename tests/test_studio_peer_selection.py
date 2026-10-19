from contextlib import closing

from test_studio_application_parity import headers, setup_app

from project_watch.studio.monitoring_contracts import ObjectInput


def test_name_confirmation_removal_restore_is_durable_and_has_no_jobs(tmp_path):
    app, web, pid, _ = setup_app(tmp_path)
    service = app.state.studio_application
    route = f"/api/studio/projects/{pid}/competitors/selection"
    body = {"name": "  Synthetic Peer  ", "decision": "include", "expected_version": 1, "idempotency_key": "confirm"}
    response = web.post(route, json=body)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["job_status"] == "not_requested" and receipt["version"] == 2
    assert web.post(route, json=body).json() == receipt
    row = service.relationships.view(pid)["items"][0]
    assert row["peer_name"] == "Synthetic Peer" and row["support"] == "user_specified"
    assert service.relationships.public_plan(pid, {"competitors": []})["competitors"][0]["name"] == "Synthetic Peer"
    remove = {**body, "name": row["peer_name"], "peer_id": row["peer_id"], "decision": "exclude",
              "expected_version": 2, "idempotency_key": "remove"}
    assert web.post(route, json=remove).status_code == 200
    assert not service.relationships.view(pid)["items"]
    assert service.relationships.view(pid)["exclusions"][0]["company_wide"]
    assert not service.relationships.public_plan(pid, {"competitors": [{"name": "Synthetic Peer"}]})["competitors"]
    assert web.post(route, json={**remove, "decision": "include", "expected_version": 3, "idempotency_key": "restore"}).status_code == 200
    assert len(service.relationships.history(pid)) == 3
    assert len([r for r in service.objects.page(pid) if r["name"] == "Synthetic Peer"]) == 1
    assert not service.store.jobs(pid)


def test_selection_rejects_self_cross_project_and_conflicts_without_identity_side_effects(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    service = app.state.studio_application
    route = f"/api/studio/projects/{pid}/competitors/selection"
    body = {"name": "Synthetic Optics", "decision": "include", "expected_version": 1, "idempotency_key": "self"}
    assert web.post(route, json=body).status_code == 422
    assert not service.objects.page(pid)
    assert web.post(route, json={**body, "name": "Peer", "peer_id": "other-project"}).status_code == 404
    assert web.post(route, json={**body, "name": "Peer", "expected_version": 5}).status_code == 409
    assert web.post(route, json={**body, "name": " "}).status_code == 422
    assert web.post(route, json={**body, "name": "Peer"}, headers=headers(grant)).status_code == 403
    assert not service.objects.page(pid)
    with closing(service.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_operations").fetchone()[0] == 0


def test_exact_public_identity_reuse_does_not_merge_similar_or_private_names(tmp_path):
    app, web, pid, _ = setup_app(tmp_path)
    service = app.state.studio_application
    same = service.objects.save(pid, ObjectInput(kind="company", name="Exact Peer", visibility="public"), actor="automatic")
    service.objects.save(pid, ObjectInput(kind="company", name="Exact Peer Holdings", visibility="public"), actor="user")
    service.objects.save(pid, ObjectInput(kind="company", name="Private Peer"), actor="user")
    route = f"/api/studio/projects/{pid}/competitors/selection"
    body = {"name": "Exact Peer", "decision": "include", "expected_version": 1, "idempotency_key": "existing"}
    assert web.post(route, json=body).status_code == 200
    assert service.relationships.view(pid)["items"][0]["peer_id"] == same["id"]
    assert web.post(route, json={**body, "name": "Wrong name", "peer_id": same["id"], "expected_version": 2,
                                "idempotency_key": "mismatch"}).status_code == 422
    assert web.post(route, json={**body, "name": "Private Peer", "expected_version": 2,
                                "idempotency_key": "public-name"}).status_code == 200
    assert len([r for r in service.objects.page(pid) if r["name"] == "Private Peer"]) == 2
    assert not service.store.jobs(pid)
