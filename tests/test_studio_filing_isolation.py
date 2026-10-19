"""Leased filing reads: synthetic oversized originals must not erase other results."""

import pytest

from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response


def setup_filings(tmp_path, transport):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project("Synthetic Optics")
    pipeline.store.upsert_project(project.id, {"legal_name": "Synthetic Optics Ltd"}, consent=True)
    pipeline.store.add_items(project.id, "company", [
        {"key": key, "title": f"Issuer announcement {key}",
         "url": f"https://static.cninfo.com.cn/{key}.PDF", "source": "Synthetic filing",
         "snippet": "", "published_at": None, "extra": {"matched": "fulltext"}}
        for key in ("large", "good")])
    pipeline.http_factory = lambda _settings: Http(transport)
    pipeline.run_external = lambda job: pipeline.fetch_bodies(job["project_id"], lambda _: None)
    pipeline.store.enqueue(project.id, "external")
    return pipeline, project.id, pipeline.store.claim()


@pytest.mark.parametrize("failure", ["response_too_large", "http_failure"])
def test_bad_filing_does_not_abort_leased_batch(tmp_path, monkeypatch, failure):
    calls = []

    def transport(method, url, *_args):
        calls.append(url)
        if url.endswith("large.PDF"):
            if failure == "http_failure":
                return Response(404, {}, b"not found")
            raise HttpError(failure, detail="private diagnostic must not persist")
        return Response(200, {}, b"%PDF synthetic")

    monkeypatch.setattr("project_watch.studio.pipeline.filing_passages",
                        lambda *_args: "[page 1] Synthetic Optics Ltd received a new order.")
    pipeline, pid, job = setup_filings(tmp_path, transport)
    pipeline.run(job)
    execution = JobExecution(pipeline.store, job)
    assert execution.read_checkpoint("dispatch_completed") == {"completed": True}
    rows = {row["key"]: row for row in pipeline.store.untriaged(pid)}
    assert rows["large"]["body_status"] == "unavailable"
    assert rows["good"]["body_status"] == "filing_text"
    assert "received a new order" in rows["good"]["body"]
    assert execution.usage()["open_data"]["unknown"] == 0
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["request_budget", "provider_backoff", "request_outcome_unknown"])
def test_execution_holds_are_not_downgraded_to_empty_filings(tmp_path, failure):
    def transport(*_args):
        raise HttpError(failure)

    pipeline, _pid, job = setup_filings(tmp_path, transport)
    with pytest.raises(HttpError, match=failure):
        pipeline.run(job)
    assert JobExecution(pipeline.store, job).read_checkpoint("dispatch_completed") is None


def test_oversized_read_replays_without_fetch_or_secret_diagnostic(tmp_path):
    calls = []

    def transport(*_args):
        calls.append(1)
        raise HttpError("response_too_large", detail="synthetic private diagnostic")

    pipeline, _pid, job = setup_filings(tmp_path, transport)
    execution = JobExecution(pipeline.store, job)
    http = execution.http(Http(transport), lambda _url: ("open_data", "synthetic"))
    for _ in range(2):
        with pytest.raises(HttpError) as exc:
            http.get("https://static.cninfo.com.cn/large.PDF")
        assert exc.value.code == "response_too_large" and "private" not in str(exc.value)
    assert calls == [1]
    assert execution.usage()["open_data"]["unknown"] == 0


@pytest.mark.parametrize("kind,method", [("model", "POST"), ("search", "POST"), ("open_data", "POST")])
def test_paid_or_write_oversized_outcomes_remain_held(tmp_path, kind, method):
    def transport(*_args):
        raise HttpError("response_too_large")

    pipeline, _pid, job = setup_filings(tmp_path, transport)
    execution = JobExecution(pipeline.store, job)
    http = execution.http(Http(transport), lambda _url: (kind, "synthetic"))
    with pytest.raises(HttpError, match="response_too_large"):
        http.transport(method, "https://provider.example/api", {}, b"{}", 30)
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        http.transport(method, "https://provider.example/api", {}, b"{}", 30)
    assert execution.usage()[kind]["unknown"] == 1


def test_actual_oversized_response_is_not_retained_or_repeated(tmp_path):
    from project_watch.studio.http import MAX_RESPONSE_BYTES

    calls = []

    def transport(*_args):
        calls.append(1)
        return Response(200, {}, b"x" * (MAX_RESPONSE_BYTES + 1))

    pipeline, _pid, job = setup_filings(tmp_path, transport)
    execution = JobExecution(pipeline.store, job)
    http = execution.http(Http(transport), lambda _url: ("open_data", "synthetic"))
    for _ in range(2):
        with pytest.raises(HttpError, match="response_too_large"):
            http.get("https://static.cninfo.com.cn/large.PDF")
    assert calls == [1]
    with pipeline.store._connect() as db:
        row = db.execute("SELECT status,response_body FROM studio_job_calls WHERE job_id=?", (job["id"],)).fetchone()
        assert row["status"] == "failed_read" and row["response_body"] is None


def test_expired_lease_during_oversized_read_cannot_complete(tmp_path):
    def transport(*_args):
        with pipeline.store._connect() as db:
            db.execute("UPDATE studio_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (job["id"],))
            db.commit()
        raise HttpError("response_too_large")

    pipeline, _pid, job = setup_filings(tmp_path, transport)
    with pytest.raises(RuntimeError, match="lease"):
        pipeline.run(job)
    assert JobExecution(pipeline.store, job).read_checkpoint("dispatch_completed") is None
