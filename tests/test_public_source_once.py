"""Offline explicit metadata grants; old paid uncertainty is never replayed."""

import copy
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
import test_collection_host as fixtures
from test_collection_pages import work

from project_watch.studio import public_source_once as once
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError, Response
from project_watch.studio.material_ownership import project_guard
from project_watch.studio.research_sources import research_sources


@pytest.fixture
def terminal(tmp_path):
    pipeline, ex = fixtures.setup.__wrapped__(tmp_path)
    # A settled original external round, plus an unrelated old model unknown.
    ex.call("open_data", "old-public-read", lambda: Response(200, {}, b"{}"))
    pipeline.store.finish(ex.job["id"], lease_token=ex.job["lease_token"])
    with closing(pipeline.store._connect()) as db, db:
        db.execute(
            "INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
            "VALUES (?,?,'model','synthetic','unknown',?)",
            (ex.job["id"], "f" * 64, datetime.now(UTC).isoformat()),
        )
    return pipeline, ex


def prepare(terminal, sources=("openalex_affiliation",)):
    pipeline, ex = terminal
    frozen = once.plan(
        pipeline.store, ex.job["id"], [{"source": s, "query": "Synthetic Host"} for s in sources]
    )
    grant = dict(
        approved=True,
        plan_sha256=once.digest(frozen),
        maximum_new_open_data=len(sources),
        project_open_data_ceiling=frozen["project_open_data_ceiling"],
        deadline=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    return frozen, grant


def send(*args):
    assert args[0] == "GET" and args[3] is None
    if "openalex.org" in args[1]:
        return Response(200, {}, json.dumps({"meta": {"count": 1}, "results": [work()]}).encode())
    return Response(200, {}, b'{"hitCount":0,"resultList":{"result":[]}}')


def execute(terminal, frozen=None, grant=None, **kwargs):
    frozen, grant = (frozen, grant) if frozen is not None else prepare(terminal)
    return once.run(
        terminal[0].store,
        frozen,
        grant,
        runtime_guard=kwargs.pop("runtime_guard", lambda: None),
        transport=kwargs.pop("transport", send),
        enabled=kwargs.pop("enabled", True),
        **kwargs,
    )


def test_source_saved_visible_no_material_event_followup_and_history_kept(terminal):
    pipeline, ex = terminal
    frozen, grant = prepare(terminal)
    old = copy.deepcopy(frozen["scope"])
    outcome = execute(terminal, frozen, grant)
    assert {
        k: outcome[k] for k in ("status", "saved_count", "source_calls", "model_calls")
    } == dict(status="completed", saved_count=1, source_calls=1, model_calls=0)
    result = research_sources(pipeline.store, ex.job["project_id"], "paper")
    assert result["total"] == 1 and result["items"][0]["processing_status"] == "pending"
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_evidence_tasks").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM studio_events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM studio_jobs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM studio_call_recoveries").fetchone()[0] == 0
        assert (
            dict(db.execute("SELECT * FROM studio_jobs WHERE id=?", (ex.job["id"],)).fetchone())
            == old["job"]
        )
        assert (
            db.execute(
                "SELECT status FROM studio_job_calls WHERE call_key=?", ("f" * 64,)
            ).fetchone()[0]
            == "unknown"
        )
        assert (
            db.execute("SELECT count(*) FROM studio_job_calls WHERE kind='open_data'").fetchone()[0]
            == 2
        )
        with pytest.raises(HttpError, match="request_outcome_unknown"):
            project_guard(db, ex.job["project_id"], ex.job["id"])
    with pytest.raises(ValueError, match="consumed"):
        prepare(terminal)


