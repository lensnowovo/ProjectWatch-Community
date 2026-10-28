"""任务看板: job states, queue order and each uploaded file's steps. Synthetic stores only."""

from contextlib import closing

from fastapi.testclient import TestClient

from project_watch.studio import taskboard
from project_watch.studio.app import create_studio_app


def _doc(**kw):
    base = {"intake_status": "completed", "fragments": 12, "intake_updated_at": "2026-10-01T00:00:00",
            "analyzed_at": None, "permission": None, "error_code": None}
    return {**base, **kw}


def _states(doc, job=None, model_ready=True, binding="m"):
    return {s["key"]: (s["state"], s["note"]) for s in taskboard._document_steps(doc, job, model_ready, binding)}


def test_document_steps_follow_the_upload_through_parsing_and_absorption():
    assert _states(_doc(intake_status="processing", fragments=0))["parse"][0] == "running"
    assert _states(_doc(intake_status="processing", fragments=0))["absorb"] == ("waiting", "等识别完成")
    assert _states(_doc(intake_status="failed", fragments=0, error_code="corrupt"))["parse"][0] == "failed"
    assert _states(_doc(), model_ready=False)["absorb"] == ("blocked", "需先连接 AI 模型")
    assert _states(_doc(permission=""))["absorb"][0] == "blocked"  # not authorised for the current model
    assert _states(_doc(), {"status": "running", "stage": "提取关键指标", "created_at": "2026-10-02"})["absorb"] == ("running", "提取关键指标")
    assert _states(_doc(), {"status": "queued", "stage": "", "created_at": "2026-10-02"})["absorb"] == ("waiting", "已排队")
    failed = _states(_doc(), {"status": "failed", "error": "模型服务拒绝分析本批内容", "created_at": "2026-10-02"})
    assert failed["absorb"] == ("failed", "模型服务拒绝分析本批内容")
    done = _states(_doc(analyzed_at="2026-10-03T00:00:00"))
    assert done["absorb"][0] == "done" and done["profile"][0] == "done"
    # A re-parsed file after its last analysis is not reported as absorbed.
    assert _states(_doc(analyzed_at="2026-09-01T00:00:00"))["absorb"][0] != "done"


def test_board_orders_running_then_queue_and_hides_removed_projects(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    store = app.state.studio_store
    a = client.post("/api/studio/projects", json={"entity": {"legal_name": "看板甲"}, "consent": True}).json()
    b = client.post("/api/studio/projects", json={"entity": {"legal_name": "看板乙"}, "consent": True}).json()
    with closing(store._connect()) as db, db:
        # Remove only admission metadata in this synthetic queue-reset fixture.
        db.execute("DELETE FROM studio_job_checkpoints WHERE stage='collection_request_v1'")
        db.execute("DELETE FROM studio_jobs")
    first = store.enqueue(a["id"], "external")
    store.enqueue(a["id"], "brief")
    store.enqueue(b["id"], "external")
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='running', started_at=created_at, stage='检索公司新闻' WHERE id=?", (first["id"],))
    view = client.get("/api/studio/tasks").json()
    assert [j["status"] for j in view["jobs"]] == ["running", "queued", "queued"]
    assert view["jobs"][0]["label"] == "收集公开信息" and view["jobs"][0]["stage"] == "检索公司新闻"
    assert [j.get("position") for j in view["jobs"][1:]] == [1, 2]
    assert view["summary"]["running"] == 1 and view["summary"]["queued"] == 2
    client.delete(f"/api/studio/projects/{b['id']}")
    names = {j["project_name"] for j in client.get("/api/studio/tasks").json()["jobs"]}
    assert "看板乙" not in names
    only = client.get(f"/api/studio/tasks?project_id={a['id']}").json()
    assert {j["project_id"] for j in only["jobs"]} == {a["id"]}


def test_task_page_is_served_by_the_app(tmp_path):
    client = TestClient(create_studio_app(tmp_path, start_worker=False))
    response = client.get("/tasks")
    assert response.status_code in (200, 503)  # 503 only when the web bundle is absent in a bare checkout
    assert response.status_code != 404


def test_each_failure_says_what_happened_what_to_do_and_which_button():
    assert taskboard.advice({"error": "request_outcome_unknown", "unknown": 2})["action"] == "recover"
    assert "2 次请求" in taskboard.advice({"error": "request_outcome_unknown", "unknown": 2})["why"]
    assert taskboard.advice({"error": "interrupted: request outcome uncertain or recovery limit", "unknown": 0})["action"] == "recover"
    assert taskboard.advice({"error": "provider_backoff", "unknown": 0})["action"] == "settings"
    assert taskboard.advice({"error": "request_budget", "unknown": 0})["action"] == "rerun"
    refusal = taskboard.advice({"error": "模型服务拒绝分析本批内容；已保留资料和响应，未自动重发", "unknown": 0})
    assert refusal["action"] == "settings" and "换一个模型" in refusal["todo"]
    for advice in (refusal, taskboard.advice({"error": "内部错误：AttributeError", "unknown": 0})):
        assert advice["why"] and advice["todo"]


def test_dismissed_failures_leave_the_attention_list_but_stay_in_history(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    store = app.state.studio_store
    a = client.post("/api/studio/projects", json={"entity": {"legal_name": "看板丙"}, "consent": True}).json()
    with closing(store._connect()) as db, db:
        # Remove only admission metadata in this synthetic queue-reset fixture.
        db.execute("DELETE FROM studio_job_checkpoints WHERE stage='collection_request_v1'")
        db.execute("DELETE FROM studio_jobs")
    job = store.enqueue(a["id"], "external")
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='failed', error='request_budget', started_at=created_at, finished_at=created_at WHERE id=?",
                   (job["id"],))
    [row] = client.get("/api/studio/tasks").json()["jobs"]
    assert row["advice"]["action"] == "rerun" and row["dismissed"] is False
    assert client.post(f"/api/studio/tasks/{job['id']}/dismiss").status_code == 204
    [row] = client.get("/api/studio/tasks").json()["jobs"]
    assert row["dismissed"] is True and row["status"] == "failed"
    assert client.post("/api/studio/tasks/nope/dismiss").status_code == 404
