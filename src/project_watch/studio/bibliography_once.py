"""Explicit review of one unattempted, budget-deferred paper on its original job.

No extraction, source transport, scheduler/API entry or budget reset. A separate
reviewed grant is mandatory; old once grants, tasks and checkpoints are retained.
The normal analysis child may use only the original round's reserved balance.
"""

import json
import uuid
from contextlib import closing
from datetime import UTC, datetime

from project_watch.studio import (
    dsh_record_review,
    dsh_runtime,
    event_insight_work,
    evidence_tasks,
    progressive_bootstrap,
    research_records,
)
from project_watch.studio import material_once as previous
from project_watch.studio.dsh_bootstrap import COVERAGE, PublicBootstrap
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.material_ownership import project_guard
from project_watch.studio.store import now

START = "bibliography_review_once_v1"
END = "bibliography_review_once_result_v1"


def _settled_tail(execution, db, cap):
    """Retain the existing shared tail; only an idle, unspent tail can advance."""
    budget = execution.read_checkpoint(event_insight_work.BUDGET, db=db)
    if not budget:
        return None
    row = db.execute(
        "SELECT * FROM studio_jobs WHERE id=? AND project_id=?",
        (budget.get("tail_job_id"), execution.job["project_id"]),
    ).fetchone()
    if budget.get("cap") != cap or not row or row["status"] != "done":
        raise ValueError("original analysis tail is not settled")
    tail = JobExecution(execution.store, dict(row))
    payload = (
        tail.read_checkpoint(event_insight_work.INPUT, db=db)
        or tail.read_checkpoint("public_stage_input_v1", db=db)
        or {}
    )
    if payload.get("round_root_job_id") != execution.job["id"]:
        raise ValueError("analysis tail belongs to another round")
    if db.execute(
        """SELECT 1 FROM studio_job_calls c WHERE
        (c.job_id=? OR EXISTS (SELECT 1 FROM studio_job_budget_links b WHERE b.job_id=?
         AND b.origin_job_id=c.job_id AND b.call_key=c.call_key))
        AND (c.status IN ('started','unknown') OR (c.kind='model' AND c.job_id!=?))""",
        (row["id"], row["id"], execution.job["id"]),
    ).fetchone():
        raise ValueError("analysis tail has unresolved or spent model balance")
    return dict(
        budget=budget,
        budget_raw=db.execute(
            "SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?",
            (execution.job["id"], event_insight_work.BUDGET),
        ).fetchone()[0],
        job_sha256=previous.digest(dict(row)),
        checkpoints_sha256=previous.digest(
            [
                list(r)
                for r in db.execute(
                    "SELECT stage,data FROM studio_job_checkpoints WHERE job_id=? ORDER BY stage",
                    (row["id"],),
                )
            ]
        ),
        calls_sha256=previous.digest(
            [
                list(r)
                for r in db.execute(
                    "SELECT job_id,call_key,status,kind,provider FROM studio_job_calls WHERE job_id=? ORDER BY call_key",
                    (row["id"],),
                )
            ]
        ),
    )


def plan(pipeline, job_id, task_id):
    with closing(pipeline.store._connect()) as db:
        db.execute("BEGIN")
        return _plan(pipeline, db, job_id, task_id)


