"""Optional self-hosted identities for a future single-institution team mode.

This module has no HTTP entry point by itself. The unauthenticated local preview
continues to run without the optional Argon2 dependency.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

ProjectRole = Literal["viewer", "analyst", "reviewer", "admin"]
Permission = Literal["read", "propose", "review", "manage"]
ExecutorPermission = Literal["read", "propose"]

_ALLOWED: dict[ProjectRole, frozenset[Permission]] = {
    "viewer": frozenset({"read"}),
    "analyst": frozenset({"read", "propose"}),
    "reviewer": frozenset({"read", "propose", "review"}),
    "admin": frozenset({"read", "propose", "review", "manage"}),
}
_USERNAME = re.compile(r"^[a-z0-9_.@-]{3,80}$")
_SESSION_HOURS = 8
_LOCK_MINUTES = 15


class AuthenticationError(RuntimeError):
    """Credentials or session are invalid, without disclosing which part."""


class AuthorizationError(RuntimeError):
    """An authenticated user lacks a required project permission."""


@dataclass(frozen=True, slots=True)
class TeamUser:
    id: str
    username: str
    global_admin: bool
    active: bool


@dataclass(frozen=True, slots=True)
class ProjectMember:
    user_id: str
    username: str
    role: ProjectRole
    active: bool


@dataclass(frozen=True, slots=True)
class TeamSession:
    user_id: str
    username: str
    global_admin: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TeamSecurityEvent:
    id: str
    actor_user_id: str | None
    action: str
    target_user_id: str | None
    project_id: str | None
    subject_id: str | None
    previous_role: str | None
    new_role: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutorGrant:
    id: str
    project_id: str
    issued_by: str
    label: str
    permission: ExecutorPermission
    expires_at: datetime
    revoked_at: datetime | None


class TeamAuthStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise ValueError("team auth requires an existing Project Watch database")
        self.hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
        self._dummy_hash = self.hasher.hash("not-a-real-user-password")
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS team_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    global_admin INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS team_memberships (
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    user_id TEXT NOT NULL REFERENCES team_users(id),
                    role TEXT NOT NULL CHECK (role IN ('viewer', 'analyst', 'reviewer', 'admin')),
                    PRIMARY KEY (project_id, user_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS team_sessions (
                    token_sha256 TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES team_users(id),
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS team_sessions_user
                   ON team_sessions (user_id, expires_at)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS team_security_events (
                    id TEXT PRIMARY KEY,
                    actor_user_id TEXT,
                    action TEXT NOT NULL,
                    target_user_id TEXT,
                    project_id TEXT,
                    subject_id TEXT,
                    previous_role TEXT,
                    new_role TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            security_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(team_security_events)")
            }
            if "subject_id" not in security_columns:
                connection.execute("ALTER TABLE team_security_events ADD COLUMN subject_id TEXT")
            connection.execute(
                """CREATE INDEX IF NOT EXISTS team_security_events_order
                   ON team_security_events (created_at, id)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS team_executor_tokens (
                    id TEXT PRIMARY KEY,
                    token_sha256 TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    issued_by TEXT NOT NULL REFERENCES team_users(id),
                    label TEXT NOT NULL,
                    permission TEXT NOT NULL CHECK (permission IN ('read', 'propose')),
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS team_executor_tokens_project
                   ON team_executor_tokens (project_id, expires_at)"""
            )

    @staticmethod
    def _append_security_event(
        connection: sqlite3.Connection,
        *,
        action: str,
        actor_user_id: str | None = None,
        target_user_id: str | None = None,
        project_id: str | None = None,
        subject_id: str | None = None,
        previous_role: str | None = None,
        new_role: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO team_security_events
               (id, actor_user_id, action, target_user_id, project_id,
                subject_id, previous_role, new_role, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                actor_user_id,
                action,
                target_user_id,
                project_id,
                subject_id,
                previous_role,
                new_role,
                datetime.now(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _username(value: str) -> str:
        username = value.strip().lower()
        if not _USERNAME.fullmatch(username):
            raise ValueError("username must be 3-80 lowercase ASCII letters, digits or ._@-")
        return username

    @staticmethod
    def _password(value: str) -> str:
        if not 12 <= len(value) <= 128:
            raise ValueError("password must contain 12-128 characters")
        return value

    @staticmethod
    def _user(row: sqlite3.Row) -> TeamUser:
        return TeamUser(
            id=str(row["id"]),
            username=str(row["username"]),
            global_admin=bool(row["global_admin"]),
            active=bool(row["active"]),
        )

    def bootstrap_admin(self, username: str, password: str) -> TeamUser:
        username = self._username(username)
        password_hash = self.hasher.hash(self._password(password))
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM team_users LIMIT 1").fetchone():
                raise ValueError("team identities already bootstrapped")
            user_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO team_users
                   (id, username, password_hash, global_admin, created_at)
                   VALUES (?, ?, ?, 1, ?)""",
                (user_id, username, password_hash, datetime.now(UTC).isoformat()),
            )
            self._append_security_event(
                connection, action="bootstrap_admin", target_user_id=user_id
            )
            return TeamUser(user_id, username, True, True)

    def create_user(
        self, username: str, password: str, *, actor_user_id: str | None = None
    ) -> TeamUser:
        username = self._username(username)
        password_hash = self.hasher.hash(self._password(password))
        with closing(self._connect()) as connection, connection:
            user_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO team_users
                   (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)""",
                (user_id, username, password_hash, datetime.now(UTC).isoformat()),
            )
            self._append_security_event(
                connection,
                action="create_user",
                actor_user_id=actor_user_id,
                target_user_id=user_id,
            )
            return TeamUser(user_id, username, False, True)

    def get_user(self, user_id: str) -> TeamUser | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM team_users WHERE id = ?", (user_id,)).fetchone()
            return self._user(row) if row is not None else None

    def find_active_user(self, username: str) -> TeamUser | None:
        username = self._username(username)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM team_users WHERE username = ? AND active = 1", (username,)
            ).fetchone()
            return self._user(row) if row is not None else None

    def list_project_members(
        self, project_id: str, *, limit: int = 100, after_username: str | None = None
    ) -> list[ProjectMember]:
        if not 1 <= limit <= 100:
            raise ValueError("member page limit must be between 1 and 100")
        cursor = self._username(after_username) if after_username is not None else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT u.id, u.username, u.active, m.role
                   FROM team_memberships m JOIN team_users u ON u.id = m.user_id
                   WHERE m.project_id = ? AND u.username > ?
                   ORDER BY u.username LIMIT ?""",
                (project_id, cursor, limit),
            ).fetchall()
        return [
            ProjectMember(str(row["id"]), str(row["username"]), row["role"], bool(row["active"]))
            for row in rows
        ]

    def is_bootstrapped(self) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT 1 FROM team_users LIMIT 1").fetchone() is not None

    def login(self, username: str, password: str) -> tuple[str, str]:
        try:
            username = self._username(username)
        except ValueError as exc:
            raise AuthenticationError("invalid credentials") from exc
        now = datetime.now(UTC)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM team_users WHERE username = ?", (username,)
            ).fetchone()
            stored_hash = str(row["password_hash"]) if row is not None else self._dummy_hash
            try:
                valid_password = self.hasher.verify(stored_hash, password)
            except (VerifyMismatchError, VerificationError):
                valid_password = False
            if row is None or not row["active"]:
                raise AuthenticationError("invalid credentials")
            locked_until = (
                datetime.fromisoformat(str(row["locked_until"]))
                if row["locked_until"] is not None
                else None
            )
            if locked_until is not None and now < locked_until:
                raise AuthenticationError("invalid credentials")
            if not valid_password:
                failures = int(row["failed_attempts"]) + 1
                next_lock = (
                    (now + timedelta(minutes=_LOCK_MINUTES)).isoformat() if failures >= 5 else None
                )
                connection.execute(
                    """UPDATE team_users SET failed_attempts = ?, locked_until = ? WHERE id = ?""",
                    (failures, next_lock, row["id"]),
                )
                self._append_security_event(
                    connection, action="login_failed", target_user_id=str(row["id"])
                )
                if next_lock is not None:
                    self._append_security_event(
                        connection, action="account_locked", target_user_id=str(row["id"])
                    )
                # Returning after the transaction commits preserves the lockout counter.
            else:
                connection.execute(
                    """UPDATE team_users
                       SET failed_attempts = 0, locked_until = NULL WHERE id = ?""",
                    (row["id"],),
                )
                token = secrets.token_urlsafe(32)
                connection.execute(
                    """INSERT INTO team_sessions
                       (token_sha256, user_id, expires_at, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        row["id"],
                        (now + timedelta(hours=_SESSION_HOURS)).isoformat(),
                        now.isoformat(),
                    ),
                )
                self._append_security_event(
                    connection,
                    action="login_success",
                    actor_user_id=str(row["id"]),
                    target_user_id=str(row["id"]),
                )
        if not valid_password:
            raise AuthenticationError("invalid credentials")
        return token, self.csrf_for(token)

    def csrf_for(self, token: str) -> str:
        if self.session(token) is None:
            raise AuthenticationError("invalid session")
        return hmac.new(token.encode(), b"project-watch:csrf:v1", hashlib.sha256).hexdigest()

    def session(self, token: str, *, now: datetime | None = None) -> TeamSession | None:
        if not token or len(token) > 256:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        at = now or datetime.now(UTC)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT s.*, u.username, u.global_admin, u.active
                   FROM team_sessions s JOIN team_users u ON u.id = s.user_id
                   WHERE s.token_sha256 = ?""",
                (token_hash,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None or not row["active"]:
                return None
            expires = datetime.fromisoformat(str(row["expires_at"]))
            if at >= expires:
                return None
            return TeamSession(
                user_id=str(row["user_id"]),
                username=str(row["username"]),
                global_admin=bool(row["global_admin"]),
                expires_at=expires,
            )

    def validate_csrf(self, token: str, csrf: str) -> bool:
        if self.session(token) is None or not csrf or len(csrf) > 256:
            return False
        return hmac.compare_digest(
            hmac.new(token.encode(), b"project-watch:csrf:v1", hashlib.sha256).hexdigest(),
            csrf,
        )

    def logout(self, token: str) -> None:
        if not token or len(token) > 256:
            return
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            row = connection.execute(
                """SELECT user_id FROM team_sessions
                   WHERE token_sha256 = ? AND revoked_at IS NULL""",
                (token_hash,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE team_sessions SET revoked_at = ? WHERE token_sha256 = ?",
                (datetime.now(UTC).isoformat(), token_hash),
            )
            self._append_security_event(
                connection,
                action="logout",
                actor_user_id=str(row["user_id"]),
                target_user_id=str(row["user_id"]),
            )

    def grant_project_role(
        self,
        project_id: str,
        user_id: str,
        role: ProjectRole,
        *,
        actor_user_id: str | None = None,
    ) -> None:
        if role not in _ALLOWED:
            raise ValueError("invalid project role")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
                is None
            ):
                raise ValueError("project does not exist")
            user = connection.execute(
                "SELECT active FROM team_users WHERE id = ?", (user_id,)
            ).fetchone()
            if user is None or not user["active"]:
                raise ValueError("user does not exist or is inactive")
            prior = connection.execute(
                "SELECT role FROM team_memberships WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO team_memberships (project_id, user_id, role)
                   VALUES (?, ?, ?) ON CONFLICT (project_id, user_id)
                   DO UPDATE SET role = excluded.role""",
                (project_id, user_id, role),
            )
            if prior is None or prior["role"] != role:
                self._append_security_event(
                    connection,
                    action="grant_project_role",
                    actor_user_id=actor_user_id,
                    target_user_id=user_id,
                    project_id=project_id,
                    previous_role=str(prior["role"]) if prior is not None else None,
                    new_role=role,
                )

    def revoke_project_role(
        self, project_id: str, user_id: str, *, actor_user_id: str | None = None
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT role FROM team_memberships WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            connection.execute(
                "DELETE FROM team_memberships WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            )
            if prior is not None:
                self._append_security_event(
                    connection,
                    action="revoke_project_role",
                    actor_user_id=actor_user_id,
                    target_user_id=user_id,
                    project_id=project_id,
                    previous_role=str(prior["role"]),
                )

    def require(self, user_id: str, project_id: str, permission: Permission) -> None:
        if permission not in {"read", "propose", "review", "manage"}:
            raise ValueError("invalid project permission")
        with closing(self._connect()) as connection:
            user = connection.execute(
                "SELECT active, global_admin FROM team_users WHERE id = ?", (user_id,)
            ).fetchone()
            if user is None or not user["active"]:
                raise AuthorizationError("project not available")
            if user["global_admin"]:
                return
            membership = connection.execute(
                """SELECT role FROM team_memberships WHERE user_id = ? AND project_id = ?""",
                (user_id, project_id),
            ).fetchone()
            if membership is None or permission not in _ALLOWED[membership["role"]]:
                raise AuthorizationError("project not available")

    def revoke_user_sessions(self, user_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE team_sessions SET revoked_at = ?
                   WHERE user_id = ? AND revoked_at IS NULL""",
                (datetime.now(UTC).isoformat(), user_id),
            )

    def reset_password(self, user_id: str, password: str, *, actor_user_id: str) -> None:
        password_hash = self.hasher.hash(self._password(password))
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            actor = connection.execute(
                "SELECT active, global_admin FROM team_users WHERE id = ?", (actor_user_id,)
            ).fetchone()
            if actor is None or not actor["active"] or not actor["global_admin"]:
                raise AuthorizationError("administrator required")
            target = connection.execute(
                "SELECT active FROM team_users WHERE id = ?", (user_id,)
            ).fetchone()
            if target is None or not target["active"]:
                raise ValueError("user does not exist or is inactive")
            self._replace_password(connection, user_id, password_hash, actor_user_id)

    def recover_admin_offline(self, username: str, password: str) -> TeamUser:
        """Privileged offline recovery; caller must stop services and verify a backup."""
        username = self._username(username)
        password_hash = self.hasher.hash(self._password(password))
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM team_users WHERE username = ?", (username,)
            ).fetchone()
            if row is None or not row["active"] or not row["global_admin"]:
                raise ValueError("active administrator not found")
            user_id = str(row["id"])
            self._replace_password(connection, user_id, password_hash, None, offline=True)
            return self._user(row)

    def _replace_password(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        password_hash: str,
        actor_user_id: str | None,
        *,
        offline: bool = False,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """UPDATE team_users SET password_hash = ?, failed_attempts = 0,
               locked_until = NULL WHERE id = ?""",
            (password_hash, user_id),
        )
        connection.execute(
            "UPDATE team_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now, user_id),
        )
        connection.execute(
            """UPDATE team_executor_tokens SET revoked_at = ?
               WHERE issued_by = ? AND revoked_at IS NULL""",
            (now, user_id),
        )
        self._append_security_event(
            connection,
            action="recover_admin_offline" if offline else "reset_password",
            actor_user_id=actor_user_id,
            target_user_id=user_id,
        )

    def deactivate_user(self, user_id: str, *, actor_user_id: str | None = None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE team_users SET active = 0 WHERE id = ? AND active = 1", (user_id,)
            ).rowcount
            if changed:
                connection.execute(
                    """UPDATE team_sessions SET revoked_at = ?
                       WHERE user_id = ? AND revoked_at IS NULL""",
                    (datetime.now(UTC).isoformat(), user_id),
                )
                self._append_security_event(
                    connection,
                    action="deactivate_user",
                    actor_user_id=actor_user_id,
                    target_user_id=user_id,
                )

    def unlock_user(self, user_id: str, *, actor_user_id: str | None = None) -> None:
        with closing(self._connect()) as connection, connection:
            changed = connection.execute(
                """UPDATE team_users SET failed_attempts = 0, locked_until = NULL
                   WHERE id = ? AND (failed_attempts > 0 OR locked_until IS NOT NULL)""",
                (user_id,),
            ).rowcount
            if changed:
                self._append_security_event(
                    connection,
                    action="unlock_user",
                    actor_user_id=actor_user_id,
                    target_user_id=user_id,
                )

    def list_security_events(self, *, limit: int = 100) -> list[TeamSecurityEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("security audit limit must be between 1 and 500")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM team_security_events
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            TeamSecurityEvent(
                id=str(row["id"]),
                actor_user_id=row["actor_user_id"],
                action=str(row["action"]),
                target_user_id=row["target_user_id"],
                project_id=row["project_id"],
                subject_id=row["subject_id"],
                previous_role=row["previous_role"],
                new_role=row["new_role"],
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        ]

    @staticmethod
    def _manager_on_connection(
        connection: sqlite3.Connection, project_id: str, actor_user_id: str
    ) -> bool:
        row = connection.execute(
            """SELECT u.active, u.global_admin, m.role
               FROM team_users u
               LEFT JOIN team_memberships m
                 ON m.user_id = u.id AND m.project_id = ?
               WHERE u.id = ?""",
            (project_id, actor_user_id),
        ).fetchone()
        return bool(row and row["active"] and (row["global_admin"] or row["role"] == "admin"))

    @staticmethod
    def _executor_grant(row: sqlite3.Row) -> ExecutorGrant:
        return ExecutorGrant(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            issued_by=str(row["issued_by"]),
            label=str(row["label"]),
            permission=row["permission"],
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            revoked_at=(
                datetime.fromisoformat(str(row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
        )

    def issue_executor_token(
        self,
        project_id: str,
        actor_user_id: str,
        *,
        label: str,
        permission: ExecutorPermission,
        duration_minutes: int = 60,
    ) -> tuple[str, ExecutorGrant]:
        label = label.strip()
        if not 1 <= len(label) <= 80:
            raise ValueError("executor label must contain 1-80 characters")
        if permission not in {"read", "propose"}:
            raise ValueError("executor permission must be read or propose")
        if not 5 <= duration_minutes <= 480:
            raise ValueError("executor lifetime must be between 5 and 480 minutes")
        now = datetime.now(UTC)
        token = "pwe_" + secrets.token_urlsafe(32)
        token_id = str(uuid.uuid4())
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            ).fetchone() is None or not self._manager_on_connection(
                connection, project_id, actor_user_id
            ):
                raise AuthorizationError("project not available")
            connection.execute(
                """INSERT INTO team_executor_tokens
                   (id, token_sha256, project_id, issued_by, label, permission,
                    expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    token_id,
                    hashlib.sha256(token.encode()).hexdigest(),
                    project_id,
                    actor_user_id,
                    label,
                    permission,
                    (now + timedelta(minutes=duration_minutes)).isoformat(),
                    now.isoformat(),
                ),
            )
            self._append_security_event(
                connection,
                action="issue_executor_token",
                actor_user_id=actor_user_id,
                project_id=project_id,
                subject_id=token_id,
            )
        grant = ExecutorGrant(
            id=token_id,
            project_id=project_id,
            issued_by=actor_user_id,
            label=label,
            permission=permission,
            expires_at=now + timedelta(minutes=duration_minutes),
            revoked_at=None,
        )
        return token, grant

    def executor_grant(
        self, token: str, *, permission: ExecutorPermission = "read", now: datetime | None = None
    ) -> ExecutorGrant | None:
        if (
            permission not in {"read", "propose"}
            or not token.startswith("pwe_")
            or len(token) > 256
        ):
            return None
        at = now or datetime.now(UTC)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT t.*, u.active, u.global_admin, m.role
                   FROM team_executor_tokens t
                   JOIN team_users u ON u.id = t.issued_by
                   LEFT JOIN team_memberships m
                     ON m.user_id = u.id AND m.project_id = t.project_id
                   WHERE t.token_sha256 = ?""",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or not row["active"]
            or not (row["global_admin"] or row["role"] == "admin")
            or at >= datetime.fromisoformat(str(row["expires_at"]))
            or (permission == "propose" and row["permission"] != "propose")
        ):
            return None
        return self._executor_grant(row)

    def list_executor_tokens(self, project_id: str) -> list[ExecutorGrant]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM team_executor_tokens
                   WHERE project_id = ? ORDER BY created_at DESC, id DESC""",
                (project_id,),
            ).fetchall()
        return [self._executor_grant(row) for row in rows]

    def revoke_executor_token(self, project_id: str, token_id: str, *, actor_user_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._manager_on_connection(connection, project_id, actor_user_id):
                raise AuthorizationError("project not available")
            changed = connection.execute(
                """UPDATE team_executor_tokens SET revoked_at = ?
                   WHERE id = ? AND project_id = ? AND revoked_at IS NULL""",
                (datetime.now(UTC).isoformat(), token_id, project_id),
            ).rowcount
            if changed:
                self._append_security_event(
                    connection,
                    action="revoke_executor_token",
                    actor_user_id=actor_user_id,
                    project_id=project_id,
                    subject_id=token_id,
                )
            return bool(changed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Project Watch team identity setup")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.add_argument("--db", type=Path, required=True)
    bootstrap.add_argument("--username", required=True)
    recover = commands.add_parser("recover-admin-offline")
    recover.add_argument("--db", type=Path, required=True)
    recover.add_argument("--username", required=True)
    recover.add_argument("--confirm-services-stopped-and-backup-verified", action="store_true")
    args = parser.parse_args()
    if args.command == "bootstrap-admin":
        from project_watch.repository import ProjectRepository

        ProjectRepository(args.db)
        password = getpass.getpass("New administrator password (12-128 characters): ")
        confirmation = getpass.getpass("Repeat password: ")
        if password != confirmation:
            raise SystemExit("Passwords did not match")
        user = TeamAuthStore(args.db).bootstrap_admin(args.username, password)
        print(f"Created administrator: {user.username} ({user.id})")
    elif args.command == "recover-admin-offline":
        if not args.confirm_services_stopped_and_backup_verified:
            parser.error("stop services, verify a backup, then provide the confirmation flag")
        if not args.db.is_file():
            parser.error("identity database does not exist")
        password = getpass.getpass("New administrator password (12-128 characters): ")
        confirmation = getpass.getpass("Repeat password: ")
        if password != confirmation:
            raise SystemExit("Passwords did not match")
        user = TeamAuthStore(args.db).recover_admin_offline(args.username, password)
        print(f"Recovered administrator: {user.username} ({user.id}); sessions and tokens revoked")


if __name__ == "__main__":
    main()
