"""Operator consent is explicit, local and bounded; a GET never invokes a model."""

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.dsh_executor import DshProposalModel
from project_watch.model_provider import ModelProviderConfig
from project_watch.repository import ProjectRepository


def client_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_WATCH_SETTINGS_TOKEN", "synthetic-operator")
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Synthetic reading")
    source = repo.add_source(project.id, kind="document", reference="bp.txt", content_sha256="a" * 64)
    model = DshProposalModel(ModelProviderConfig("https://example.test", "anthropic-messages", "fake"), "never-return")
    client = TestClient(create_app(repo.db_path, proposal_model=model), base_url="http://127.0.0.1")
    return repo, project, source, model, client


def test_explicit_consent_and_restart(tmp_path, monkeypatch):
    repo, project, source, model, client = client_setup(tmp_path, monkeypatch)
    route = f"/api/workspace/projects/{project.id}/reading-authorization"
    before = client.get(route)
    assert before.status_code == 200
    assert before.json()["authorizations"] == []
    assert before.json()["automatic_reading_available"] is False  # This test app starts no threads.
    body = {"consent": True, "config_id": model.authorization_id(), "request_key": "synthetic-consent-1",
            "rule_version": before.json()["rule_version"], "max_output_tokens": before.json()["model"]["max_output_tokens"],
            "source_hashes": [source.content_sha256], "include_future": False,
            "max_requests": 2}
    assert client.post(route, json=body).status_code == 403
    headers = {"Origin": "http://127.0.0.1", "X-Settings-Token": "synthetic-operator"}
    assert client.post(route, json=body, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    assert client.post(route, json={**body, "consent": False}, headers=headers).status_code == 422
    assert client.post(route, json={**body, "config_id": "b" * 64}, headers=headers).status_code == 409
    assert client.post(route, json={**body, "source_hashes": ["b" * 64]}, headers=headers).status_code == 422
    saved = client.post(route, json=body, headers=headers)
    assert saved.status_code == 200
    assert "never-return" not in saved.text
    grant_id = saved.json()["id"]
    assert client.post(route, json=body, headers=headers).json()["id"] == grant_id
    assert client.post(route, json={**body, "max_requests": 3}, headers=headers).status_code == 422
    reboot = TestClient(create_app(repo.db_path, proposal_model=model), base_url="http://127.0.0.1")
    assert reboot.get(route).json()["authorizations"][0]["id"] == grant_id
    assert reboot.patch(route + "/" + grant_id, headers=headers, json={"status": "paused"}).status_code == 200
    assert reboot.get(route).json()["authorizations"][0]["status"] == "paused"
    assert reboot.post(route, json=body, headers=headers).json()["status"] == "paused"
    assert len(reboot.get(route).json()["authorizations"]) == 1
    other = repo.create_project("Other company")
    foreign_route = f"/api/workspace/projects/{other.id}/reading-authorization/{grant_id}"
    assert reboot.patch(foreign_route, headers=headers, json={"status": "active"}).status_code == 404
    assert model._runs == 0
    assert repo.list_observations(project.id) == []


def test_credential_or_receiver_change_invalidates_fingerprint():
    config = ModelProviderConfig("https://example.test", "anthropic-messages", "fake")
    original = DshProposalModel(config, "key1")
    assert original.authorization_id() == DshProposalModel(config, "key1").authorization_id()
    assert original.authorization_id() != DshProposalModel(config, "key2").authorization_id()
    assert original.authorization_id() != DshProposalModel(
        ModelProviderConfig("https://other.test", "anthropic-messages", "fake"), "key1").authorization_id()


@pytest.mark.parametrize("seen_rule,seen_output", [(None, None), ("company-reading-v1", 768),
                                                  ("company-reading-v2", 1024),
                                                  ("company-reading-v2", 3072),
                                                  ("company-reading-v4", 3072),
                                                  ("company-reading-future", 3072)])
def test_stale_consent_dialog_cannot_expand_egress(tmp_path, monkeypatch, seen_rule, seen_output):
    _, project, source, model, client = client_setup(tmp_path, monkeypatch)
    route = f"/api/workspace/projects/{project.id}/reading-authorization"
    body = {"consent": True, "config_id": model.authorization_id(),
            "request_key": "synthetic-stale-consent", "source_hashes": [source.content_sha256],
            "max_requests": 2}
    if seen_rule is not None:
        body.update(rule_version=seen_rule, max_output_tokens=seen_output)
    response = client.post(route, json=body, headers={
        "Origin": "http://127.0.0.1", "X-Settings-Token": "synthetic-operator"})
    assert response.status_code == 409
    assert client.get(route).json()["authorizations"] == []
    assert model._runs == 0


def test_no_model_readable_and_team_disabled(tmp_path):
    from fastapi import FastAPI

    from project_watch.model_settings import ModelSettings
    from project_watch.reading_authorization_api import attach_reading_authorization
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Name only")
    route = f"/api/workspace/projects/{project.id}/reading-authorization"
    client = TestClient(create_app(repo.db_path), base_url="http://127.0.0.1")
    assert client.get(route).json()["model"] is None
    app = FastAPI()
    attach_reading_authorization(app, repo, ModelSettings(), team_mode=True)
    assert TestClient(app, base_url="http://127.0.0.1").get(route).status_code == 404