def _plan(pipeline, db, job_id, task_id):
    store = pipeline.store
    job = dict(db.execute("SELECT * FROM studio_jobs WHERE id=?", (job_id,)).fetchone())
    if job["status"] != "done":
        raise ValueError("completed original round required")
    if db.execute(
        "SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage=?", (job_id, START)
    ).fetchone():
        raise ValueError("bibliography review already consumed")
    project_guard(db, job["project_id"], job_id)
    if store.get_project(job["project_id"])["status"] != "active":
        raise HttpError("project_paused")
    if db.execute(
        "SELECT 1 FROM studio_jobs WHERE project_id=? AND status IN ('queued','running')",
        (job["project_id"],),
    ).fetchone():
        raise ValueError("project has other work")
    if db.execute(
        """SELECT 1 FROM studio_job_calls c WHERE c.status IN ('started','unknown') AND
        (c.job_id=? OR EXISTS (SELECT 1 FROM studio_job_budget_links b WHERE b.job_id=?
        AND b.origin_job_id=c.job_id AND b.call_key=c.call_key))""",
        (job_id, job_id),
    ).fetchone():
        raise HttpError("request_outcome_unknown")
    task, identity = previous._material(db, job, task_id)
    if task["status"] != "pending" or json.loads(task["result"]):
        raise ValueError("unattempted pending material required")
    if (
        db.execute("SELECT 1 FROM studio_material_calls WHERE task_id=?", (task_id,)).fetchone()
        or db.execute(
            "SELECT 1 FROM studio_job_checkpoints WHERE job_id=? AND stage IN (?,?,?)",
            (
                job_id,
                "extract_attempt:" + task_id,
                "extract_result:" + task_id,
                evidence_tasks.DEFERRED + task_id,
            ),
        ).fetchone()
    ):
        raise ValueError("selected material already attempted")
    execution = JobExecution(store, job)
    deferred = execution.read_checkpoint("material_deferred:" + task_id, db=db)
    caps = execution.read_checkpoint("material_round_caps_v1", db=db)
    owner = execution.read_checkpoint(progressive_bootstrap.OWNER, db=db) or {}
    round_state = execution.read_checkpoint(event_insight_work.ROUND, db=db) or {}
    if (
        deferred
        != dict(
            task_id=task_id,
            policy=task["policy"],
            dispatch="not_started",
            reason="publication_reserve",
        )
        or owner.get("owner") != "materials"
        or not caps
        or type(caps.get("model")) is not int
    ):
        raise ValueError("explicit unattempted budget deferral required")
    if (
        type(round_state.get("cap")) is not int
        or round_state.get("collection_cap") != caps["model"]
        or not 0 <= round_state["cap"] - caps["model"] <= 6
        or round_state.get("analysis_enabled") is False
    ):
        raise ValueError("original unused analysis reserve required")
    if execution.read_checkpoint(previous.START, db=db) and not execution.read_checkpoint(
        previous.END, db=db
    ):
        raise ValueError("previous continuation not settled")
    used = execution.budget_used("model", db=db)
    if caps["model"] - used < 1:
        raise ValueError("one original review call required")
    source = evidence_tasks._input(execution, task)
    candidate = research_records.bibliographic_proposal(store, task, source)
    if candidate is None:
        raise ValueError("bounded attributed bibliography required")
    return dict(
        job_id=job_id,
        project_id=job["project_id"],
        original_job=job,
        original_task=task,
        material=identity,
        original_caps=caps,
        used_model=used,
        maximum_new_model=1,
        source_calls=0,
        extraction_calls=0,
        admission_calls=1,
        analysis_round_cap=round_state["cap"],
        analysis_reserved=round_state["cap"] - caps["model"],
        proposal=candidate,
        model_binding=pipeline.model_binding(),
        original_tail=_settled_tail(execution, db, round_state["cap"]),
        checkpoints_sha256=previous.digest(
            [
                list(r)
                for r in db.execute(
                    "SELECT stage,data FROM studio_job_checkpoints WHERE job_id=? ORDER BY stage",
                    (job_id,),
                )
            ]
        ),
        calls_sha256=previous.digest(
            [
                list(r)
                for r in db.execute(
                    "SELECT job_id,call_key,status,kind,provider FROM studio_job_calls WHERE job_id=? ORDER BY call_key",
                    (job_id,),
                )
            ]
        ),
    )


class ReviewExecution(previous.OnceExecution):
    def __init__(self, pipeline, job, frozen, token, deadline):
        super().__init__(pipeline.store, job, frozen, token, deadline)
        self.pipeline = pipeline
        self.budgets["model"] = min(frozen["original_caps"]["model"], frozen["used_model"] + 1)

    @staticmethod
    def _stage(stage):
        return "bibliography-once:" + previous.digest(stage)[:40]

    def _guard(self, db):
        JobExecution._guard(self, db)
        saved = JobExecution._read_checkpoint(self, db, START)
        if not saved or saved["token"] != self.token or now() >= self.deadline:
            raise RuntimeError("bibliography lease expired or replaced")
        project_guard(db, self.job["project_id"], self.job["id"])
        if (
            db.execute(
                "SELECT status FROM studio_projects WHERE project_id=?", (self.job["project_id"],)
            ).fetchone()[0]
            != "active"
        ):
            raise HttpError("project_paused")
        if db.execute(
            "SELECT 1 FROM studio_jobs WHERE project_id=? AND id!=? AND status IN ('running','queued')",
            (self.job["project_id"], self.job["id"]),
        ).fetchone():
            raise ValueError("project has other work")
        task, identity = previous._material(db, self.job, self.frozen["material"]["task_id"])
        proposal = research_records.bibliographic_proposal(
            self.store, task, evidence_tasks._input(self, task)
        )
        if (
            identity != self.frozen["material"]
            or proposal != self.frozen["proposal"]
            or self.pipeline.model_binding() != self.frozen["model_binding"]
            or _settled_tail(self, db, self.frozen["analysis_round_cap"])
            != self.frozen["original_tail"]
        ):
            raise ValueError("bibliography inputs changed")