@pytest.mark.parametrize(
    "change",
    [
        "default_off",
        "not_approved",
        "digest",
        "cap",
        "expiry",
        "extended",
        "wire",
        "source",
        "query",
    ],
)
def test_bad_grants_never_send_or_start(terminal, change):
    frozen, grant = prepare(terminal)
    enabled = True
    if change == "default_off":
        enabled = False
    if change == "not_approved":
        grant["approved"] = False
    if change == "digest":
        grant["plan_sha256"] = "0" * 64
    if change == "cap":
        grant["project_open_data_ceiling"] += 1
    if change in ("expiry", "extended"):
        grant["deadline"] = (
            datetime.now(UTC) + timedelta(minutes=-1 if change == "expiry" else 120)
        ).isoformat()
    if change == "wire":
        frozen["requests"][0]["wire"]["method"] = "POST"
    if change == "source":
        frozen["requests"][0]["source"] = "search"
    if change == "query":
        frozen["requests"][0]["query"] = "unconfirmed alias"
    if change in ("wire", "source", "query"):
        grant["plan_sha256"] = once.digest(frozen)
    with pytest.raises((ValueError, HttpError)):
        execute(
            terminal,
            frozen,
            grant,
            enabled=enabled,
            transport=lambda *_: pytest.fail("must not send"),
        )
    assert terminal[1].read_checkpoint(once.START) is None


@pytest.mark.parametrize(
    "change", ["pause", "version", "alias", "unknown", "call", "recovery", "queued"]
)
def test_changed_snapshot_refuses_dispatch(terminal, change):
    pipeline, ex = terminal
    frozen, grant = prepare(terminal)
    with closing(pipeline.store._connect()) as db, db:
        if change == "pause":
            db.execute("UPDATE studio_projects SET status='paused'")
        if change == "version":
            db.execute("UPDATE studio_projects SET config_version=config_version+1")
        if change == "alias":
            db.execute("UPDATE studio_projects SET aliases='[\"Other\"]'")
        if change == "unknown":
            db.execute("UPDATE studio_job_calls SET status='started' WHERE kind='model'")
        if change == "call":
            db.execute("UPDATE studio_job_calls SET response_status=201 WHERE kind='open_data'")
        if change == "recovery":
            db.execute(
                "INSERT INTO studio_call_recoveries(job_id,base_key,previous_key,next_key,created_at) VALUES (?,?,?,?,?)",
                (ex.job["id"], "a" * 64, "f" * 64, "b" * 64, datetime.now(UTC).isoformat()),
            )
        if change == "queued":
            db.execute("UPDATE studio_jobs SET status='queued'")
    with pytest.raises((ValueError, HttpError)):
        execute(terminal, frozen, grant, transport=lambda *_: pytest.fail("must not send"))


def test_crash_lock_blocks_new_grant_and_ordinary_execution(terminal):
    pipeline, ex = terminal
    with closing(pipeline.store._connect()) as db, db:
        db.execute(
            "INSERT INTO studio_job_checkpoints VALUES (?,?,?)", (ex.job["id"], once.START, "{}")
        )
    with pytest.raises(ValueError, match="consumed"):
        prepare(terminal)
    with (
        closing(pipeline.store._connect()) as db,
        pytest.raises(HttpError, match="independent_source_active"),
    ):
        ex._guard(db)


def test_public_timeout_stops_without_next_source_or_model_retry(terminal):
    pipeline, ex = terminal
    frozen, grant = prepare(terminal, ("openalex_affiliation", "europepmc_affiliation"))
    calls = []

    def timeout(*args):
        calls.append(args)
        raise TimeoutError("synthetic")

    with pytest.raises(HttpError, match="public_read_failed"):
        execute(terminal, frozen, grant, transport=timeout)
    assert len(calls) == 1
    assert ex.read_checkpoint(once.END)["status"] == "failed"
    with closing(pipeline.store._connect()) as db:
        assert (
            db.execute("SELECT count(*) FROM studio_job_calls WHERE status='unknown'").fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT count(*) FROM studio_job_calls WHERE status='failed_read'"
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(ValueError, match="consumed"):
        prepare(terminal)


def test_existing_lower_original_cap_still_wins(terminal):
    pipeline, ex = terminal
    with closing(pipeline.store._connect()) as db, db:
        db.execute(
            "INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
            (ex.job["id"], "material_round_caps_v1", json.dumps({"open_data": 1})),
        )
    with pytest.raises(HttpError, match="request_budget"):
        execute(terminal, transport=lambda *_: pytest.fail("old cap must hold"))


def test_runtime_drain_during_read_prevents_ingest(terminal):
    pipeline, _ex = terminal
    checks = []

    def runtime_guard():
        if checks:
            raise HttpError("runtime_draining")

    def pause(*args):
        checks.append("draining")
        return send(*args)

    with pytest.raises(HttpError, match="runtime_draining"):
        execute(terminal, transport=pause, runtime_guard=runtime_guard)
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_items").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM studio_job_calls WHERE status='started'").fetchone()[0]
            == 0
        )


