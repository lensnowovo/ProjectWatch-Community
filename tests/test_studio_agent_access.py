import pytest
from test_studio_agent_instance import make_service

from project_watch.studio.application import AccessDenied


def test_project_scope_permission_revocation_and_secret_hash(tmp_path):
    service, project_id = make_service(tmp_path / "access.sqlite3")
    read = service.issue_grant(project_id, "read", "reader")
    assert service.authorize(read["token"], project_id, "read")["permission"] == "read"
    with pytest.raises(AccessDenied):
        service.authorize(read["token"], project_id, "write")
    with pytest.raises(AccessDenied):
        service.authorize(read["token"], "other-project", "read")
    from contextlib import closing

    with closing(service.store._connect()) as db:
        stored = dict(db.execute("SELECT * FROM studio_agent_grants").fetchone())
    assert read["token"] not in str(stored)
    assert read["token"] not in str(service.list_grants(project_id))
    service.revoke_grant(project_id, read["id"])
    with pytest.raises(AccessDenied):
        service.authorize(read["token"], project_id, "read")


def test_expired_grant_is_rejected(tmp_path):
    service, project_id = make_service(tmp_path / "expiry.sqlite3")
    grant = service.issue_grant(project_id, "write", "writer")
    from contextlib import closing

    with closing(service.store._connect()) as db, db:
        db.execute("UPDATE studio_agent_grants SET expires_at=0")
    with pytest.raises(AccessDenied):
        service.authorize(grant["token"], project_id, "write")
