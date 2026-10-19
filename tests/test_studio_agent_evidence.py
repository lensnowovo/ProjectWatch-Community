from fastapi.testclient import TestClient
from test_studio_application_parity import headers, setup_app
from test_studio_evidence_versions import sample

from project_watch.studio.monitoring_contracts import LocatedRef


def test_agent_evidence_default_public_scope_and_explicit_internal(tmp_path):
    app, _, pid, _ = setup_app(tmp_path)
    service = app.state.studio_application
    private = service.evidence.save(sample(pid, "private text"))
    public = service.evidence.save(sample(pid, "public text").model_copy(update={"visibility": "public"}))
    ordinary = service.issue_grant(pid, "read", "public")
    internal = service.issue_grant(pid, "read", "private", allow_internal=True)
    with TestClient(app) as http:
        root = f"/api/studio/agent/projects/{pid}/evidence"
        page = http.get(root, headers=headers(ordinary)).json()
        assert [r["id"] for r in page["items"]] == [public["id"]]
        assert page["instance_id"] == service.instance_id
        assert http.get(root + f"/{private['id']}/parts/0", headers=headers(ordinary)).status_code == 404
        response = http.get(root + f"/{private['id']}/parts/0", headers=headers(internal))
        assert response.json()["part"]["text"] == "private text"
        ref = LocatedRef(evidence_version_id=public["id"], part_index=0, start=0, end=6, quote="public")
        assert http.post(root + "/resolve", json=ref.model_dump(), headers=headers(ordinary)).json()["quote"] == "public"
        forged = {**ref.model_dump(), "quote": "invent"}
        assert http.post(root + "/resolve", json=forged, headers=headers(ordinary)).status_code == 422
        service.revoke_grant(pid, internal["id"])
        assert http.get(root, headers=headers(internal)).status_code == 403
        assert not service.store.jobs(pid)