def test_same_request_from_any_old_project_job_cannot_be_reissued(terminal):
    pipeline, _ex = terminal
    frozen, _ = prepare(terminal)
    with closing(pipeline.store._connect()) as db, db:
        db.execute(
            "UPDATE studio_job_calls SET call_key=? WHERE kind=?",
            (frozen["requests"][0]["journal_key"], "open_data"),
        )
    with pytest.raises(ValueError, match="previously attempted"):
        prepare(terminal)


def test_grant_excludes_other_job_transport_while_active(terminal):
    pipeline, ex = terminal

    def fenced(*args):
        with (
            closing(pipeline.store._connect()) as db,
            pytest.raises(HttpError, match="independent_source_active"),
        ):
            JobExecution(pipeline.store, ex.job)._guard(db)
        return send(*args)

    execute(terminal, transport=fenced)


def test_multiple_sources_share_existing_project_total_and_do_not_follow_pages(terminal):
    frozen, grant = prepare(
        terminal, ("openalex_affiliation", "europepmc_affiliation", "europepmc_patent")
    )
    assert frozen["project_open_data_ceiling"] == 4  # Includes the original settled GET.
    calls = []

    def paginated(*args):
        calls.append(args[1])
        if "openalex.org" in args[1]:
            return Response(
                200, {}, json.dumps({"meta": {"count": 500}, "results": [work()]}).encode()
            )
        return send(*args)

    result = execute(terminal, frozen, grant, transport=paginated)
    assert len(calls) == result["source_calls"] == 3
    assert result["sources"][0]["truncated"] is True
    assert result["sources"][0]["fetched_pages"] == 1


def test_old_unknown_on_source_cannot_use_model_uncertainty_exception(terminal):
    with closing(terminal[0].store._connect()) as db, db:
        db.execute("UPDATE studio_job_calls SET kind='open_data' WHERE status='unknown'")
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        prepare(terminal)


def test_new_unknown_during_get_preserves_arrived_reply_but_does_not_ingest(terminal):
    pipeline, ex = terminal

    def changed(*args):
        with closing(pipeline.store._connect()) as db, db:
            db.execute(
                "INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
                "VALUES (?,?,'model','synthetic','unknown',?)",
                (ex.job["id"], "e" * 64, datetime.now(UTC).isoformat()),
            )
        return send(*args)

    with pytest.raises(HttpError, match="independent_source_changed"):
        execute(terminal, transport=changed)
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_items").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM studio_job_calls WHERE kind='open_data' AND status='completed'"
            ).fetchone()[0]
            == 2
        )


def test_received_http_failure_stops_next_source(terminal):
    frozen, grant = prepare(terminal, ("openalex_affiliation", "europepmc_patent"))
    sent = []

    def unavailable(*args):
        sent.append(args)
        return Response(503, {}, b"{}")

    with pytest.raises(HttpError, match="http_source_unavailable"):
        execute(terminal, frozen, grant, transport=unavailable)
    assert len(sent) == 1
    assert terminal[1].read_checkpoint(once.END)["source_calls"] == 1


def test_exact_get_transport_rejects_same_origin_and_cross_origin_redirects():
    import urllib.request

    from project_watch.studio.http import NoRedirectHandler

    request = urllib.request.Request("https://api.openalex.org/works?search=Synthetic")
    for target in ("https://api.openalex.org/works?search=Other", "https://other.invalid/"):
        with pytest.raises(HttpError, match="unsafe_redirect"):
            NoRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)


def test_source_only_cannot_rewrite_already_triaged_item(terminal):
    pipeline, ex = terminal
    with closing(pipeline.store._connect()) as db, db:
        db.execute(
            "INSERT INTO studio_items(id,project_id,key,category,title,url,source,snippet,collected_at,triaged) "
            "VALUES ('existing',?,'existing','paper','Old','https://doi.org/10.0/1','Synthetic','Old',?,1)",
            (ex.job["project_id"], datetime.now(UTC).isoformat()),
        )
    with pytest.raises(HttpError, match="source_only_admitted_item"):
        execute(terminal)
    with closing(pipeline.store._connect()) as db:
        assert (
            db.execute("SELECT title FROM studio_items WHERE id='existing'").fetchone()[0] == "Old"
        )
        assert db.execute("SELECT count(*) FROM studio_evidence_versions").fetchone()[0] == 0


