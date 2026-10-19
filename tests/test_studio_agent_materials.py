from contextlib import closing

from test_studio_application_parity import headers, setup_app

from project_watch.domain import FragmentInput
from project_watch.studio.settings import ModelConfig


def upload(client, pid, grant, *, version=1, key="submit-one", text=b"Synthetic internal revenue 10"):
    return client.post(f"/api/studio/agent/projects/{pid}/sources", headers=headers(grant),
                       data={"expected_version": str(version), "idempotency_key": key},
                       files={"file": ("synthetic.txt", text, "text/plain")})


def prepare(tmp_path):
    app, client, pid, _ = setup_app(tmp_path)
    service = app.state.studio_application
    grant = service.issue_grant(pid, "write", "materials", allow_internal=True)
    return app, client, pid, grant


def test_submission_scope_idempotency_and_actual_parser_receipt(tmp_path):
    app, client, pid, grant = prepare(tmp_path)
    public = app.state.studio_application.issue_grant(pid, "write", "public-only")
    assert upload(client, pid, public).status_code == 403
    response = upload(client, pid, grant)
    assert response.status_code == 202
    receipt = response.json()
    assert receipt["job_status"] == "queued" and receipt["analysis_status"] == "owner_authorization_required"
    assert receipt["saved"] is True and receipt["version"] == 2
    assert upload(client, pid, grant).json() == receipt
    assert upload(client, pid, grant, text=b"different contents").status_code == 409
    assert upload(client, pid, grant, key="stale-upload").status_code == 409
    repo = app.state.intake_worker.repository
    assert len(repo.list_sources(pid)) == len(repo.list_intake(pid)) == 1
    assert not app.state.studio_store.jobs(pid)
    app.state.studio_application.revoke_grant(pid, grant["id"])
    assert upload(client, pid, grant, version=2, key="revoked").status_code == 403


def test_parse_only_snapshot_and_provider_specific_owner_authorization(tmp_path):
    app, client, pid, grant = prepare(tmp_path)
    pipeline = app.state.studio_pipeline
    pipeline.settings_store.update({"model": {"base_url": "https://model.example/v1", "model": "synthetic", "api_key": "synthetic"}})
    store = app.state.studio_store
    store.upsert_project(pid, {"legal_name": "Synthetic Optics"}, consent=True)
    version = app.state.studio_application.project(pid)["config_version"]
    receipt = upload(client, pid, grant, version=version).json()
    repo = app.state.intake_worker.repository
    job = repo.claim_next_intake()
    repo.complete_intake(job.id, [FragmentInput(ordinal=1, locator="line 1", text="Synthetic internal revenue 10")])
    assert pipeline.snapshot_ready_documents() == 1
    assert pipeline.snapshot_ready_documents() == 0
    assert pipeline._fragments(pid)[1:] == ([], [])
    assert not store.unanalyzed_ready_projects(model_binding=pipeline.model_binding())
    evidence = app.state.studio_application.evidence.page(pid, include_internal=True)["items"]
    assert len(evidence) == 1  # local parsing evidence exists without model disclosure
    root = f"/api/studio/projects/{pid}/sources/{receipt['source_id']}/authorize-analysis"
    body = {"expected_version": receipt["version"], "idempotency_key": "authorize-model"}
    assert client.post(root, json=body, headers=headers(grant)).status_code == 403
    assert client.post(root, json=body).status_code == 200
    assert store.unanalyzed_ready_projects(model_binding=pipeline.model_binding()) == [pid]
    assert pipeline._fragments(pid)[1]
    store.put_profile(pid, {"summary": "Synthetic private summary", "sections": [{"points": [{"refs": [{"source_id": receipt["source_id"]}]}]}]})
    store.mark_analyzed(pid, [receipt["source_id"]])
    assert pipeline.model_view().get_profile(pid)["summary"]
    pipeline.settings_store.update({"model": {"model": "other-model"}})
    assert not store.unanalyzed_ready_projects(model_binding=pipeline.model_binding())
    assert pipeline._fragments(pid)[1:] == ([], [])
    assert pipeline.model_view().get_profile(pid) is None
    assert store.get_profile(pid)["summary"]  # owner keeps its old report
    status = client.get(f"/api/studio/agent/projects/{pid}/sources/{receipt['source_id']}", headers=headers(grant)).json()
    assert status["parser_status"] == "completed" and status["analysis_status"] == "owner_authorization_required"
    assert "model.example" not in str(status) and "synthetic" not in str(status)


def test_upload_transaction_rollback_and_owner_parity(tmp_path, monkeypatch):
    app, client, pid, grant = prepare(tmp_path)
    repo = app.state.intake_worker.repository
    original = repo.enqueue_intake
    def broken(*args, **kwargs):
        raise ValueError("synthetic queue failure")
    monkeypatch.setattr(repo, "enqueue_intake", broken)
    assert upload(client, pid, grant).status_code == 422
    assert not repo.list_sources(pid)
    with closing(app.state.studio_store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_operations").fetchone()[0] == 0
    monkeypatch.setattr(repo, "enqueue_intake", original)
    owner = client.post(f"/api/studio/projects/{pid}/files", files={"file": ("owner.txt", b"Owner contents", "text/plain")})
    assert owner.status_code == 202 and owner.json()["status"] == "queued"
    assert owner.json()["operation_id"]
    assert len(repo.list_sources(pid)) == 1


def test_source_status_cannot_leak_other_project(tmp_path):
    app, client, pid, grant = prepare(tmp_path)
    repo = app.state.intake_worker.repository
    other = repo.create_project("Another synthetic company")
    foreign = repo.add_source(other.id, kind="document", reference="private.txt", content_sha256="a" * 64)
    response = client.get(f"/api/studio/agent/projects/{pid}/sources/{foreign.id}", headers=headers(grant))
    assert response.status_code == 404
    assert foreign.reference not in response.text
    assert ModelConfig().ready is False


def test_thin_bridge_submits_text_and_explicit_file_without_model_calls(tmp_path):
    from project_watch.studio.agent_bridge import AgentClient

    app, web, pid, grant = prepare(tmp_path)
    def transport(method, url, headers, body):
        response = web.request(method, url, headers=headers, content=body)
        return response.status_code, response.content
    client = AgentClient("http://localhost", grant["instance_id"], pid, grant["token"], transport=transport)
    first = client.submit_text("test.txt", "Synthetic evidence", 1, "bridge-text")
    assert first["analysis_status"] == "owner_authorization_required"
    assert client.source_status(first["source_id"])["parser_status"] == "queued"
    file_path = tmp_path / "upload.txt"
    file_path.write_text("Another synthetic source", encoding="utf-8")
    second = client.submit_file(file_path, first["version"], "bridge-file")
    assert second["analysis_status"] == "owner_authorization_required" and second["job_status"] == "queued"
    repo = app.state.intake_worker.repository
    original = repo.get_source(first["source_id"])
    path = app.state.intake_worker.blob_store.path_for_hash(original.content_sha256)
    # Unsupported extensionless originals cannot be submitted as a new format.
    import pytest

    from project_watch.studio.agent_bridge import AgentClientError
    with pytest.raises(AgentClientError):
        client.submit_file(path, second["version"], "unsupported-file")
    assert len(repo.list_sources(pid)) == 2
