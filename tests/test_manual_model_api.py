from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.domain import FragmentInput
from project_watch.repository import ProjectRepository


class Model:
    calls = 0
    config = "test-config"

    def disclosure(self):
        return {"config_id": self.config, "model": "synthetic", "enabled": True}

    def propose(self, evidence):
        self.calls += 1
        return {
            "source_id": evidence.source_id,
            "source_sha256": evidence.source_sha256,
            "source_locator": evidence.source_locator,
            "subject": "财务表现",
            "statement": "收入",
            "number": "42",
            "unit": "万元",
            "period": "2026",
            "basis": "forecast",
        }


def setup(tmp_path: Path, model):
    path = tmp_path / "watch.sqlite3"
    repo = ProjectRepository(path)
    project = repo.create_project("Synthetic")
    source = repo.add_source(
        project.id, kind="document", reference="sample.txt", content_sha256="a" * 64
    )
    repo.enqueue_intake(project.id, source.id)
    job = repo.claim_next_intake()
    repo.complete_intake(
        job.id, [FragmentInput(ordinal=1, locator="line 1", text="2026年预测收入42万元。")]
    )
    client = TestClient(create_app(path, proposal_model=model), base_url="http://127.0.0.1")
    return repo, project, source, client


def preview(client, project, source):
    return client.get(
        f"/api/projects/{project.id}/ai-extraction",
        params={"source_id": source.id, "source_locator": "line 1"},
    )


def test_explicit_consent_single_use_ticket_and_pending_candidate(tmp_path):
    model = Model()
    repo, project, source, client = setup(tmp_path, model)
    disclosed = preview(client, project, source).json()
    path = f"/api/projects/{project.id}/ai-extraction"
    body = {"ticket": disclosed["ticket"], "consent": True}
    assert client.post(path, json=body).status_code == 403
    headers = {"Origin": "http://127.0.0.1"}
    assert client.post(path, json={**body, "consent": False}, headers=headers).status_code == 422
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 200
    assert result.json()["status"] == "candidate"
    assert repo.list_observations(project.id)[0].basis == "forecast"
    assert repo.list_observations(project.id, status="confirmed") == []
    assert client.post(path, json=body, headers=headers).status_code == 409
    assert model.calls == 1


def test_disabled_wrong_project_changed_config_and_source(tmp_path):
    model = Model()
    repo, project, source, client = setup(tmp_path, model)
    other = repo.create_project("Other")
    assert preview(client, other, source).status_code == 404
    ticket = preview(client, project, source).json()["ticket"]
    model.config = "changed"
    path = f"/api/projects/{project.id}/ai-extraction"
    assert (
        client.post(
            path, json={"consent": True, "ticket": ticket}, headers={"Origin": "http://127.0.0.1"}
        ).status_code
        == 409
    )
    assert model.calls == 0
    disabled = TestClient(create_app(repo.db_path))
    assert preview(disabled, project, source).status_code == 503


def test_failure_does_not_echo_provider_or_write_fact(tmp_path):
    from project_watch.executors import ExecutorUnavailable

    class Failed(Model):
        def propose(self, evidence):
            raise ExecutorUnavailable("SECRET upstream prompt")

    repo, project, source, client = setup(tmp_path, Failed())
    ticket = preview(client, project, source).json()["ticket"]
    result = client.post(
        f"/api/projects/{project.id}/ai-extraction",
        json={"consent": True, "ticket": ticket},
        headers={"Origin": "http://127.0.0.1"},
    )
    assert result.status_code == 502
    assert "SECRET" not in result.text
    assert repo.list_observations(project.id) == []