@pytest.mark.parametrize("selection", ["disabled", "not_applicable"])
def test_manual_source_exclusion_is_not_overridden(terminal, selection):
    from project_watch.studio.collection_targets import decide_source

    pipeline, ex = terminal
    decide_source(
        pipeline.store,
        ex.job["project_id"],
        adapter="openalex_affiliation",
        selection=selection,
        reason="Synthetic owner decision",
        expected_version=0,
    )
    with pytest.raises(ValueError, match="excluded by owner"):
        prepare(terminal)


def test_source_selection_change_during_get_prevents_ingest(terminal):
    from project_watch.studio.collection_targets import decide_source

    pipeline, ex = terminal

    def changed(*args):
        decide_source(
            pipeline.store,
            ex.job["project_id"],
            adapter="openalex_affiliation",
            selection="disabled",
            reason="Synthetic owner decision",
            expected_version=0,
        )
        return send(*args)

    with pytest.raises(HttpError, match="independent_source_changed"):
        execute(terminal, transport=changed)
    with closing(pipeline.store._connect()) as db:
        assert db.execute("SELECT count(*) FROM studio_items").fetchone()[0] == 0


def test_cumulative_count_and_permanent_lock_are_project_wide(terminal):
    pipeline, ex = terminal
    with closing(pipeline.store._connect()) as db, db:
        old = dict(db.execute("SELECT * FROM studio_jobs WHERE id=?", (ex.job["id"],)).fetchone())
        old["id"] = "other-terminal"
        db.execute(
            "INSERT INTO studio_jobs ("
            + ",".join(old)
            + ") VALUES ("
            + ",".join("?" for _ in old)
            + ")",
            list(old.values()),
        )
        db.execute(
            "INSERT INTO studio_job_calls(job_id,call_key,kind,provider,status,created_at) "
            "VALUES ('other-terminal','old-other-call','open_data','synthetic','completed',?)",
            (datetime.now(UTC).isoformat(),),
        )
    frozen, grant = prepare(terminal)
    assert frozen["project_open_data_ceiling"] == 3
    execute(terminal, frozen, grant)
    with pytest.raises(ValueError, match="consumed"):
        once.plan(
            pipeline.store,
            "other-terminal",
            [{"source": "europepmc_affiliation", "query": "Synthetic Host"}],
        )


@pytest.mark.parametrize("field", ["maximum_new_open_data", "project_open_data_ceiling"])
def test_grant_caps_require_integer_not_numeric_coercion(terminal, field):
    frozen, grant = prepare(terminal)
    grant[field] = float(grant[field])
    with pytest.raises(ValueError, match="finite source grant"):
        execute(terminal, frozen, grant)
    with closing(terminal[0].store._connect()) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM studio_job_checkpoints WHERE stage=?", (once.START,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("during", [False, True])
def test_recovery_alias_cannot_reroute_or_consume_source_request(terminal, during):
    pipeline, ex = terminal
    frozen, grant = prepare(terminal)
    key = frozen["requests"][0]["journal_key"]

    def alias():
        with closing(pipeline.store._connect()) as db, db:
            db.execute(
                "INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
                (ex.job["id"], "dsh_request:" + key, json.dumps({"next_key": "synthetic-slot"})),
            )

    if during:
        # Insert after plan validation, before the START admission transaction.
        with pytest.raises(ValueError, match="source plan changed"):
            execute(
                terminal,
                frozen,
                grant,
                runtime_guard=alias,
                transport=lambda *_: pytest.fail("recovery alias must not send"),
            )
    else:
        alias()
        with pytest.raises(ValueError, match="recovery identity"):
            prepare(terminal)
    with closing(pipeline.store._connect()) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM studio_job_calls WHERE call_key IN (?,?)",
                (key, "synthetic-slot"),
            ).fetchone()[0]
            == 0
        )
