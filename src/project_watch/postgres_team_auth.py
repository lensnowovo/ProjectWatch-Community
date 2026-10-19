"""Self-hosted team identity store for the isolated PostgreSQL schema.

The dedicated team HTTP factory verifies sessions before passing user identity
to this store. Caller-supplied actor IDs are not proof of identity.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from project_watch.postgres_pool import open_project_watch_pool
from project_watch.team_auth import (
    _ALLOWED,
    _LOCK_MINUTES,
    _SESSION_HOURS,
    AuthenticationError,
    AuthorizationError,
    ExecutorGrant,
    ExecutorPermission,
    Permission,
    ProjectMember,
    ProjectRole,
    TeamAuthStore,
    TeamSecurityEvent,
    TeamSession,
    TeamUser,
)

_BOOTSTRAP_LOCK = 604596779367113460


class PostgresTeamAuthStore:
    def __init__(self, dsn: str, *, pool_size: int = 8) -> None:
        self._pool = open_project_watch_pool(dsn, pool_size=pool_size)
        self.hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
        self._dummy_hash = self.hasher.hash("not-a-real-user-password")

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> PostgresTeamAuthStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def _security_event(
        connection,
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
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
    def _admin_on_connection(connection, actor_user_id: str, *, lock: bool = False) -> None:
        suffix = " FOR UPDATE" if lock else ""
        actor = connection.execute(
            "SELECT active, global_admin FROM team_users WHERE id = %s" + suffix,
            (actor_user_id,),
        ).fetchone()
        if actor is None or not actor["active"] or not actor["global_admin"]:
            raise AuthorizationError("administrator required")

    @staticmethod
    def _manager_on_connection(
        connection, project_id: str, actor_user_id: str, *, lock: bool = False
    ) -> None:
        suffix = " FOR UPDATE" if lock else ""
        actor = connection.execute(
            "SELECT active, global_admin FROM team_users WHERE id = %s" + suffix,
            (actor_user_id,),
        ).fetchone()
        if actor is None or not actor["active"]:
            raise AuthorizationError("project not available")
        if actor["global_admin"]:
            return
        membership = connection.execute(
            """SELECT role FROM team_memberships
               WHERE project_id = %s AND user_id = %s"""
            + suffix,
            (project_id, actor_user_id),
        ).fetchone()
        if membership is None or membership["role"] != "admin":
            raise AuthorizationError("project not available")

    def bootstrap_admin(self, username: str, password: str) -> TeamUser:
        username = TeamAuthStore._username(username)
        password_hash = self.hasher.hash(TeamAuthStore._password(password))
        with self._pool.connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_BOOTSTRAP_LOCK,))
            if connection.execute("SELECT 1 FROM team_users LIMIT 1").fetchone():
                raise ValueError("team identities already bootstrapped")
            user_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO team_users
                   (id, username, password_hash, global_admin, created_at)
                   VALUES (%s, %s, %s, 1, %s)""",
                (user_id, username, password_hash, datetime.now(UTC).isoformat()),
            )
            self._security_event(connection, action="bootstrap_admin", target_user_id=user_id)
            return TeamUser(user_id, username, True, True)

    def is_bootstrapped(self) -> bool:
        with self._pool.connection() as connection:
            return connection.execute("SELECT 1 FROM team_users LIMIT 1").fetchone() is not None

    def create_user(
        self, username: str, password: str, *, actor_user_id: str | None = None
    ) -> TeamUser:
        username = TeamAuthStore._username(username)
        password_hash = self.hasher.hash(TeamAuthStore._password(password))
        with self._pool.connection() as connection:
            self._admin_on_connection(connection, actor_user_id or "", lock=True)
            user_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO team_users
                   (id, username, password_hash, created_at) VALUES (%s, %s, %s, %s)""",
                (user_id, username, password_hash, datetime.now(UTC).isoformat()),
            )
            self._security_event(
                connection,
                action="create_user",
                actor_user_id=actor_user_id,
                target_user_id=user_id,
            )
            return TeamUser(user_id, username, False, True)

    def get_user(self, user_id: str) -> TeamUser | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM team_users WHERE id = %s", (user_id,)
            ).fetchone()
            return TeamAuthStore._user(row) if row else None

    def find_active_user(self, username: str) -> TeamUser | None:
        username = TeamAuthStore._username(username)
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM team_users WHERE username = %s AND active = 1", (username,)
            ).fetchone()
            return TeamAuthStore._user(row) if row else None

    def list_project_members(
        self, project_id: str, *, limit: int = 100, after_username: str | None = None
    ) -> list[ProjectMember]:
        if not 1 <= limit <= 100:
            raise ValueError("member page limit must be between 1 and 100")
        cursor = TeamAuthStore._username(after_username) if after_username is not None else ""
        with self._pool.connection() as connection:
            rows = connection.execute(
                """SELECT u.id, u.username, u.active, m.role
                   FROM team_memberships m JOIN team_users u ON u.id = m.user_id
                   WHERE m.project_id = %s AND u.username > %s
                   ORDER BY u.username LIMIT %s""",
                (project_id, cursor, limit),
            ).fetchall()
        return [
            ProjectMember(str(row["id"]), str(row["username"]), row["role"], bool(row["active"]))
            for row in rows
        ]

    def login(self, username: str, password: str) -> tuple[str, str]:
        try:
            username = TeamAuthStore._username(username)
        except ValueError as exc:
            raise AuthenticationError("invalid credentials") from exc
        now = datetime.now(UTC)
        valid_password = False
        token = ""
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM team_users WHERE username = %s FOR UPDATE", (username,)
            ).fetchone()
            stored_hash = str(row["password_hash"]) if row is not None else self._dummy_hash
            try:
                valid_password = self.hasher.verify(stored_hash, password)
            except (VerifyMismatchError, VerificationError):
                valid_password = False
            if row is None or not row["active"]:
                raise AuthenticationError("invalid credentials")
            locked_until = (
                datetime.fromisoformat(row["locked_until"])
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
                    """UPDATE team_users SET failed_attempts = %s, locked_until = %s
                       WHERE id = %s""",
                    (failures, next_lock, row["id"]),
                )
                self._security_event(
                    connection, action="login_failed", target_user_id=str(row["id"])
                )
                if next_lock is not None:
                    self._security_event(
                        connection, action="account_locked", target_user_id=str(row["id"])
                    )
            else:
                connection.execute(
                    """UPDATE team_users SET failed_attempts = 0, locked_until = NULL
                       WHERE id = %s""",
                    (row["id"],),
                )
                token = secrets.token_urlsafe(32)
                connection.execute(
                    """INSERT INTO team_sessions
                       (token_sha256, user_id, expires_at, created_at)
                       VALUES (%s, %s, %s, %s)""",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        row["id"],
                        (now + timedelta(hours=_SESSION_HOURS)).isoformat(),
                        now.isoformat(),
                    ),
                )
                self._security_event(
                    connection,
                    action="login_success",
                    actor_user_id=str(row["id"]),
                    target_user_id=str(row["id"]),
                )
        # Failed-attempt updates must commit before raising.
        if not valid_password:
            raise AuthenticationError("invalid credentials")
        return token, self.csrf_for(token)

    def session(self, token: str, *, now: datetime | None = None) -> TeamSession | None:
        if not token or len(token) > 256:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        at = now or datetime.now(UTC)
        with self._pool.connection() as connection:
            row = connection.execute(
                """SELECT s.*, u.username, u.global_admin, u.active
                   FROM team_sessions s JOIN team_users u ON u.id = s.user_id
                   WHERE s.token_sha256 = %s""",
                (token_hash,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None or not row["active"]:
                return None
            expires = datetime.fromisoformat(row["expires_at"])
            if at >= expires:
                return None
            return TeamSession(
                user_id=str(row["user_id"]),
                username=str(row["username"]),
                global_admin=bool(row["global_admin"]),
                expires_at=expires,
            )

    def csrf_for(self, token: str) -> str:
        if self.session(token) is None:
            raise AuthenticationError("invalid session")
        return hmac.new(token.encode(), b"project-watch:csrf:v1", hashlib.sha256).hexdigest()

    def validate_csrf(self, token: str, csrf: str) -> bool:
        if self.session(token) is None or not csrf or len(csrf) > 256:
            return False
        return hmac.compare_digest(
            hmac.new(token.encode(), b"project-watch:csrf:v1", hashlib.sha256).hexdigest(), csrf
        )

    def logout(self, token: str) -> None:
        if not token or len(token) > 256:
            return
        with self._pool.connection() as connection:
            row = connection.execute(
                """UPDATE team_sessions SET revoked_at = %s
                   WHERE token_sha256 = %s AND revoked_at IS NULL
                   RETURNING user_id""",
                (datetime.now(UTC).isoformat(), hashlib.sha256(token.encode()).hexdigest()),
            ).fetchone()
            if row is not None:
                self._security_event(
                    connection,
                    action="logout",
                    actor_user_id=str(row["user_id"]),
                    target_user_id=str(row["user_id"]),
                )

    def require(self, user_id: str, project_id: str, permission: Permission) -> None:
        if permission not in {"read", "propose", "review", "manage"}:
            raise ValueError("invalid project permission")
        with self._pool.connection() as connection:
            user = connection.execute(
                "SELECT active, global_admin FROM team_users WHERE id = %s", (user_id,)
            ).fetchone()
            if user is None or not user["active"]:
                raise AuthorizationError("project not available")
            if user["global_admin"]:
                return
            membership = connection.execute(
                """SELECT role FROM team_memberships WHERE user_id = %s
                   AND project_id = %s""",
                (user_id, project_id),
            ).fetchone()
            if membership is None or permission not in _ALLOWED[membership["role"]]:
                raise AuthorizationError("project not available")

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
        with self._pool.connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE id = %s FOR UPDATE", (project_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("project does not exist")
            self._manager_on_connection(connection, project_id, actor_user_id or "", lock=True)
            user = connection.execute(
                "SELECT active FROM team_users WHERE id = %s FOR UPDATE", (user_id,)
            ).fetchone()
            if user is None or not user["active"]:
                raise ValueError("user does not exist or is inactive")
            prior = connection.execute(
                """SELECT role FROM team_memberships
                   WHERE project_id = %s AND user_id = %s FOR UPDATE""",
                (project_id, user_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO team_memberships (project_id, user_id, role)
                   VALUES (%s, %s, %s) ON CONFLICT (project_id, user_id)
                   DO UPDATE SET role = excluded.role""",
                (project_id, user_id, role),
            )
            if prior is None or prior["role"] != role:
                self._security_event(
                    connection,
                    action="grant_project_role",
                    actor_user_id=actor_user_id,
                    target_user_id=user_id,
                    project_id=project_id,
                    previous_role=str(prior["role"]) if prior else None,
                    new_role=role,
                )

    def revoke_project_role(
        self, project_id: str, user_id: str, *, actor_user_id: str | None = None
    ) -> None:
        with self._pool.connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE id = %s FOR UPDATE", (project_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("project does not exist")
            self._manager_on_connection(connection, project_id, actor_user_id or "", lock=True)
            prior = connection.execute(
                """DELETE FROM team_memberships WHERE project_id = %s AND user_id = %s
                   RETURNING role""",
                (project_id, user_id),
            ).fetchone()
            if prior is not None:
                self._security_event(
                    connection,
                    action="revoke_project_role",
                    actor_user_id=actor_user_id,
                    target_user_id=user_id,
                    project_id=project_id,
                    previous_role=str(prior["role"]),
                )

    def revoke_user_sessions(self, user_id: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """UPDATE team_sessions SET revoked_at = %s
                   WHERE user_id = %s AND revoked_at IS NULL""",
                (datetime.now(UTC).isoformat(), user_id),
            )

    def reset_password(self, user_id: str, password: str, *, actor_user_id: str) -> None:
        password_hash = self.hasher.hash(TeamAuthStore._password(password))
        with self._pool.connection() as connection:
            self._admin_on_connection(connection, actor_user_id, lock=True)
            target = connection.execute(
                "SELECT active FROM team_users WHERE id = %s FOR UPDATE", (user_id,)
            ).fetchone()
            if target is None or not target["active"]:
                raise ValueError("user does not exist or is inactive")
            self._replace_password(connection, user_id, password_hash, actor_user_id)

    def recover_admin_offline(self, username: str, password: str) -> TeamUser:
        """Privileged offline recovery; caller must stop services and verify a backup."""
        username = TeamAuthStore._username(username)
        password_hash = self.hasher.hash(TeamAuthStore._password(password))
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM team_users WHERE username = %s FOR UPDATE", (username,)
            ).fetchone()
            if row is None or not row["active"] or not row["global_admin"]:
                raise ValueError("active administrator not found")
            user_id = str(row["id"])
            self._replace_password(connection, user_id, password_hash, None, offline=True)
            return TeamAuthStore._user(row)

    def _replace_password(
        self,
        connection,
        user_id: str,
        password_hash: str,
        actor_user_id: str | None,
        *,
        offline: bool = False,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """UPDATE team_users SET password_hash = %s, failed_attempts = 0,
               locked_until = NULL WHERE id = %s""",
            (password_hash, user_id),
        )
        connection.execute(
            "UPDATE team_sessions SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL",
            (now, user_id),
        )
        connection.execute(
            """UPDATE team_executor_tokens SET revoked_at = %s
               WHERE issued_by = %s AND revoked_at IS NULL""",
            (now, user_id),
        )
        self._security_event(
            connection,
            action="recover_admin_offline" if offline else "reset_password",
            actor_user_id=actor_user_id,
            target_user_id=user_id,
        )

    def deactivate_user(self, user_id: str, *, actor_user_id: str | None = None) -> None:
        with self._pool.connection() as connection:
            self._admin_on_connection(connection, actor_user_id or "", lock=True)
            target = connection.execute(
                "SELECT global_admin FROM team_users WHERE id = %s FOR UPDATE", (user_id,)
            ).fetchone()
            if target is not None and target["global_admin"]:
                raise ValueError("the bootstrap administrator cannot be deactivated")
            changed = connection.execute(
                "UPDATE team_users SET active = 0 WHERE id = %s AND active = 1 RETURNING id",
                (user_id,),
            ).fetchone()
            if changed is not None:
                connection.execute(
                    """UPDATE team_sessions SET revoked_at = %s
                       WHERE user_id = %s AND revoked_at IS NULL""",
                    (datetime.now(UTC).isoformat(), user_id),
                )
                self._security_event(
                    connection,
                    action="deactivate_user",
                    actor_user_id=actor_user_id,
                    target_user_id=user_id,
                )

    def unlock_user(self, user_id: str, *, actor_user_id: str | None = None) -> None:
        with self._pool.connection() as connection:
            self._admin_on_connection(connection, actor_user_id or "", lock=True)
            changed = connection.execute(
                """UPDATE team_users SET failed_attempts = 0, locked_until = NULL
                   WHERE id = %s AND (failed_attempts > 0 OR locked_until IS NOT NULL)
                   RETURNING id""",
                (user_id,),
            ).fetchone()
            if changed is not None:
                self._security_event(
                    connection,
                    action="unlock_user",
                    actor_user_id=actor_user_id,
                    target_user_id=user_id,
                )

    def list_security_events(self, *, limit: int = 100) -> list[TeamSecurityEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("security audit limit must be between 1 and 500")
        with self._pool.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM team_security_events
                   ORDER BY created_at DESC, id DESC LIMIT %s""",
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
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            ]

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
        with self._pool.connection() as connection:
            self._manager_on_connection(connection, project_id, actor_user_id, lock=True)
            if (
                connection.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,)).fetchone()
                is None
            ):
                raise AuthorizationError("project not available")
            connection.execute(
                """INSERT INTO team_executor_tokens
                   (id, token_sha256, project_id, issued_by, label, permission,
                    expires_at, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
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
            self._security_event(
                connection,
                action="issue_executor_token",
                actor_user_id=actor_user_id,
                project_id=project_id,
                subject_id=token_id,
            )
        return token, ExecutorGrant(
            id=token_id,
            project_id=project_id,
            issued_by=actor_user_id,
            label=label,
            permission=permission,
            expires_at=now + timedelta(minutes=duration_minutes),
            revoked_at=None,
        )

    def executor_grant(
        self,
        token: str,
        *,
        permission: ExecutorPermission = "read",
        now: datetime | None = None,
    ) -> ExecutorGrant | None:
        if (
            permission not in {"read", "propose"}
            or not token.startswith("pwe_")
            or len(token) > 256
        ):
            return None
        at = now or datetime.now(UTC)
        with self._pool.connection() as connection:
            row = connection.execute(
                """SELECT t.*, u.active, u.global_admin, m.role
                   FROM team_executor_tokens t
                   JOIN team_users u ON u.id = t.issued_by
                   LEFT JOIN team_memberships m
                     ON m.user_id = u.id AND m.project_id = t.project_id
                   WHERE t.token_sha256 = %s""",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or not row["active"]
                or not (row["global_admin"] or row["role"] == "admin")
                or at >= datetime.fromisoformat(row["expires_at"])
                or (permission == "propose" and row["permission"] != "propose")
            ):
                return None
            return TeamAuthStore._executor_grant(row)

    def list_executor_tokens(self, project_id: str) -> list[ExecutorGrant]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM team_executor_tokens
                   WHERE project_id = %s ORDER BY created_at DESC, id DESC""",
                (project_id,),
            ).fetchall()
            return [TeamAuthStore._executor_grant(row) for row in rows]

    def revoke_executor_token(self, project_id: str, token_id: str, *, actor_user_id: str) -> bool:
        with self._pool.connection() as connection:
            self._manager_on_connection(connection, project_id, actor_user_id, lock=True)
            changed = connection.execute(
                """UPDATE team_executor_tokens SET revoked_at = %s
                   WHERE id = %s AND project_id = %s AND revoked_at IS NULL RETURNING id""",
                (datetime.now(UTC).isoformat(), token_id, project_id),
            ).fetchone()
            if changed is not None:
                self._security_event(
                    connection,
                    action="revoke_executor_token",
                    actor_user_id=actor_user_id,
                    project_id=project_id,
                    subject_id=token_id,
                )
            return changed is not None


def main() -> None:
    parser = argparse.ArgumentParser(description="Project Watch PostgreSQL team identity setup")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.add_argument("--username", required=True)
    recover = commands.add_parser("recover-admin-offline")
    recover.add_argument("--username", required=True)
    recover.add_argument("--confirm-services-stopped-and-backup-verified", action="store_true")
    args = parser.parse_args()
    if (
        args.command == "recover-admin-offline"
        and not args.confirm_services_stopped_and_backup_verified
    ):
        parser.error("stop services, verify a backup, then provide the confirmation flag")
    dsn = os.environ.get("PROJECT_WATCH_DATABASE_URL")
    if not dsn:
        raise SystemExit("PROJECT_WATCH_DATABASE_URL is required")
    if args.command == "bootstrap-admin":
        password = getpass.getpass("New administrator password (12-128 characters): ")
        confirmation = getpass.getpass("Repeat password: ")
        if password != confirmation:
            raise SystemExit("Passwords did not match")
        with PostgresTeamAuthStore(dsn) as auth:
            user = auth.bootstrap_admin(args.username, password)
        print(f"Created administrator: {user.username} ({user.id})")
    elif args.command == "recover-admin-offline":
        password = getpass.getpass("New administrator password (12-128 characters): ")
        confirmation = getpass.getpass("Repeat password: ")
        if password != confirmation:
            raise SystemExit("Passwords did not match")
        with PostgresTeamAuthStore(dsn) as auth:
            user = auth.recover_admin_offline(args.username, password)
        print(f"Recovered administrator: {user.username} ({user.id}); sessions and tokens revoked")


if __name__ == "__main__":
    main()
