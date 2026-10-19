from test_studio_application_parity import headers, setup_app

from project_watch.studio.monitoring_contracts import Alias, ObjectInput


def test_agent_object_write_receipt_idempotency_and_owner_conflict(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    root = f"/api/studio/agent/projects/{pid}"
    payload = {"object": {"kind": "company", "name": "Synthetic public", "visibility": "public"},
               "expected_project_version": 1, "idempotency_key": "object-one"}
    created = web.post(root + "/objects", json=payload, headers=headers(grant))
    assert created.status_code == 200
    receipt = created.json()
    assert web.post(root + "/objects", json=payload, headers=headers(grant)).json() == receipt
    oid = receipt["object_id"]
    object_read = web.get(root + "/objects/" + oid, headers=headers(grant)).json()
    assert object_read["identity_basis"] == "user_specified" and object_read["version"] == 1
    payload.update(object_id=oid, expected_object_version=1, expected_project_version=2, idempotency_key="owner-change")
    payload["object"]["name"] = "Corrected"
    assert web.post(f"/api/studio/projects/{pid}/monitor-objects", json=payload).status_code == 200
    assert web.get(root + "/objects/" + oid, headers=headers(grant)).json()["name"] == "Corrected"
    payload["idempotency_key"] = "stale-agent"
    assert web.post(root + "/objects", json=payload, headers=headers(grant)).status_code == 409
    assert app.state.studio_application.project(pid)["config_version"] == 3
    assert not app.state.studio_store.jobs(pid)


def test_public_grant_cannot_read_aliases_or_maintain_private_objects(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)
    service = app.state.studio_application
    item = service.objects.save(pid, ObjectInput(kind="company", name="Public", visibility="public", aliases=[
        Alias(name="Hidden internal alias"),
    ]), actor="user")
    private = service.objects.save(pid, ObjectInput(kind="person", name="Private person"), actor="user")
    root = f"/api/studio/agent/projects/{pid}/objects"
    assert "Hidden internal" not in web.get(root + "/" + item["id"], headers=headers(grant)).text
    assert "Private person" not in web.get(root, headers=headers(grant)).text
    assert web.get(root + "/" + private["id"], headers=headers(grant)).status_code == 404
    payload = {"object": {"kind": "person", "name": "Secret"}, "expected_project_version": 1,
               "idempotency_key": "private-write"}
    assert web.post(root, json=payload, headers=headers(grant)).status_code == 403
