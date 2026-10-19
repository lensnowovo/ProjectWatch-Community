from fastapi.testclient import TestClient

from project_watch.studio.app import create_studio_app


def setup_app(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    project = app.state.intake_worker.repository.create_project("Synthetic Optics")
    project_id = project.id
    app.state.studio_store.upsert_project(project_id, {"legal_name": project.name}, consent=False)
    grant = client.post(f"/api/studio/projects/{project_id}/agent-grants", json={
        "permission": "write", "label": "synthetic-adapter",
    }).json()
    return app, client, project_id, grant


def headers(grant):
    return {"Authorization": f"Bearer {grant['token']}", "X-ProjectWatch-Instance": grant["instance_id"]}


def test_agent_and_web_share_versions_and_status(tmp_path):
    app, client, pid, grant = setup_app(tmp_path)
    route = f"/api/studio/agent/projects/{pid}"
    project = client.get(route, headers=headers(grant)).json()
    assert project["config_version"] == client.get(f"/api/studio/projects/{pid}").json()["project"]["config_version"]
    reply = client.patch(route + "/status", headers=headers(grant), json={
        "status": "paused", "expected_version": project["config_version"], "idempotency_key": "pause-via-agent",
    })
    assert reply.status_code == 200
    receipt = reply.json()
    assert client.get(f"/api/studio/projects/{pid}").json()["project"]["status"] == "paused"
    assert client.get(route + f"/operations/{receipt['operation_id']}", headers=headers(grant)).json() == receipt
    client.patch(f"/api/studio/projects/{pid}", json={"status": "active"})
    assert client.get(route, headers=headers(grant)).json()["status"] == "active"
    assert not app.state.studio_store.jobs(pid)


def test_denials_no_token_escalation_and_no_network(tmp_path):
    app, client, pid, grant = setup_app(tmp_path)
    route = f"/api/studio/agent/projects/{pid}"
    assert client.get(route).status_code == 401
    wrong = {**headers(grant), "X-ProjectWatch-Instance": "another-runtime"}
    assert client.get(route, headers=wrong).status_code == 403
    assert client.get("/api/studio/settings", headers=headers(grant)).status_code == 403
    assert client.patch(f"/api/studio/projects/{pid}", headers=headers(grant), json={"status": "paused"}).status_code == 403
    assert client.post(f"/api/studio/projects/{pid}/agent-grants", headers=headers(grant), json={
        "permission": "write", "label": "escalate",
    }).status_code == 403
    reader = client.post(f"/api/studio/projects/{pid}/agent-grants", json={
        "permission": "read", "label": "reader",
    }).json()
    assert client.patch(route + "/status", headers=headers(reader), json={
        "status": "paused", "expected_version": 1, "idempotency_key": "denied",
    }).status_code == 403
    assert client.get("/api/studio/agent/projects/other", headers=headers(grant)).status_code == 403
    assert client.get(route, headers=headers(grant)).status_code == 200
    assert not app.state.studio_store.jobs(pid)


def test_search_bounded_to_project_and_revoke(tmp_path):
    _, client, pid, grant = setup_app(tmp_path)
    route = f"/api/studio/agent/projects/{pid}"
    assert client.get(route + "/search?q=Optics", headers=headers(grant)).json()["items"][0]["id"] == pid
    assert client.get(route + "/search?q=Nonmatch", headers=headers(grant)).json()["items"] == []
    assert client.delete(f"/api/studio/projects/{pid}/agent-grants/{grant['id']}").status_code == 204
    assert client.get(route, headers=headers(grant)).status_code == 403


def test_validation_and_cross_site_owner_grant_rejected(tmp_path):
    _, client, pid, grant = setup_app(tmp_path)
    route = f"/api/studio/agent/projects/{pid}"
    assert client.post(f"/api/studio/projects/{pid}/agent-grants", headers={"Origin": "https://evil.example"},
                       json={"permission": "write", "label": "evil"}).status_code == 403
    assert client.patch(route + "/status", headers=headers(grant), json={
        "status": "paused", "expected_version": True, "idempotency_key": "invalid",
    }).status_code == 422
    assert client.patch(route + "/status", headers=headers(grant), json={
        "status": "paused", "expected_version": 1, "idempotency_key": "first",
    }).status_code == 200
    assert client.patch(route + "/status", headers=headers(grant), json={
        "status": "active", "expected_version": 1, "idempotency_key": "second",
    }).status_code == 409
