"""Team API rejects anonymous, cross-project, wrong-role and spoofed review calls."""

from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.domain import FragmentInput
from project_watch.repository import ProjectRepository
from project_watch.team_auth import TeamAuthStore


def _team(tmp_path: Path) -> tuple[TestClient, TeamAuthStore, ProjectRepository]:
    path = tmp_path / "watch.sqlite3"
    repository = ProjectRepository(path)
    auth = TeamAuthStore(path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    client = TestClient(
        create_app(path, team_auth=auth, team_origin="https://testserver"),
        base_url="https://testserver",
    )
    return client, auth, repository


def _login(client: TestClient, username: str, password: str) -> str:
    result = client.post("/api/auth/login", json={"username": username, "password": password})
    assert result.status_code == 200
    assert "httponly" in result.headers["set-cookie"].lower()
    assert "secure" in result.headers["set-cookie"].lower()
    assert "samesite=strict" in result.headers["set-cookie"].lower()
    csrf = result.json()["csrf_token"]
    assert client.get("/api/auth/session").json()["csrf_token"] == csrf
    return csrf


def test_team_auth_session_csrf_and_admin_project_creation(tmp_path: Path) -> None:
    client, _, repository = _team(tmp_path)
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/projects", json={"name": "Blocked"}).status_code == 401

    csrf = _login(client, "admin@example.test", "synthetic-admin-password")
    assert client.post("/api/projects", json={"name": "Blocked"}).status_code == 403
    assert (
        client.post(
            "/api/projects",
            json={"name": "Blocked"},
            headers={"X-CSRF-Token": "wrong"},
        ).status_code
        == 403
    )
    created = client.post(
        "/api/projects",
        json={"name": "Synthetic Team"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201
    assert [item["id"] for item in client.get("/api/projects").json()] == [created.json()["id"]]
    event = repository.list_audit_events(created.json()["id"])[0]
    assert event.action == "project.created"
    assert event.actor_ref == client.get("/api/auth/session").json()["user_id"]
    assert (
        client.post(
            "/api/projects",
            json={"name": "Cross site"},
            headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
        ).status_code
        == 403
    )

    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/projects").status_code == 401


def test_financial_screen_requires_team_session_and_project_read(tmp_path: Path) -> None:
    client, auth, repo = _team(tmp_path)
    project = repo.create_project("Financial screen synthetic")
    path = f"/api/projects/{project.id}/financial-screen"
    assert client.get(path).status_code == 401
    admin_csrf = _login(client, "admin@example.test", "synthetic-admin-password")
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["analysis"]["status"] == "insufficient_data"
    assert client.get(f"/api/projects/{project.id}/monitoring/domains").status_code == 200

    auth.create_user("outside@example.test", "synthetic-outside-password")
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": admin_csrf}).status_code == 204
    _login(client, "outside@example.test", "synthetic-outside-password")
    assert client.get(path).status_code in {403, 404}
    assert client.get(f"/api/projects/{project.id}/monitoring/domains").status_code in {403, 404}


def test_identity_review_is_human_scoped_and_requires_evidence(tmp_path: Path) -> None:
    client, auth, repo = _team(tmp_path)
    project = repo.create_project("Example Project")
    other = repo.create_project("Other Project")
    path = f"/api/projects/{project.id}/identity"
    assert client.get(path).status_code == 401
    admin_csrf = _login(client, "admin@example.test", "synthetic-admin-password")
    actor_id = client.get("/api/auth/session").json()["user_id"]
    viewer = auth.create_user(
        "identity-viewer", "synthetic-viewer-password", actor_user_id=actor_id
    )
    auth.grant_project_role(project.id, viewer.id, "viewer", actor_user_id=actor_id)
    payload = {
        "legal_name": "Example Company Limited",
        "registration_code": None,
        "evidence_source_id": None,
        "evidence_locator": None,
        "evidence_url": "https://example.org/company",
        "note": "Checked the public company profile",
        "expected_version": 0,
    }
    assert client.post(path, json=payload).status_code == 403
    assert (
        client.post(
            path, json={**payload, "evidence_url": None}, headers={"X-CSRF-Token": admin_csrf}
        ).status_code
        == 422
    )
    verified = client.post(path, json=payload, headers={"X-CSRF-Token": admin_csrf})
    assert verified.status_code == 201
    assert verified.json()["actor_ref"] == actor_id
    assert verified.json()["actor_name"] == "admin@example.test"
    assert repo.get_project(project.id).name == "Example Project"
    assert repo.list_monitoring_plans(project.id) == []
    assert client.get(path).json()["status"] == "verified"
    assert client.get(path).json()["history"][0]["version"] == 1
    assert client.post(path, json=payload, headers={"X-CSRF-Token": admin_csrf}).status_code == 409

    viewer_client = TestClient(client.app, base_url="https://testserver")
    viewer_csrf = _login(viewer_client, "identity-viewer", "synthetic-viewer-password")
    assert viewer_client.get(path).status_code == 200
    assert viewer_client.get(f"/api/projects/{other.id}/identity").status_code == 404
    assert (
        viewer_client.post(
            f"{path}/reopen",
            json={"note": "Need another check", "expected_version": 1},
            headers={"X-CSRF-Token": viewer_csrf},
        ).status_code
        == 404
    )
    reopened = client.post(
        f"{path}/reopen",
        json={"note": "Need another check", "expected_version": 1},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert reopened.status_code == 201
    assert client.get(path).json()["status"] == "unverified"
    assert [item["action"] for item in client.get(path).json()["history"]] == ["reopen", "verify"]


def test_team_project_roles_and_verified_reviewer_identity(tmp_path: Path) -> None:
    client, auth, repo = _team(tmp_path)
    first = repo.create_project("Synthetic First")
    second = repo.create_project("Synthetic Second")
    admin_csrf = _login(client, "admin@example.test", "synthetic-admin-password")
    created = client.post(
        "/api/team/users",
        json={"username": "member@example.test", "password": "synthetic-member-password"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert created.status_code == 201
    member_id = created.json()["id"]
    assert "password" not in created.json()
    granted = client.put(
        f"/api/projects/{first.id}/members/{member_id}",
        json={"role": "viewer"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert granted.status_code == 204

    member_client = TestClient(client.app, base_url="https://testserver")
    member_csrf = _login(member_client, "member@example.test", "synthetic-member-password")
    assert [item["id"] for item in member_client.get("/api/projects").json()] == [first.id]
    first_page = client.get("/api/projects", params={"limit": 1}).json()
    second_page = client.get(
        "/api/projects",
        params={
            "limit": 1,
            "after_created_at": first_page[0]["created_at"],
            "after_id": first_page[0]["id"],
        },
    ).json()
    assert {first_page[0]["id"], second_page[0]["id"]} == {first.id, second.id}
    assert member_client.get(f"/api/projects/{second.id}").status_code == 404
    assert member_client.get(f"/api/projects/{first.id}").status_code == 200
    assert member_client.get(f"/api/projects/{first.id}/capabilities").json() == {
        "read": True,
        "propose": False,
        "review": False,
        "manage": False,
    }
    assert member_client.get(f"/api/projects/{second.id}/capabilities").status_code == 404
    assert member_client.get(f"/api/projects/{second.id}/metric-series").status_code == 404
    assert member_client.get(f"/api/projects/{first.id}/metric-series").status_code == 200
    assert (
        member_client.post(
            f"/api/projects/{first.id}/tasks",
            json={
                "contract_version": 1,
                "kind": "coverage",
                "source_ids": [],
                "idempotency_key": "x",
            },
            headers={"X-CSRF-Token": member_csrf},
        ).status_code
        == 404
    )

    source = repo.add_source(
        first.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64
    )
    observation = repo.add_observation(
        first.id,
        source_id=source.id,
        subject="经营情况",
        statement="Synthetic revenue claim",
        source_locator="line 1",
    )
    assert (
        member_client.post(
            f"/api/projects/{first.id}/observations/{observation.id}/confirm",
            json={"reviewer": "spoofed", "expected_version": 1},
            headers={"X-CSRF-Token": member_csrf},
        ).status_code
        == 404
    )
    auth.grant_project_role(first.id, member_id, "reviewer")
    assert member_client.get(f"/api/projects/{first.id}/capabilities").json() == {
        "read": True,
        "propose": True,
        "review": True,
        "manage": False,
    }
    reviewed = member_client.post(
        f"/api/projects/{first.id}/observations/{observation.id}/confirm",
        json={"reviewer": "spoofed", "expected_version": 1},
        headers={"X-CSRF-Token": member_csrf},
    )
    assert reviewed.status_code == 200
    assert repo.review_history(observation.id)[0].reviewer == member_id
    assert repo.list_audit_events(first.id)[-1].actor_kind == "verified_user"
    assert repo.list_audit_events(first.id)[-1].actor_ref == member_id

    rejected_candidate = repo.add_observation(
        first.id,
        source_id=source.id,
        subject="经营情况",
        statement="Unsupported second claim",
        source_locator="line 2",
    )
    reject_path = f"/api/projects/{first.id}/observations/{rejected_candidate.id}/reject"
    assert member_client.post(reject_path, json={"expected_version": 1}).status_code == 403
    rejected = member_client.post(
        reject_path,
        json={"reviewer": "spoofed", "expected_version": 1},
        headers={"X-CSRF-Token": member_csrf},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert repo.review_history(rejected_candidate.id)[0].reviewer == member_id
    assert repo.review_history(rejected_candidate.id)[0].action == "reject"
    assert repo.list_audit_events(first.id)[-1].action == "candidate.rejected"
    assert (
        member_client.post(
            f"/api/projects/{second.id}/observations/{rejected_candidate.id}/reject",
            json={"expected_version": 1},
            headers={"X-CSRF-Token": member_csrf},
        ).status_code
        == 404
    )

    auth.revoke_project_role(first.id, member_id)
    assert member_client.get(f"/api/projects/{first.id}").status_code == 404
    auth.deactivate_user(member_id)
    assert member_client.get("/api/projects").status_code == 401


def test_team_member_setup_is_scoped_and_requires_manager_and_csrf(tmp_path: Path) -> None:
    admin_client, auth, repo = _team(tmp_path)
    project = repo.create_project("Managed")
    other = repo.create_project("Other")
    _login(admin_client, "admin@example.test", "synthetic-admin-password")
    manager = auth.create_user("manager@example.test", "synthetic-manager-password")
    member = auth.create_user("member@example.test", "synthetic-member-password")
    outside = auth.create_user("outside@example.test", "synthetic-outside-password")
    auth.grant_project_role(project.id, manager.id, "admin")
    auth.grant_project_role(other.id, outside.id, "viewer")

    manager_client = TestClient(admin_client.app, base_url="https://testserver")
    manager_csrf = _login(manager_client, manager.username, "synthetic-manager-password")
    path = f"/api/projects/{project.id}/members"
    assert manager_client.get(path).status_code == 200
    assert manager_client.get(f"/api/projects/{other.id}/members").status_code == 404
    assert (
        manager_client.post(path, json={"username": member.username, "role": "viewer"}).status_code
        == 403
    )
    added = manager_client.post(
        path,
        json={"username": " MEMBER@EXAMPLE.TEST ", "role": "analyst"},
        headers={"X-CSRF-Token": manager_csrf},
    )
    assert added.status_code == 201
    assert added.json() == {
        "user_id": member.id,
        "username": member.username,
        "role": "analyst",
        "active": True,
    }
    page = manager_client.get(path).json()
    assert {(item["username"], item["role"]) for item in page} == {
        (member.username, "analyst"),
        (manager.username, "admin"),
    }
    assert all("password" not in item for item in page)
    assert (
        manager_client.post(
            f"/api/projects/{other.id}/members",
            json={"username": member.username, "role": "viewer"},
            headers={"X-CSRF-Token": manager_csrf},
        ).status_code
        == 404
    )
    assert (
        manager_client.post(
            path,
            json={"username": "missing@example.test", "role": "viewer"},
            headers={"X-CSRF-Token": manager_csrf},
        ).status_code
        == 404
    )

    viewer_client = TestClient(admin_client.app, base_url="https://testserver")
    viewer_csrf = _login(viewer_client, member.username, "synthetic-member-password")
    assert viewer_client.get(path).status_code == 404
    assert (
        viewer_client.post(
            path,
            json={"username": outside.username, "role": "viewer"},
            headers={"X-CSRF-Token": viewer_csrf},
        ).status_code
        == 404
    )
    assert admin_client.get(path, params={"limit": 1}).json()[0]["username"] == manager.username
    assert (
        admin_client.get(path, params={"after_username": manager.username}).json()[0]["username"]
        == member.username
    )


def test_team_http_requires_https(tmp_path: Path) -> None:
    client, _, _ = _team(tmp_path)
    http_client = TestClient(client.app, base_url="http://testserver")
    assert (
        http_client.post(
            "/api/auth/login",
            json={"username": "admin@example.test", "password": "synthetic-admin-password"},
        ).status_code
        == 400
    )


def test_incremental_review_requires_reviewer_and_uses_verified_identity(tmp_path: Path) -> None:
    client, auth, repo = _team(tmp_path)
    project = repo.create_project("Review Impact Team")
    old_source = repo.add_source(
        project.id, kind="document", reference="old.txt", content_sha256="a" * 64
    )
    repo.enqueue_intake(project.id, old_source.id)
    old_job = repo.claim_next_intake()
    repo.complete_intake(
        old_job.id,
        [FragmentInput(1, "line 1", "营业收入待核对")],
        native_topic="财务表现",
        native_topic_basis="content",
    )
    observation = repo.add_observation(
        project.id,
        source_id=old_source.id,
        subject="财务表现",
        statement="旧判断待核对",
        source_locator="line 1",
    )
    repo.confirm_observation(
        observation.id, reviewer="admin", expected_version=1, reviewer_verified=True
    )
    new_source = repo.add_source(
        project.id, kind="document", reference="new.txt", content_sha256="b" * 64
    )
    repo.enqueue_intake(project.id, new_source.id)
    new_job = repo.claim_next_intake()
    repo.complete_intake(
        new_job.id,
        [FragmentInput(1, "line 1", "净利润待核对")],
        native_topic="财务表现",
        native_topic_basis="content",
    )
    impact = repo.list_review_impacts(project.id)[0]

    member = auth.create_user("impact-reviewer@example.test", "synthetic-reviewer-password")
    auth.grant_project_role(project.id, member.id, "viewer")
    member_client = TestClient(client.app, base_url="https://testserver")
    csrf = _login(member_client, member.username, "synthetic-reviewer-password")
    path = f"/api/projects/{project.id}/review-impacts/{impact.id}/resolve"
    assert len(member_client.get(f"/api/projects/{project.id}/review-impacts").json()) == 1
    assert (
        member_client.post(
            path,
            json={"resolution": "follow_up", "note": "需要核实", "reviewer": "spoofed"},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 404
    )
    auth.grant_project_role(project.id, member.id, "reviewer")
    reviewed = member_client.post(
        path,
        json={"resolution": "follow_up", "note": "需要核实", "reviewer": "spoofed"},
        headers={"X-CSRF-Token": csrf},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["reviewer_ref"] == member.id
    assert repo.get_observation(observation.id).status == "confirmed"
    assert repo.list_audit_events(project.id)[-1].actor_ref == member.id


def test_source_comparison_respects_team_read_membership(tmp_path: Path) -> None:
    client, auth, repo = _team(tmp_path)
    first = repo.create_project("Synthetic Comparison")
    other = repo.create_project("Other Comparison")
    _login(client, "admin@example.test", "synthetic-admin-password")
    before = client.post(
        f"/api/projects/{first.id}/files",
        files={"file": ("report.txt", b"Old synthetic value", "text/plain")},
        headers={"X-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"]},
    ).json()["source"]["id"]
    after = client.post(
        f"/api/projects/{first.id}/files",
        files={"file": ("report.txt", b"New synthetic value", "text/plain")},
        headers={"X-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"]},
    ).json()["source"]["id"]
    for _ in range(2):
        assert client.app.state.intake_worker.process_one().status == "completed"
    url = f"/api/projects/{first.id}/sources/{after}/compare"

    member = auth.create_user("compare-viewer@example.test", "synthetic-viewer-password")
    auth.grant_project_role(other.id, member.id, "viewer")
    member_client = TestClient(client.app, base_url="https://testserver")
    _login(member_client, member.username, "synthetic-viewer-password")
    assert member_client.get(url, params={"before_source_id": before}).status_code == 404
    auth.grant_project_role(first.id, member.id, "viewer")
    visible = member_client.get(url, params={"before_source_id": before})
    assert visible.status_code == 200
    assert visible.json()["counts"]["changed"] == 1


def test_every_team_api_route_denies_anonymous_requests(tmp_path: Path) -> None:
    client, _, _ = _team(tmp_path)
    for route in client.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path == "/api/auth/login":
            continue
        filled = path.replace("{project_id}", "missing").replace("{source_id}", "missing")
        filled = filled.replace("{observation_id}", "missing").replace("{job_id}", "missing")
        filled = filled.replace("{task_id}", "missing").replace("{plan_id}", "missing")
        filled = filled.replace("{user_id}", "missing").replace("{token_id}", "missing")
        for method in route.methods:
            response = client.request(method, filled)
            assert response.status_code == 401, f"{method} {path} returned {response.status_code}"


def test_team_candidate_audit_and_indirect_resource_isolation(tmp_path: Path) -> None:
    client, auth, repo = _team(tmp_path)
    first = repo.create_project("Synthetic Authorized")
    other = repo.create_project("Synthetic Private")
    member = auth.create_user("analyst@example.test", "synthetic-analyst-password")
    auth.grant_project_role(first.id, member.id, "analyst")
    source = repo.add_source(
        first.id, kind="document", reference="synthetic.txt", content_sha256="c" * 64
    )
    private_source = repo.add_source(
        other.id, kind="document", reference="private.txt", content_sha256="d" * 64
    )
    member_client = TestClient(client.app, base_url="https://testserver")
    csrf = _login(member_client, "analyst@example.test", "synthetic-analyst-password")
    assert member_client.get(f"/api/sources/{private_source.id}/fragments").status_code == 404
    assert member_client.get(f"/api/sources/{source.id}/fragments").status_code == 200
    assert (
        member_client.get(
            f"/api/projects/{first.id}/sources/{private_source.id}/fragments"
        ).status_code
        == 404
    )
    assert (
        member_client.get(f"/api/projects/{first.id}/sources/{source.id}/fragments").status_code
        == 200
    )
    created = member_client.post(
        f"/api/projects/{first.id}/observations",
        json={
            "source_id": source.id,
            "subject": "经营情况",
            "statement": "Synthetic candidate",
            "source_locator": "line 1",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201
    event = repo.list_audit_events(first.id)[-1]
    assert event.actor_kind == "verified_user"
    assert event.actor_ref == member.id
    assert event.channel == "team_web"
    assert (
        member_client.post(
            f"/api/projects/{other.id}/observations",
            json={
                "source_id": private_source.id,
                "subject": "经营情况",
                "statement": "Not authorized",
                "source_locator": "line 1",
            },
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 404
    )


def test_security_audit_and_account_management_require_admin(tmp_path: Path) -> None:
    client, auth, repo = _team(tmp_path)
    project = repo.create_project("Synthetic Managed")
    admin_csrf = _login(client, "admin@example.test", "synthetic-admin-password")
    created = client.post(
        "/api/team/users",
        json={"username": "member@example.test", "password": "synthetic-member-password"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    member_id = created.json()["id"]
    assert (
        client.put(
            f"/api/projects/{project.id}/members/{member_id}",
            json={"role": "reviewer"},
            headers={"X-CSRF-Token": admin_csrf},
        ).status_code
        == 204
    )
    member_client = TestClient(client.app, base_url="https://testserver")
    member_csrf = _login(member_client, "member@example.test", "synthetic-member-password")
    assert member_client.get("/api/team/security-events").status_code == 403
    assert (
        member_client.post(
            f"/api/team/users/{member_id}/deactivate",
            headers={"X-CSRF-Token": member_csrf},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/team/users/{auth.get_user(member_id).id}/unlock",
            headers={"X-CSRF-Token": admin_csrf},
        ).status_code
        == 204
    )
    assert (
        client.post(
            f"/api/team/users/{member_id}/deactivate",
            headers={"X-CSRF-Token": admin_csrf},
        ).status_code
        == 204
    )
    events = client.get("/api/team/security-events")
    assert events.status_code == 200
    assert [event["action"] for event in events.json()][:4] == [
        "deactivate_user",
        "login_success",
        "grant_project_role",
        "create_user",
    ]
    assert events.json()[0]["actor_user_id"] == client.get("/api/auth/session").json()["user_id"]
    assert member_client.get("/api/projects").status_code == 401
    assert (
        client.post(
            f"/api/team/users/{client.get('/api/auth/session').json()['user_id']}/deactivate",
            headers={"X-CSRF-Token": admin_csrf},
        ).status_code
        == 409
    )


def test_admin_password_reset_revokes_session_and_requires_csrf(tmp_path: Path) -> None:
    client, auth, _ = _team(tmp_path)
    admin_csrf = _login(client, "admin@example.test", "synthetic-admin-password")
    member = auth.create_user("member@example.test", "synthetic-member-password")
    member_client = TestClient(client.app, base_url="https://testserver")
    member_csrf = _login(member_client, member.username, "synthetic-member-password")
    url = f"/api/team/users/{member.id}/reset-password"
    body = {"password": "synthetic-new-member"}
    assert client.post(url, json=body).status_code == 403
    assert (
        member_client.post(url, json=body, headers={"X-CSRF-Token": member_csrf}).status_code == 403
    )
    assert (
        client.post(
            url, json={"password": "short"}, headers={"X-CSRF-Token": admin_csrf}
        ).status_code
        == 422
    )
    assert client.post(url, json=body, headers={"X-CSRF-Token": admin_csrf}).status_code == 204
    assert member_client.get("/api/auth/session").status_code == 401
    assert (
        member_client.post(
            "/api/auth/login",
            json={"username": member.username, "password": "synthetic-member-password"},
        ).status_code
        == 401
    )
    assert _login(member_client, member.username, "synthetic-new-member")
    events = client.get("/api/team/security-events").json()
    reset = next(event for event in events if event["action"] == "reset_password")
    assert reset["target_user_id"] == member.id


def test_executor_token_issue_list_and_revoke_are_manager_only(tmp_path: Path) -> None:
    client, auth, repo = _team(tmp_path)
    project = repo.create_project("Synthetic Token Project")
    admin_csrf = _login(client, "admin@example.test", "synthetic-admin-password")
    issue = client.post(
        f"/api/projects/{project.id}/executor-tokens",
        json={"label": "Synthetic Codex", "permission": "propose", "duration_minutes": 5},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert issue.status_code == 201
    secret = issue.json()["token"]
    token_id = issue.json()["id"]
    assert issue.headers["Cache-Control"] == "no-store"
    listed = client.get(f"/api/projects/{project.id}/executor-tokens")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert "token" not in listed.json()[0]
    assert auth.executor_grant(secret, permission="propose").id == token_id
    member = auth.create_user("reader@example.test", "synthetic-reader-password")
    auth.grant_project_role(project.id, member.id, "viewer")
    reader_client = TestClient(client.app, base_url="https://testserver")
    reader_csrf = _login(reader_client, "reader@example.test", "synthetic-reader-password")
    assert reader_client.get(f"/api/projects/{project.id}/executor-tokens").status_code == 404
    assert (
        reader_client.post(
            f"/api/projects/{project.id}/executor-tokens",
            json={"label": "Blocked", "permission": "read"},
            headers={"X-CSRF-Token": reader_csrf},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/projects/{project.id}/executor-tokens/{token_id}",
            headers={"X-CSRF-Token": admin_csrf},
        ).status_code
        == 204
    )
    assert auth.executor_grant(secret) is None
