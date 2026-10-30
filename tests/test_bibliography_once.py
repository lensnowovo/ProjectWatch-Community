"""Same task, one reserved review, old failed/deferred history retained."""

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_collection_plan import candidate, plan
from test_event_insight import install as install_events
from test_research_records import paper
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import bibliography_once as once
from project_watch.studio import event_insight_work, evidence_tasks, insight
from project_watch.studio.collection_ingest import CollectionIngest
from project_watch.studio.dsh_bootstrap import PublicBootstrap
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.material_once import END as PREVIOUS_END
from project_watch.studio.material_once import START as PREVIOUS_START
from project_watch.studio.sources import _item


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    pipeline, ex, seen = install_events(tmp_path, monkeypatch, monitor=False, source_count=0)
    ex.checkpoint(
        "material_pipeline_v1",
        dict(
            owner="materials",
            entry="bootstrap",
            policy=evidence_tasks.TYPED_POLICY,
            mapping_version="subject-v1",
        ),
    )
    event_insight_work.begin(ex, cap=16, shared=True)
    ex.checkpoint("material_round_caps_v1", {**ex.budgets, "model": 10})
    unit = plan(
        [candidate() | dict(category="paper", source="openalex_affiliation")],
        project_id=ex.job["project_id"],
        round_id=ex.job["id"],
    )["units"][0]
    refs = []
    for i in range(2):
        row = paper() | {"id": "https://openalex.org/W" + str(1234567 + i)}
        item = _item(
            "paper",
            row["display_name"],
            row["id"],
            source="Synthetic Journal",
            published_at=row["publication_date"],
            extra={"openalex_id": row["id"], "source_record": row},
        )
        refs += CollectionIngest(ex)(unit, [item])["evidence_refs"]
    ex.checkpoint("collection_source_imports_v1", refs)
    ex.checkpoint(
        "collection_admission_v1", {"enabled": False, "reason": "synthetic_saved_collection"}
    )
    _, _, _, collectors = pipeline.tools(execution=ex)
    research = PublicBootstrap(pipeline, ex, collectors, None)
    research.state["finished"] = {"status": "partial", "records": 0}
    research.save()
    with closing(pipeline.store._connect()) as db, db:
        ids = [r[0] for r in db.execute("SELECT id FROM studio_evidence_tasks ORDER BY rowid")]
        db.execute(
            "UPDATE studio_evidence_tasks SET result=? WHERE id=?",
            (json.dumps({"code": "material_output_limit", "dispatch": "completed"}), ids[0]),
        )
    ex.checkpoint(
        evidence_tasks.DEFERRED + ids[0], {"code": "material_output_limit", "dispatch": "completed"}
    )
    ex.checkpoint(
        "material_closeout_v1", {"reason": "publication_reserve", "pending_task_ids": ids}
    )
    ex.checkpoint(
        "material_deferred:" + ids[1],
        dict(
            task_id=ids[1],
            policy=evidence_tasks.TYPED_POLICY,
            dispatch="not_started",
            reason="publication_reserve",
        ),
    )
    ex.checkpoint(PREVIOUS_START, {"old_grant": "consumed"})
    ex.checkpoint(PREVIOUS_END, {"outcome": "material_output_limit"})
    for i in range(9):
        ex.call(
            "model",
            "settled-" + str(i),
            lambda: Response(200, {}, b'{"choices":[{"finish_reason":"length"}]}'),
        )
    pipeline.store.finish(ex.job["id"], lease_token=ex.job["lease_token"])
    frozen = once.plan(pipeline, ex.job["id"], ids[1])
    with closing(pipeline.store._connect()) as db:
        history = dict(
            db.execute(
                "SELECT stage,data FROM studio_job_checkpoints WHERE job_id=?", (ex.job["id"],)
            )
        )
        calls = [tuple(r) for r in db.execute("SELECT * FROM studio_job_calls ORDER BY call_key")]
    return pipeline, ex, frozen, history, calls, seen


def start(terminal):
    return once.begin(
        terminal[0],
        terminal[2],
        deadline=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        grant_id="synthetic-new-bibliography-grant",
    )


