"""Synthetic one-shot continuation; never real provider credentials."""
import json
from contextlib import closing

import pytest
import test_studio_dsh_monitor as fixtures

from project_watch.studio import recovery
from project_watch.studio.dsh_monitor import PublicMonitor
from project_watch.studio.dsh_runtime import Gateway, guarded_http, session_directory
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import ModelConfig


@pytest.fixture
def loop(tmp_path):
    return fixtures.loop.__wrapped__(tmp_path)


def request():
    # Reach the synthetic transport with a valid loaded-plugin request.
    return {"model": "synthetic", "messages": [{"role": "user", "content": "continue synthetic"}],
            "tools": [{"type": "function", "function": {"name": name}} for name in PublicMonitor.tools]}


def gateway(loop, transport):
    model = ModelConfig(base_url="https://model.example/v1", model="synthetic", api_key="synthetic")
    http = loop.execution.http(Http(transport), lambda _: ("model", "synthetic"))
    return Gateway(loop, model, guarded_http(loop, http))


def lost(*args):
    raise HttpError("connection_failed")


def ok(*args):
    return Response(200, {}, json.dumps({"choices": [{"message": {
        "role": "assistant", "content": "synthetic"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2}}).encode())


def failed(loop):
    settings = loop.pipeline.settings_store.load()
    settings.model = ModelConfig(base_url="https://model.example/v1", model="synthetic", api_key="synthetic")
    loop.pipeline.settings_store.save(settings)
    # Recreate after intentional fixture configuration, before first request.
    loop.execution.checkpoint("dsh_binding", None)
    loop = PublicMonitor(loop.pipeline, loop.execution, loop.collectors, loop.reader)
    loop.execution.checkpoint("recovery_inputs", {"binding": recovery.input_binding(loop.pipeline, loop.project_id)})
    watch = loop.watches()[0]
    loop.tool("pw_explore", {"watch_id": watch["id"], "query": watch["queries"][0]})
    with pytest.raises(HttpError):
        gateway(loop, lost).model_call(request())
    assert loop.execution.usage()["model"]["unknown"] == 1
    loop.store.finish(loop.execution.job["id"], error="request_outcome_unknown",
                      lease_token=loop.execution.job["lease_token"])
    return loop


def authorize(loop):
    jid = loop.execution.job["id"]
    preview = recovery.preview(loop.pipeline, loop.project_id, jid)
    assert preview["can_recover"]
    recovery.authorize(loop.pipeline, loop.project_id, jid, expected_version=preview["version"],
                       idempotency_key="synthetic-once", accept_duplicate_cost=True)
    job = loop.store.claim()
    return JobExecution(loop.store, job)


def test_authorized_same_job_continues_and_preserves_old_unknown(loop):
    loop = failed(loop)
    execution = authorize(loop)
    restored = PublicMonitor(loop.pipeline, execution, loop.collectors, loop.reader)
    directory = session_directory(restored)
    assert directory.name.startswith("r-")
    assert "同一任务续跑" in restored.task
    with pytest.raises(HttpError):
        restored.tool("pw_context", {})  # Authorization does not authorize tool bypass.
    calls = []
    def send(*args):
        calls.append(1)
        return ok()
    # Deliberately changed payload: a continuation is explicitly recorded, not exact replay.
    continued = {**request(), "messages": [{"role": "user", "content": "new checkpoint turn"}]}
    gateway(restored, send).model_call(continued)
    gateway(restored, send).model_call(continued)  # New request identity is replayable too.
    assert calls == [1]
    assert execution.read_checkpoint("dsh_hold") == {}
    restored.guard()
    assert restored.plan.explorations.view(restored.project_id)[restored.watches()[0]["id"]]["state"] == "active"
    with closing(restored.store._connect()) as db:
        rows = db.execute("SELECT status FROM studio_job_calls WHERE job_id=?", (execution.job["id"],)).fetchall()
        assert sorted(r[0] for r in rows) == ["completed", "unknown"]
        audit = db.execute("SELECT data FROM studio_job_checkpoints WHERE stage LIKE 'dsh_continuation:%'").fetchone()
        assert json.loads(audit[0])["mode"] == "checkpoint_continuation"
    assert execution.usage()["model"]["unknown"] == 0
    # A different continuation answer must NEVER masquerade as the old payload's reply.
    with pytest.raises(HttpError, match="recovery_payload_mismatch"):
        gateway(restored, send).model_call(request())
    assert calls == [1]


def test_new_unknown_stops_without_inheriting_authorization(loop):
    loop = failed(loop)
    restored = PublicMonitor(loop.pipeline, authorize(loop), loop.collectors, loop.reader)
    calls = []
    def send(*args):
        calls.append(1)
        return lost()
    g = gateway(restored, send)
    for _ in range(2):
        with pytest.raises(HttpError):
            g.model_call(request())
    assert calls == [1]
    assert restored.execution.usage()["model"]["unknown"] == 1
    assert recovery.dsh_model_continuation(restored.pipeline, restored.execution) is None
    with pytest.raises(HttpError):
        restored.tool("pw_context", {})


def test_bad_route_does_not_consume_slot(loop):
    loop = failed(loop)
    restored = PublicMonitor(loop.pipeline, authorize(loop), loop.collectors, loop.reader)
    g = gateway(restored, lambda *a: pytest.fail("unexpected dispatch"))
    with pytest.raises(ValueError):
        g.model_call({**request(), "model": "other"})
    assert recovery.dsh_model_continuation(restored.pipeline, restored.execution)
    assert restored.recovery_admission is False


def test_received_error_stays_held_without_reusing_consent(loop):
    loop = failed(loop)
    restored = PublicMonitor(loop.pipeline, authorize(loop), loop.collectors, loop.reader)
    calls = []
    def server_error(*args):
        calls.append(1)
        return Response(500, {}, b'{"error":"synthetic"}')
    g = gateway(restored, server_error)
    for _ in range(2):
        with pytest.raises(HttpError, match="model_http_500"):
            g.model_call(request())
    assert calls == [1]


def test_other_unknown_blocks_even_with_pending_slot(loop):
    loop = failed(loop)
    execution = authorize(loop)
    with pytest.raises(HttpError):
        execution.call("search", "synthetic-unrelated", lost)
    assert recovery.dsh_model_continuation(loop.pipeline, execution) is None
    with pytest.raises(HttpError):
        PublicMonitor(loop.pipeline, execution, loop.collectors, loop.reader)


@pytest.mark.parametrize("change", ["no_consent", "config", "paused", "foreign_job"])
def test_continuation_cannot_bypass_boundaries(loop, change):
    loop = failed(loop)
    if change == "no_consent":
        with loop.store._connect() as db, db:
            db.execute("UPDATE studio_jobs SET status='queued' WHERE id=?", (loop.execution.job["id"],))
        execution = JobExecution(loop.store, loop.store.claim())
    else:
        execution = authorize(loop)
    if change == "config":
        settings = loop.pipeline.settings_store.load()
        settings.model.model = "changed"
        loop.pipeline.settings_store.save(settings)
    elif change == "paused":
        with loop.store._connect() as db, db:
            db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (loop.project_id,))
    elif change == "foreign_job":
        loop.store.finish(execution.job["id"], error="held", lease_token=execution.job["lease_token"])
        queued = loop.store.enqueue(loop.project_id, "insight", input_version="synthetic-other")
        execution = JobExecution(loop.store, loop.store.claim())
        assert execution.job["id"] == queued["id"]
    with pytest.raises((HttpError, ValueError)):
        PublicMonitor(loop.pipeline, execution, loop.collectors, loop.reader)
