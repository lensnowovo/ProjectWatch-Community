"""Replan evidence contract: synthetic storage and fake models, no paid calls."""
import json

import pytest

from project_watch.studio import insight, plan_replan
from project_watch.studio.app import create_studio_app


@pytest.fixture
def env(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    pipe = app.state.studio_pipeline
    pid = pipe.repository.create_project("合成药业").id
    pipe.store.upsert_project(pid, {"legal_name": "合成药业"}, consent=True)
    plan = pipe.monitoring_plan
    watch = plan.add(pid, {"text": "合成资产一期入组进度如何？", "label": "入组",
                           "axis": "self", "objects": ["合成资产（SYN101）"],
                           "queries": ["合成资产 入组"]}, actor="human", origin="human")
    eid = pipe.store.create_event(pid, {"title": "合成资产一期进展更新",
                                       "summary": "合成资产一期已入组 12 例患者",
                                       "category": "company", "importance": "major"}, [])
    return pipe, pid, plan, watch, eid


class Model:
    ready = True

    def __init__(self, changes):
        self.changes = changes
        self.inputs = []

    def json(self, system, user, max_tokens=0):
        self.inputs.append(user)
        return {"changes": self.changes(user) if callable(self.changes) else self.changes}


def run(pipe, pid, model, trigger="external"):
    return plan_replan.run(pipe, model, pid, since="2000-01-01", trigger=trigger)


def material(pipe, pid, *texts):
    with pipe.store._connect() as db:
        db.execute("INSERT INTO studio_doc_changes (id,project_id,source_ids,data,created_at) VALUES (?,?,?,?,?)",
                   ("synthetic-changes", pid, "[]", json.dumps({"changes": [
                       {"kind": "新进展", "text": text} for text in texts]}, ensure_ascii=False),
                    "2026-10-14T00:00:00+00:00"))


def note(text, refs):
    return {"action": "note", "item": "W1", "text": text, "refs": refs, "why": "本轮新增依据"}


def test_model_can_use_the_same_numbered_summary_as_validation(env):
    pipe, pid, plan, watch, _ = env
    model = Model(lambda prompt: [note("一期已入组 12 例" if "12 例" in prompt else "未收到入组人数", ["E1"])])
    assert run(pipe, pid, model)["noted"] == 1
    assert "12 例" in model.inputs[0]
    assert plan._get(pid, watch["id"])["current_state"] == "一期已入组 12 例"
    entry = next(c for c in plan.view(pid)["changes"] if c["action"] == "modify")["evidence"][0]
    assert entry["text"] == "合成资产一期已入组 12 例患者"


def test_numbers_cannot_be_borrowed_from_database_after_prompt_is_built(env):
    pipe, pid, plan, watch, eid = env
    def respond(prompt):
        with pipe.store._connect() as db:
            db.execute("UPDATE studio_events SET summary=? WHERE id=?", ("已入组 99 例患者", eid))
        return [note("一期已入组 99 例", ["E1"])]
    result = run(pipe, pid, Model(respond))
    assert result["noted"] == 0 and result["dropped"] == 1
    assert plan._get(pid, watch["id"])["current_state"] == ""


@pytest.mark.parametrize("kind", ["material", "gap"])
@pytest.mark.parametrize("text,expected", [
    ("行政办公场地已迁至新园区", 0),
    ("合成药业行政办公场地已迁至新园区", 0),
    ("SYN101 入组进度需要继续关注", 1),
    ("XSYN101X 厂房搬迁", 0),
    ("入组进度仍待公司补充", 1),
])
def test_materials_and_gaps_need_a_focus_specific_link(env, monkeypatch, kind, text, expected):
    pipe, pid, plan, watch, _ = env
    if kind == "material":
        material(pipe, pid, text)
        ref, trigger = "M1", "internal"
    else:
        monkeypatch.setattr(insight, "load", lambda *_: {"analysis": {"gaps": [text]}})
        ref, trigger = "G1", "external"
    result = run(pipe, pid, Model([note(text, [ref])]), trigger)
    assert result["noted"] == expected
    assert bool(plan._get(pid, watch["id"])["current_state"]) == bool(expected)


def test_unrelated_material_cannot_support_completed_clinical_recruitment(env):
    pipe, pid, plan, watch, _ = env
    material(pipe, pid, "行政办公场地已迁至新园区")
    result = run(pipe, pid, Model([note("一期入组已完成", ["M1"])]), "internal")
    assert result["noted"] == 0
    assert plan._get(pid, watch["id"])["current_state"] == ""


@pytest.mark.parametrize("number,expected", [(12, 1), (99, 0)])
def test_filtered_material_cannot_supply_numbers_to_a_valid_reference(env, number, expected):
    pipe, pid, plan, watch, _ = env
    material(pipe, pid, "合成资产已入组 12 例", "其他项目报告病例 99 例")
    result = run(pipe, pid, Model([note(f"一期已入组 {number} 例", ["M1", "M2"])]), "internal")
    assert result["noted"] == expected
    if expected:
        entry = next(c for c in plan.view(pid)["changes"] if c["action"] == "modify")
        assert [e["ref"] for e in entry["evidence"]] == ["M1"]
    else:
        assert plan._get(pid, watch["id"])["current_state"] == ""