def with_settled_tail(terminal):
    pipeline, original, frozen, history, calls, seen = terminal
    tail = pipeline.store.enqueue(
        original.job["project_id"],
        "external_followup",
        automatic=True,
        input_version="synthetic-settled-tail",
    )
    with closing(pipeline.store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='done' WHERE id=?", (tail["id"],))
        db.execute(
            "INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
            (
                tail["id"],
                "public_stage_input_v1",
                json.dumps({"round_root_job_id": original.job["id"]}),
            ),
        )
        db.execute(
            "INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
            (
                original.job["id"],
                event_insight_work.BUDGET,
                json.dumps({"cap": 16, "tail_job_id": tail["id"]}),
            ),
        )
        db.execute(
            "INSERT INTO studio_job_budget_links SELECT ?,job_id,call_key FROM studio_job_calls WHERE job_id=?",
            (tail["id"], original.job["id"]),
        )
        history = dict(
            db.execute(
                "SELECT stage,data FROM studio_job_checkpoints WHERE job_id=?",
                (original.job["id"],),
            )
        )
    frozen = once.plan(pipeline, original.job["id"], frozen["material"]["task_id"])
    return pipeline, original, frozen, history, calls, seen


@pytest.mark.parametrize("mode", ["valid", "reject", "unknown", "settled_tail"])
def test_single_review_keeps_history_and_reserved_analysis(terminal, monkeypatch, mode):
    pipeline, original, frozen, history, calls, _ = terminal
    if mode == "settled_tail":
        terminal = with_settled_tail(terminal)
        pipeline, original, frozen, history, calls, _ = terminal
        mode = "valid"
    send = pipeline.http_factory(None).transport
    review_calls = []

    def transport(method, url, headers, raw, timeout):
        assert method == "POST" and url == "https://model.example.invalid/v1/chat/completions"
        body = json.loads(raw)
        if not body.get("tools"):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("purpose") == "public_record_admission":
                review_calls.append(body)
                assert len(review_calls) == 1
                if mode == "unknown":
                    raise TimeoutError("synthetic")
                value = synthetic_review(payload)
                if mode == "reject":
                    value["checks"][0].update(
                        status="insufficient", reason="Synthetic attribution rejection", evidence=[]
                    )
                return Response(
                    200,
                    {},
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "message": {"role": "assistant", "content": json.dumps(value)},
                                    "finish_reason": "stop",
                                }
                            ]
                        }
                    ).encode(),
                )
        assert mode == "valid" and body.get("tools"), "No extraction or extra admission allowed"
        return send(method, url, headers, raw, timeout)

    pipeline.http_factory = lambda _: Http(transport)
    ex = start(terminal)
    if mode == "unknown":
        with pytest.raises((TimeoutError, HttpError)):
            once.run(pipeline, ex)
    else:
        result = once.run(pipeline, ex)
        assert result["reason"] == (
            "bibliography_published" if mode == "valid" else "semantic_rejected"
        )
    assert len(review_calls) == 1 and ex.budget_used("model") == 10
    with closing(pipeline.store._connect()) as db:
        current = dict(
            db.execute(
                "SELECT stage,data FROM studio_job_checkpoints WHERE job_id=?", (ex.job["id"],)
            )
        )
        assert all(current[k] == v for k, v in history.items() if k != event_insight_work.BUDGET)
        if frozen["original_tail"]:
            saved_plan = json.loads(current[once.START])["plan"]
            assert saved_plan["original_tail"]["budget_raw"] == history[event_insight_work.BUDGET]
            assert json.loads(current[event_insight_work.BUDGET])["cap"] == 16
        assert (
            dict(db.execute("SELECT * FROM studio_jobs WHERE id=?", (ex.job["id"],)).fetchone())
            == frozen["original_job"]
        )
        for row in calls:
            assert (
                tuple(
                    db.execute(
                        "SELECT * FROM studio_job_calls WHERE job_id=? AND call_key=?",
                        (row[0], row[1]),
                    ).fetchone()
                )
                == row
            )
        assert db.execute("SELECT count(*) FROM studio_evidence_tasks").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM studio_material_calls").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT status FROM studio_evidence_tasks WHERE id=?",
                (frozen["material"]["task_id"],),
            ).fetchone()[0]
            == "complete"
        )
    with pytest.raises(ValueError, match="consumed"):
        once.plan(pipeline, ex.job["id"], frozen["material"]["task_id"])
    events = pipeline.store.list_events(project_id=ex.job["project_id"])
    assert len(events) == (1 if mode == "valid" else 0)
    if mode == "valid":
        job = pipeline.store.claim()
        assert job["kind"] == "insight" and job["id"] == result["insight_job_id"]
        child = JobExecution(pipeline.store, job)
        bound = child.read_checkpoint(event_insight_work.INPUT)
        assert bound["cap"] == 16 and bound["round_root_job_id"] == original.job["id"]
        assert child.budget_used("model") == 10
        # The normal analysis provider is separate from the one-call review stub.
        pipeline.http_factory = lambda _: Http(send)
        pipeline.run(job)
        assert insight.load(pipeline.store, ex.job["project_id"])["status"] == "current"
        assert child.budget_used("model") <= 16
    else:
        assert pipeline.store.claim() is None