def begin(pipeline, frozen, *, deadline, grant_id):
    seconds = (datetime.fromisoformat(deadline) - datetime.now(UTC)).total_seconds()
    if not grant_id or not 0 < seconds <= 5400:
        raise ValueError("finite bibliography review grant required")
    store = pipeline.store
    with closing(store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if _plan(pipeline, db, frozen["job_id"], frozen["material"]["task_id"]) != frozen:
            raise ValueError("bibliography baseline changed")
        token = uuid.uuid4().hex
        db.execute(
            "INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
            (
                frozen["job_id"],
                START,
                json.dumps(
                    dict(
                        plan=frozen,
                        token=token,
                        deadline=deadline,
                        grant_id=grant_id,
                        started_at=now(),
                    )
                ),
            ),
        )
        db.execute(
            "UPDATE studio_jobs SET status='running',lease_token=?,lease_until=? WHERE id=? AND status='done'",
            (token, deadline, frozen["job_id"]),
        )
        job = dict(
            db.execute("SELECT * FROM studio_jobs WHERE id=?", (frozen["job_id"],)).fetchone()
        )
    return ReviewExecution(pipeline, job, frozen, token, deadline)


def finish(execution, outcome):
    with closing(execution.store._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT lease_token FROM studio_jobs WHERE id=?", (execution.job["id"],)
        ).fetchone()
        if not row or row[0] != execution.token:
            raise RuntimeError("bibliography lease replaced; preserve owner")
        db.execute(
            "INSERT INTO studio_job_checkpoints VALUES (?,?,?)",
            (
                execution.job["id"],
                END,
                json.dumps(
                    dict(
                        outcome=outcome,
                        ended_at=now(),
                        new_model_used=execution.budget_used("model", db=db)
                        - execution.frozen["used_model"],
                    )
                ),
            ),
        )
        original = execution.frozen["original_job"]
        columns = [k for k in original if k != "id"]
        db.execute(
            "UPDATE studio_jobs SET " + ",".join('"' + k + '"=?' for k in columns) + " WHERE id=?",
            [original[k] for k in columns] + [original["id"]],
        )


def run(pipeline, execution):
    outcome = {"status": "stopped", "reason": "execution_failed"}
    gateway = None
    try:
        task = execution.frozen["original_task"]
        source = evidence_tasks._input(execution, task)
        proposal = evidence_tasks.RecordProposal.model_validate(execution.frozen["proposal"])
        observations = [row.model_dump() for row in proposal.observations]
        used = {ref for row in observations for ref in row["refs"]}
        # The old not_started deferral stays intact; this explicit operation
        # saves a candidate on the SAME task without invoking any extractor.
        evidence_tasks._save(
            execution,
            task,
            "complete",
            dict(
                observations=observations,
                refs={ref: source["refs"][ref] for ref in used},
                review_status="pending",
            ),
        )
        settings, http, _, collectors = pipeline.tools(execution=execution)
        research = PublicBootstrap(pipeline, execution, collectors, None)
        gateway = dsh_runtime.Gateway(
            research, settings.model, dsh_runtime.guarded_http(research, http)
        )
        progressive_bootstrap._map_records(research, task_id=task["id"])
        if (
            execution.read_checkpoint("material_mapping_v2")["issues"]
            or len(research.state["records"]) != 1
        ):
            raise ValueError("single bibliography candidate required")
        old = pipeline.store.get_public_profile(research.project_id) or {}
        if (
            dsh_record_review.pending_admission_calls(
                research, research.state["records"], old.get("facts") or []
            )
            != 1
        ):
            raise ValueError("admission exceeds single call")
        _, _, receipt = dsh_record_review.admit(
            research, research.state["records"], old.get("facts") or []
        )
        if receipt["rejected"]:
            outcome = {"status": "stopped", "reason": "semantic_rejected", "admission": receipt}
            return outcome
        with closing(pipeline.store._connect()) as db:
            before = dict(
                db.execute(
                    "SELECT h.event_id,h.revision_id FROM studio_event_revision_heads h JOIN studio_events e ON e.id=h.event_id WHERE e.project_id=?",
                    (research.project_id,),
                )
            )
        result = research.pw_finish({"gaps": {k: "本次仅复核一份已有论文" for k in COVERAGE}})
        research.save()
        with closing(pipeline.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            execution._guard(db)
            versions = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT h.event_id,h.revision_id FROM studio_event_revision_heads h JOIN studio_events e ON e.id=h.event_id WHERE e.project_id=?",
                    (research.project_id,),
                )
                if before.get(r[0]) != r[1]
            }
            pipeline.objects.ensure_subject(research.project_id, db=db)
            work, unresolved = event_insight_work.inputs(
                pipeline, research.project_id, versions, db=db
            )
            state = dict(
                cap=execution.frozen["analysis_round_cap"], objects=work, unresolved=unresolved
            )
            child = (
                event_insight_work._enqueue(
                    pipeline, db, research.project_id, execution.job["id"], state
                )
                if work
                else None
            )
        outcome = dict(
            status="partial",
            reason="bibliography_published",
            publication=result,
            insight_job_id=child["id"] if child else None,
            unresolved=unresolved,
            analysis_reserved=execution.frozen["analysis_reserved"],
        )
        return outcome
    except BaseException as exc:
        outcome = {"status": "stopped", "reason": getattr(exc, "code", type(exc).__name__)}
        raise
    finally:
        if gateway:
            gateway.closing.set()
        finish(execution, outcome)
