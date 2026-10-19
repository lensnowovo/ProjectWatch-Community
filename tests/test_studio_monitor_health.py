from test_studio_agent_instance import make_service


def test_no_updates_is_not_failure_or_business_silence(tmp_path):
    from project_watch.studio.health import HealthStore

    service, pid = make_service(tmp_path / "health.sqlite3")
    health = HealthStore(service.store)
    health.record(pid, {"company": {"checked": 2, "added": 0, "errors": []}})
    result = health.read(pid)
    assert result["sources"][0]["status"] == "available"
    assert result["sources"][0]["data_scope"] == "metadata"
    assert result["business_silence"] == "not_evaluated"


def test_partial_success_and_source_failure_do_not_claim_full_coverage(tmp_path):
    from project_watch.studio.health import HealthStore

    service, pid = make_service(tmp_path / "health.sqlite3")
    health = HealthStore(service.store)
    health.record(pid, {"company": {"checked": 1, "added": 0, "errors": ["private query and key"]},
                        "paper": {"checked": 0, "added": 0, "errors": ["timeout"]}})
    result = health.read(pid)
    assert result["coverage"] == "partial"
    assert {r["status"] for r in result["sources"]} == {"partial", "unavailable"}
    assert "private query" not in str(result)
    assert service.evidence.dependencies.changes(pid)["items"][-1]["kind"] == "source_health_checked"


def test_paused_and_no_observations_are_distinct_and_reads_do_not_mutate(tmp_path):
    from project_watch.studio.health import HealthStore

    service, pid = make_service(tmp_path / "health.sqlite3")
    health = HealthStore(service.store)
    before = service.evidence.dependencies.changes(pid)["snapshot_through"]
    assert health.read(pid)["coverage"] == "not_observed"
    service.store.set_status(pid, "paused")
    assert health.read(pid)["monitoring"] == "paused"
    assert service.evidence.dependencies.changes(pid)["snapshot_through"] == before


def test_health_survives_restart_without_reclassifying_failure_as_withdrawal(tmp_path):
    from project_watch.studio.health import HealthStore
    from project_watch.studio.store import StudioStore

    service, pid = make_service(tmp_path / "health.sqlite3")
    HealthStore(service.store).record(pid, {"company": {"checked": 0, "added": 0, "errors": ["failure"]}})
    result = HealthStore(StudioStore(service.store.db_path)).read(pid)
    assert result["sources"][0]["status"] == "unavailable"
    assert not any(r["kind"] == "evidence_withdrawn" for r in service.evidence.dependencies.changes(pid)["items"])


def test_scoped_health_api_is_read_only_and_hides_internal_job_states(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    pipeline, service = app.state.studio_pipeline, app.state.studio_application
    project = pipeline.repository.create_project("Synthetic Health")
    service.store.upsert_project(project.id, {}, consent=False)
    private_job = service.store.enqueue(project.id, "internal")
    grant = service.issue_grant(project.id, "read", "health")
    headers = {"authorization": "Bearer " + grant["token"], "x-projectwatch-instance": service.instance_id}
    client = TestClient(app)
    path = f"/api/studio/agent/projects/{project.id}/health"
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and response.json()["jobs"] == []
    assert service.store.jobs(project.id)[0]["status"] == "queued"
    assert service.store.jobs(project.id)[0]["id"] == private_job["id"]
    assert client.get(path, headers={**headers, "x-projectwatch-instance": "wrong"}).status_code == 403
    assert client.get("/api/studio/agent/projects/other/health", headers=headers).status_code == 403
