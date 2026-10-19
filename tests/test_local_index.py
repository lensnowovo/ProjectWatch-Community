from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.local_index import reindex_local_project


def test_reindex_preserves_sources_observations_and_originals(tmp_path):
    import sqlite3

    db = tmp_path / "pilot.sqlite3"
    app = create_app(db)
    client = TestClient(app)
    project = client.post("/api/projects", json={"name": "Synthetic"}).json()["id"]
    route = f"/api/projects/{project}"
    client.post(f"{route}/files", files={"file": ("资金预算.txt", "产品研发待核对。".encode(), "text/plain")})
    app.state.intake_worker.process_one()
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT rule_version FROM document_index").fetchone()[0] == 2
        connection.execute("UPDATE document_index SET topic='产品与技术', basis='content', rule_version=1")
    observations = client.get(f"{route}/observations").json()
    assert reindex_local_project(db, project) == 1
    assert client.get(f"{route}/sources").json()[0]["topic"] == "产品与技术"
    assert reindex_local_project(db, project, apply=True) == 1
    assert reindex_local_project(db, project, apply=True) == 0
    sources = client.get(f"{route}/sources").json()
    assert len(sources) == 1 and sources[0]["topic"] == "财务表现"
    assert client.get(f"{route}/observations").json() == observations
    assert app.state.intake_worker.blob_store.path_for_hash(sources[0]["content_sha256"]).exists()
