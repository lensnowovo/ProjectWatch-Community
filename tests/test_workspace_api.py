from fastapi.testclient import TestClient

from project_watch.api import create_app


def test_workspace_pagination_and_reversible_inbox(tmp_path):
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    project = client.post("/api/projects", json={"name": "Synthetic layout"}).json()
    from project_watch.repository import ProjectRepository

    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    repo.add_source(
        project["id"], kind="document", reference="synthetic.txt", content_sha256="a" * 64
    )
    page = client.get("/api/workspace/events?limit=1").json()
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["project_name"] == "Synthetic layout"
    route = "/api/workspace/events/state"
    assert client.post(route, json={"id": item["id"], "status": "done"}).status_code == 403
    headers = {"Origin": "http://testserver"}
    assert (
        client.post(route, headers=headers, json={"id": item["id"], "status": "done"}).status_code
        == 200
    )
    assert client.get("/api/workspace/events?status=done").json()["items"][0]["id"] == item["id"]
    client.post(route, headers=headers, json={"id": item["id"], "status": "open"})
    assert client.get("/api/workspace/events?status=done").json()["items"] == []
    assert repo.list_observations(project["id"]) == []


def test_settings_require_operator_token_and_never_return_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_WATCH_SETTINGS_TOKEN", "synthetic-operator-token")
    client = TestClient(create_app(tmp_path / "watch.sqlite3"))
    assert client.get("/api/settings/model").status_code == 403
    headers = {"Origin": "http://testserver", "X-Settings-Token": "synthetic-operator-token"}
    assert client.get("/api/settings/model", headers=headers).json()["enabled"] is False
    data = {
        "base_url": "http://127.0.0.1",
        "protocol": "anthropic-messages",
        "model": "test",
        "key": "secret",
    }
    response = client.post("/api/settings/model/test", headers=headers, json=data)
    assert response.status_code == 422
    assert "secret" not in response.text


def test_projection_pages_and_sidecar_are_separate_from_facts(tmp_path):
    from project_watch.repository import ProjectRepository

    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Synthetic paging")
    for i in range(3):
        repo.add_source(
            project.id, kind="document", reference=f"sample-{i}.txt", content_sha256=f"{i:064x}"
        )
    client = TestClient(create_app(repo.db_path))
    first = client.get("/api/workspace/events?limit=2").json()
    second = client.get("/api/workspace/events?limit=2&offset=2").json()
    assert first["has_more"] is True and second["has_more"] is False
    assert len({r["id"] for r in first["items"] + second["items"]}) == 3
    projects = client.get("/api/workspace/projects?q=Synthetic").json()["items"]
    assert projects[0]["source_count"] == 3 and projects[0]["pending_count"] == 0
    assert client.get("/api/workspace/tasks").json()["items"] == []
    assert client.get("/api/workspace/events?limit=101").status_code == 422
    # Removing the optional UI sidecar resets only inbox classifications.
    from pathlib import Path

    Path(str(repo.db_path) + ".client.sqlite3").unlink()
    client = TestClient(create_app(repo.db_path))
    assert len(client.get("/api/workspace/events").json()["items"]) == 3
    assert len(repo.list_sources(project.id)) == 3


def test_local_workspace_routes_disabled_in_team_mode(tmp_path):
    from fastapi import FastAPI

    from project_watch.repository import ProjectRepository
    from project_watch.workspace_api import attach_workspace_routes

    app = FastAPI()
    attach_workspace_routes(app, ProjectRepository(tmp_path / "watch.sqlite3"), team_mode=True)
    client = TestClient(app)
    for path in ["events", "projects", "tasks", "attention", "projects/any/reading", "projects/any/profile", "projects/any/profile/history"]:
        assert client.get("/api/workspace/" + path).status_code == 404
    assert client.patch("/api/workspace/projects/any/profile", json={"expected_version": 0}, headers={"Origin": "http://testserver"}).status_code == 404


def test_project_cards_hide_named_demo_before_pagination_without_deleting(tmp_path):
    from project_watch.repository import ProjectRepository

    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    real = repo.create_project("Company under review")
    repo.create_project("合成验收项目")
    repo.create_project("Synthetic demo")
    repo.add_source(real.id, kind="document", reference="company-note.txt", content_sha256="c" * 64)
    client = TestClient(create_app(repo.db_path))
    page = client.get("/api/workspace/projects?include_demo=false&limit=1").json()
    assert page["has_more"] is False
    assert page["items"][0]["id"] == real.id
    assert page["items"][0]["latest_material"] == "company-note.txt"
    assert page["items"][0]["signal_count"] == 0
    assert len(client.get("/api/workspace/projects").json()["items"]) == 3


def test_attention_excludes_material_arrivals_and_supports_search(tmp_path):
    from project_watch.repository import ProjectRepository

    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Example optics")
    repo.add_source(project.id, kind="document", reference="intro.txt", content_sha256="a" * 64)
    client = TestClient(create_app(repo.db_path))
    assert client.get("/api/workspace/attention").json() == {"items": []}
    assert client.get("/api/workspace/attention?q=other").json() == {"items": []}


def test_attention_respects_demo_filter_and_reversible_ignore(tmp_path):
    from datetime import UTC, datetime

    from project_watch.domain import SignalInput
    from project_watch.repository import ProjectRepository

    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    for name in ["Example optics", "Synthetic demo"]:
        project = repo.create_project(name)
        plan = repo.create_monitoring_plan(
            project.id, kind="rss", target="https://example.org/feed", cadence_minutes=60
        )
        repo.record_source_run(
            plan.id,
            status="completed",
            error_code=None,
            signals=(
                SignalInput(
                    external_id="1",
                    title="Company update",
                    url="https://example.org/1",
                    summary="Unverified",
                    published_at=datetime.now(UTC),
                    freshness="current",
                ),
            ),
        )
    client = TestClient(create_app(repo.db_path))
    items = client.get("/api/workspace/attention").json()["items"]
    assert len(items) == 1 and items[0]["project_name"] == "Example optics"
    assert len(client.get("/api/workspace/attention?include_demo=true").json()["items"]) == 2
    action = {"id": "signal:" + items[0]["id"], "status": "ignored"}
    headers = {"Origin": "http://testserver"}
    assert (
        client.post("/api/workspace/events/state", headers=headers, json=action).status_code == 200
    )
    assert client.get("/api/workspace/attention").json()["items"] == []
    action["status"] = "open"
    client.post("/api/workspace/events/state", headers=headers, json=action)
    assert len(client.get("/api/workspace/attention").json()["items"]) == 1
