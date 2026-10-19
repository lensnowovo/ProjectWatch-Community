"""Synthetic team identities never reuse real credentials."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from project_watch.api import create_team_app
from project_watch.repository import ProjectRepository
from project_watch.team_auth import AuthenticationError, AuthorizationError, TeamAuthStore


def _store(tmp_path: Path) -> tuple[TeamAuthStore, ProjectRepository]:
    database = tmp_path / "watch.sqlite3"
    repository = ProjectRepository(database)
    return TeamAuthStore(database), repository


def test_bootstrap_login_session_and_role_matrix(tmp_path: Path) -> None:
    auth, repo = _store(tmp_path)
    admin = auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    with pytest.raises(ValueError, match="already bootstrapped"):
        auth.bootstrap_admin("other@example.test", "synthetic-admin-password")
    project = repo.create_project("Synthetic Team Project")
    member = auth.create_user("member@example.test", "synthetic-member-password")
    auth.grant_project_role(project.id, member.id, "viewer")

    token, csrf = auth.login("member@example.test", "synthetic-member-password")
    session = auth.session(token)
    assert session is not None
    assert session.user_id == member.id
    assert auth.validate_csrf(token, csrf)
    assert not auth.validate_csrf(token, "wrong")
    auth.require(member.id, project.id, "read")
    with pytest.raises(AuthorizationError):
        auth.require(member.id, project.id, "propose")
    with pytest.raises(AuthorizationError):
        auth.require(member.id, project.id, "review")
    auth.require(admin.id, project.id, "review")

    auth.grant_project_role(project.id, member.id, "analyst")
    auth.require(member.id, project.id, "propose")
    with pytest.raises(AuthorizationError):
        auth.require(member.id, project.id, "review")
    auth.grant_project_role(project.id, member.id, "reviewer")
    auth.require(member.id, project.id, "review")
    auth.revoke_project_role(project.id, member.id)
    with pytest.raises(AuthorizationError):
        auth.require(member.id, project.id, "read")

    auth.logout(token)
    assert auth.session(token) is None


def test_wrong_password_lockout_and_user_deactivation(tmp_path: Path) -> None:
    auth, _ = _store(tmp_path)
    user = auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    with pytest.raises(AuthenticationError):
        auth.login("missing@example.test", "synthetic-admin-password")
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            auth.login("admin@example.test", "wrong-password-here")
    with pytest.raises(AuthenticationError):
        auth.login("admin@example.test", "synthetic-admin-password")
    auth.unlock_user(user.id)
    token, _ = auth.login("admin@example.test", "synthetic-admin-password")
    auth.deactivate_user(user.id)
    assert auth.session(token) is None


def test_session_security_audit_is_bounded_and_contains_no_credentials(tmp_path: Path) -> None:
    auth, _ = _store(tmp_path)
    admin = auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    initial_events = len(auth.list_security_events())
    with pytest.raises(AuthenticationError):
        auth.login("unknown@example.test", "synthetic-unknown-password")
    assert len(auth.list_security_events()) == initial_events
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            auth.login(admin.username, "synthetic-wrong-password")
    events = auth.list_security_events()
    assert sum(event.action == "login_failed" for event in events) == 5
    assert sum(event.action == "account_locked" for event in events) == 1
    with pytest.raises(AuthenticationError):
        auth.login(admin.username, "synthetic-admin-password")
    assert len(auth.list_security_events()) == len(events)

    auth.unlock_user(admin.id, actor_user_id=admin.id)
    token, _ = auth.login(admin.username, "synthetic-admin-password")
    assert auth.list_security_events()[0].action == "login_success"
    auth.logout(token)
    assert auth.list_security_events()[0].action == "logout"
    audit_count = len(auth.list_security_events())
    auth.logout(token)
    assert len(auth.list_security_events()) == audit_count
    assert all(
        secret not in str(event)
        for event in auth.list_security_events()
        for secret in (token, "synthetic-admin-password", "synthetic-wrong-password")
    )


def test_session_expiry_and_revocation_survive_restart(tmp_path: Path) -> None:
    auth, _ = _store(tmp_path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    token, _ = auth.login("admin@example.test", "synthetic-admin-password")
    restarted = TeamAuthStore(auth.db_path)
    assert restarted.session(token) is not None
    assert restarted.session(token, now=datetime.now(UTC) + timedelta(hours=9)) is None
    restarted.logout(token)
    assert auth.session(token) is None


def test_password_reset_and_offline_admin_recovery_revoke_credentials(tmp_path: Path) -> None:
    auth, repo = _store(tmp_path)
    admin = auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    member = auth.create_user("member@example.test", "synthetic-member-password")
    project = repo.create_project("Synthetic Recovery")
    auth.grant_project_role(project.id, member.id, "admin")
    member_session, _ = auth.login(member.username, "synthetic-member-password")
    secret, _ = auth.issue_executor_token(
        project.id, member.id, label="Synthetic recovery", permission="read"
    )
    with pytest.raises(AuthorizationError):
        auth.reset_password(admin.id, "synthetic-new-admin", actor_user_id=member.id)
    with pytest.raises(ValueError):
        auth.reset_password("missing", "synthetic-new-member", actor_user_id=admin.id)
    assert auth.session(member_session) is not None
    auth.reset_password(member.id, "synthetic-new-member", actor_user_id=admin.id)
    assert auth.session(member_session) is None
    assert auth.executor_grant(secret) is None
    with pytest.raises(AuthenticationError):
        auth.login(member.username, "synthetic-member-password")
    assert auth.login(member.username, "synthetic-new-member")

    admin_session, _ = auth.login(admin.username, "synthetic-admin-password")
    admin_secret, _ = auth.issue_executor_token(
        project.id, admin.id, label="Admin recovery", permission="read"
    )
    with pytest.raises(ValueError):
        auth.recover_admin_offline(member.username, "synthetic-new-password")
    recovered = auth.recover_admin_offline(admin.username, "synthetic-recovered-admin")
    assert recovered.id == admin.id
    assert auth.session(admin_session) is None
    assert auth.executor_grant(admin_secret) is None
    with pytest.raises(AuthenticationError):
        auth.login(admin.username, "synthetic-admin-password")
    assert auth.login(admin.username, "synthetic-recovered-admin")
    events = auth.list_security_events()
    recovery = next(event for event in events if event.action == "recover_admin_offline")
    assert recovery.actor_user_id is None and recovery.target_user_id == admin.id
    assert any(
        event.action == "reset_password" and event.actor_user_id == admin.id for event in events
    )


def test_cross_project_and_unassigned_users_denied(tmp_path: Path) -> None:
    auth, repo = _store(tmp_path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    member = auth.create_user("member@example.test", "synthetic-member-password")
    first = repo.create_project("Synthetic First")
    other = repo.create_project("Synthetic Other")
    auth.grant_project_role(first.id, member.id, "analyst")
    auth.require(member.id, first.id, "read")
    with pytest.raises(AuthorizationError):
        auth.require(member.id, other.id, "read")


def test_exact_user_lookup_and_project_member_directory(tmp_path: Path) -> None:
    auth, repo = _store(tmp_path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    first = repo.create_project("First")
    other = repo.create_project("Other")
    member = auth.create_user("member@example.test", "synthetic-member-password")
    outsider = auth.create_user("outsider@example.test", "synthetic-outsider-password")
    auth.grant_project_role(first.id, member.id, "analyst")
    auth.grant_project_role(other.id, outsider.id, "viewer")
    assert auth.find_active_user(" MEMBER@EXAMPLE.TEST ") == member
    assert auth.find_active_user("missing@example.test") is None
    listed = auth.list_project_members(first.id)
    assert [(item.user_id, item.username, item.role, item.active) for item in listed] == [
        (member.id, member.username, "analyst", True)
    ]
    auth.deactivate_user(member.id)
    assert auth.find_active_user(member.username) is None
    assert auth.list_project_members(first.id)[0].active is False


def test_security_events_follow_committed_identity_changes(tmp_path: Path) -> None:
    auth, repo = _store(tmp_path)
    admin = auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    project = repo.create_project("Synthetic Audit")
    member = auth.create_user(
        "member@example.test", "synthetic-member-password", actor_user_id=admin.id
    )
    auth.grant_project_role(project.id, member.id, "viewer", actor_user_id=admin.id)
    auth.grant_project_role(project.id, member.id, "viewer", actor_user_id=admin.id)
    auth.grant_project_role(project.id, member.id, "reviewer", actor_user_id=admin.id)
    with pytest.raises(ValueError, match="project does not exist"):
        auth.grant_project_role("missing", member.id, "viewer", actor_user_id=admin.id)
    auth.revoke_project_role(project.id, member.id, actor_user_id=admin.id)
    auth.deactivate_user(member.id, actor_user_id=admin.id)
    events = list(reversed(auth.list_security_events()))
    assert [event.action for event in events] == [
        "bootstrap_admin",
        "create_user",
        "grant_project_role",
        "grant_project_role",
        "revoke_project_role",
        "deactivate_user",
    ]
    assert events[1].actor_user_id == admin.id
    assert events[2].project_id == project.id
    assert events[3].previous_role == "viewer"
    assert events[3].new_role == "reviewer"
    assert events[4].previous_role == "reviewer"
    assert events[5].target_user_id == member.id


def test_executor_token_is_scoped_short_lived_and_revocable(tmp_path: Path) -> None:
    auth, repo = _store(tmp_path)
    admin = auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    first = repo.create_project("Synthetic Executor")
    other = repo.create_project("Other Executor")
    token, grant = auth.issue_executor_token(
        first.id, admin.id, label="Synthetic Codex", permission="propose", duration_minutes=5
    )
    assert token.startswith("pwe_")
    assert auth.executor_grant(token, permission="read") == grant
    assert auth.executor_grant(token, permission="propose") == grant
    assert auth.executor_grant(token, now=datetime.now(UTC) + timedelta(minutes=6)) is None
    assert all(token not in str(event) for event in auth.list_security_events())
    assert all(token not in str(item) for item in auth.list_executor_tokens(first.id))
    assert auth.list_executor_tokens(other.id) == []
    assert not auth.revoke_executor_token(other.id, grant.id, actor_user_id=admin.id)
    assert auth.executor_grant(token) is not None
    assert auth.revoke_executor_token(first.id, grant.id, actor_user_id=admin.id)
    assert auth.executor_grant(token) is None
    assert not auth.revoke_executor_token(first.id, grant.id, actor_user_id=admin.id)
    assert [event.action for event in auth.list_security_events()[:2]] == [
        "revoke_executor_token",
        "issue_executor_token",
    ]
    assert auth.list_security_events()[0].subject_id == grant.id


def test_executor_token_cannot_outlive_issuer_project_role(tmp_path: Path) -> None:
    auth, repo = _store(tmp_path)
    auth.bootstrap_admin("admin@example.test", "synthetic-admin-password")
    manager = auth.create_user("manager@example.test", "synthetic-manager-password")
    project = repo.create_project("Synthetic Delegation")
    auth.grant_project_role(project.id, manager.id, "admin")
    token, grant = auth.issue_executor_token(
        project.id, manager.id, label="WorkBuddy", permission="read"
    )
    assert auth.executor_grant(token, permission="read") == grant
    assert auth.executor_grant(token, permission="propose") is None
    auth.grant_project_role(project.id, manager.id, "reviewer")
    assert auth.executor_grant(token) is None
    with pytest.raises(AuthorizationError):
        auth.issue_executor_token(project.id, manager.id, label="Blocked", permission="read")


def test_team_factory_requires_explicit_origin_and_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "watch.sqlite3"
    ProjectRepository(database)
    monkeypatch.setenv("PROJECT_WATCH_DB_PATH", str(database))
    monkeypatch.delenv("PROJECT_WATCH_TEAM_ORIGIN", raising=False)
    with pytest.raises(RuntimeError, match="HTTPS origin"):
        create_team_app()
    monkeypatch.setenv("PROJECT_WATCH_TEAM_ORIGIN", "https://testserver")
    with pytest.raises(RuntimeError, match="bootstrap"):
        create_team_app()
    TeamAuthStore(database).bootstrap_admin("admin@example.test", "synthetic-admin-password")
    app = create_team_app()
    assert app.title.endswith("team alpha")


def test_team_factory_rejects_remote_proxy_before_opening_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "not-created.sqlite3"
    monkeypatch.setenv("PROJECT_WATCH_DB_PATH", str(database))
    monkeypatch.setenv("PROJECT_WATCH_TEAM_ORIGIN", "https://testserver")
    monkeypatch.setenv("PROJECT_WATCH_GDELT_PROXY_URL", "http://example.org:17891")
    with pytest.raises(ValueError, match="proxy"):
        create_team_app()
    assert not database.exists()