@pytest.mark.parametrize("change", ["unknown", "attempt", "spent", "wrong_defer", "paused"])
def test_plan_rejects_unsafe_or_spent_inputs(terminal, change):
    pipeline, old, frozen, _, _, _ = terminal
    tid = frozen["material"]["task_id"]
    with closing(pipeline.store._connect()) as db, db:
        if change == "unknown":
            db.execute(
                "UPDATE studio_job_calls SET status='unknown' WHERE call_key=(SELECT call_key FROM studio_job_calls LIMIT 1)"
            )
        if change == "attempt":
            db.execute(
                "INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
                (old.job["id"], "extract_attempt:" + tid, "[]"),
            )
        if change == "spent":
            db.execute(
                "UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage=?",
                (json.dumps({"model": 9}), old.job["id"], "material_round_caps_v1"),
            )
        if change == "wrong_defer":
            db.execute(
                "UPDATE studio_job_checkpoints SET data=? WHERE job_id=? AND stage=?",
                ("{}", old.job["id"], "material_deferred:" + tid),
            )
        if change == "paused":
            db.execute(
                "UPDATE studio_projects SET status='paused' WHERE project_id=?",
                (old.job["project_id"],),
            )
    with pytest.raises((ValueError, HttpError)):
        once.plan(pipeline, old.job["id"], tid)


def test_expired_grant_changed_plan_and_no_source_dispatch(terminal):
    pipeline, _, frozen, _, _, _ = terminal
    with pytest.raises(ValueError, match="finite"):
        once.begin(pipeline, frozen, deadline="2000-01-01T00:00:00+00:00", grant_id="old")
    with pytest.raises(ValueError, match="baseline"):
        once.begin(
            pipeline,
            frozen | {"used_model": 0},
            deadline=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            grant_id="bad",
        )
    ex = start(terminal)
    for kind in ("search", "page", "open_data", "site"):
        with pytest.raises(HttpError):
            ex.call(kind, "denied", lambda: pytest.fail("external call"))
    ex.deadline = "2000-01-01T00:00:00+00:00"
    with pytest.raises(RuntimeError, match="expired"):
        ex.call("model", "denied", lambda: pytest.fail("late call"))
    once.finish(ex, {"status": "stopped", "reason": "synthetic"})


@pytest.mark.parametrize(
    "change", ["active_tail", "tail_unknown", "tail_model", "tail_drift", "model_drift"]
)
def test_settled_tail_and_binding_are_frozen(terminal, monkeypatch, change):
    terminal = with_settled_tail(terminal)
    pipeline, _old, frozen, _, _, _ = terminal
    tail = frozen["original_tail"]["budget"]["tail_job_id"]
    ex = start(terminal)
    with closing(pipeline.store._connect()) as db, db:
        if change == "active_tail":
            db.execute("UPDATE studio_jobs SET status='running' WHERE id=?", (tail,))
        if change in ("tail_unknown", "tail_model"):
            db.execute(
                "INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) VALUES (?,?,?,?,?,?)",
                (
                    tail,
                    "synthetic-new",
                    "model",
                    "synthetic",
                    "unknown" if change == "tail_unknown" else "completed",
                    "2026-01-01",
                ),
            )
        if change == "tail_drift":
            db.execute("INSERT INTO studio_job_checkpoints VALUES (?,?,?)", (tail, "changed", "{}"))
    if change == "model_drift":
        monkeypatch.setattr(pipeline, "model_binding", lambda: "changed-binding")
    with pytest.raises((ValueError, HttpError)):
        ex.call("model", "not-sent", lambda: pytest.fail("must not dispatch"))
    once.finish(ex, {"status": "stopped", "reason": "synthetic-drift"})
