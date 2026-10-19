"""深度分析: numbered cross-source evidence, reference-bound judgments, scheduling. Synthetic data, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from project_watch.studio import insight, insight_generation


def _setup(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("合成细胞治疗")
    store = StudioStore(repo.db_path)
    store.put_profile(project.id, {"one_liner": "合成 CAR-T 公司", "summary": "合成公司做自体 CAR-T",
                                   "sections": [{"title": "业务与产品", "points": [{"text": "核心产品已获批", "refs": [
                                       {"source_id": "s1", "source_name": "合成年报.pdf", "locator": "page 3"}]}]}],
                                   "risks": [{"text": "单一产品依赖", "refs": []}]})
    store.add_items(project.id, "competitor", [{"key": "c1", "title": "竞品甲获批二线适应症", "url": "https://n.example/c1",
                                                 "source": "示例", "snippet": "", "published_at": "2026-05-01"}])
    store.add_items(project.id, "paper", [{"key": "a1", "title": "通用型 CAR-T 综述", "url": "https://p.example/a1",
                                            "source": "OpenAlex", "snippet": "", "published_at": "2026-02-01"}])
    _triage(store)
    return project, store, Pipeline(repo, store, SettingsStore(tmp_path / "settings.json"))


def _triage(store):
    """Only items triaged as relevant reach analysis; mark the synthetic ones as such."""
    from contextlib import closing

    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_items SET relevant=1, triaged=1")


class Model:
    ready = True
    user = ""

    def __init__(self, data):
        self.data = data

    def json(self, system, user, max_tokens=0):
        Model.user = user
        return self.data


def test_gather_numbers_every_source_and_fingerprints(tmp_path):
    project, _store, pipeline = _setup(tmp_path)
    gathered = insight_generation.gather(pipeline, project.id)
    codes = list(gathered["numbered"])
    assert any(c.startswith("D") for c in codes)
    assert gathered["numbered"]["D1"]["kind"] == "internal"
    assert "内部资料" in gathered["text"] and "[D1]" in gathered["text"]
    assert len(gathered["fingerprint"]) == 40
    assert insight_generation.gather(pipeline, project.id)["fingerprint"] == gathered["fingerprint"]  # stable for same inputs


def test_judgments_keep_only_referenced_points_and_cap_confidence():
    numbered = {"D1": {"kind": "internal", "text": "核心产品已获批", "source_id": "s1", "source_name": "年报", "locator": "page 3"},
                "C1": {"kind": "public", "text": "竞品甲获批", "title": "竞品甲获批", "url": "https://n.example/c1", "date": "2026-05-01"}}
    data = {"headline": "合成判断",
            "position": {"verdict": "ahead", "confidence": "high", "summary": "略领先",
                         "for": [{"text": "已获批", "refs": ["D1"]}, {"text": "无依据的说法", "refs": []}, {"text": "编造编号", "refs": ["Z9"]}],
                         "against": [{"text": "竞品追赶", "refs": ["c1"]}], "peers": ["竞品甲"]},
            "technology": {"route": "自体 CAR-T", "maturity": "frontier", "consensus": "consensus", "confidence": "high",
                           "academic": [], "industry": [], "risks": [{"text": "制备成本高", "refs": ["D1"]}]},
            "industry": {"verdict": "booming", "confidence": "medium", "drivers": [{"text": "商保", "refs": ["X1"]}]},
            "gaps": ["竞品销售额"]}
    out = insight_generation.normalize(data, numbered)
    assert [p["text"] for p in out["position"]["for"]] == ["已获批"]  # unreferenced and unknown codes dropped
    assert out["position"]["against"][0]["refs"][0]["url"] == "https://n.example/c1"  # codes are case-insensitive
    assert out["position"]["for"][0]["refs"][0]["source_id"] == "s1"
    assert out["position"]["verdict"] == "ahead" and out["position"]["confidence"] == "medium"  # 2 sources cannot be high
    assert out["technology"]["consensus"] == "unclear"  # no academic or industry signal
    assert out["technology"]["confidence"] == "low"
    assert out["industry"]["verdict"] == "unclear"  # invalid label and no referenced evidence
    assert [r["code"] for r in out["cited"]] == ["C1", "D1"]
    assert out["gaps"] == ["竞品销售额"]


def test_run_saves_record_and_history(tmp_path):
    project, store, pipeline = _setup(tmp_path)
    data = {"headline": "合成总判断", "position": {"verdict": "on_par", "confidence": "low", "for": [{"text": "已获批", "refs": ["D1"]}]},
            "technology": {"maturity": "mainstream", "consensus": "forming", "confidence": "low",
                           "academic": [{"text": "综述增多", "refs": ["A1"]}]}, "industry": {}, "gaps": []}
    record = insight_generation.run(pipeline, Model(data), project.id)
    assert "[A1]" in Model.user and "通用型 CAR-T 综述" in Model.user
    assert record["analysis"]["position"]["verdict"] == "on_par"
    assert record["analysis"]["technology"]["consensus"] == "forming"
    assert insight.load(store, project.id)["fingerprint"] == record["fingerprint"]
    assert insight.history(store, project.id)[0]["position"] == "on_par"


def test_due_waits_for_change_and_spacing(tmp_path):
    project, store, pipeline = _setup(tmp_path)
    now = datetime.now(UTC)
    assert insight_generation.due(pipeline, project.id, now=now)  # never analysed
    insight_generation.run(pipeline, Model({"headline": "x"}), project.id)
    assert insight_generation.due(pipeline, project.id, now=now + timedelta(hours=30)) is None  # sources unchanged
    store.add_items(project.id, "paper", [{"key": "a2", "title": "体内 CAR-T 新进展", "url": "https://p.example/a2",
                                           "source": "OpenAlex", "snippet": "", "published_at": "2026-06-01"}])
    _triage(store)
    assert insight_generation.due(pipeline, project.id, now=now + timedelta(hours=2)) is None  # too soon after the last run
    assert insight_generation.due(pipeline, project.id, now=now + timedelta(hours=30))
    store.enqueue(project.id, "external")
    assert insight_generation.due(pipeline, project.id, now=now + timedelta(hours=30)) is None  # collection still running


def test_api_returns_record_and_manual_refresh_needs_a_model(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    project = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成公司"}, "consent": True}).json()
    view = client.get(f"/api/studio/projects/{project['id']}/insight").json()
    assert view["record"] is None and view["job"] is None and view["labels"]["position"]["ahead"] == "略领先"
    client.post(f"/api/studio/projects/{project['id']}/activate")
    response = client.post(f"/api/studio/projects/{project['id']}/refresh", json={"kind": "insight"})
    assert response.status_code == 409 and "模型" in response.json()["detail"]


def test_unknown_paid_outcome_holds_deep_analysis(tmp_path):
    from contextlib import closing

    import pytest

    project, store, pipeline = _setup(tmp_path)
    job = store.enqueue(project.id, "internal")
    with closing(store._connect()) as db, db:
        db.execute("UPDATE studio_jobs SET status='failed' WHERE id=?", (job["id"],))
        columns = {r[1] for r in db.execute("PRAGMA table_info(studio_job_calls)")}
        values = {"job_id": job["id"], "kind": "model", "status": "unknown", "call_key": "k1"}
        for name in columns - set(values):
            notnull = next(r for r in db.execute("PRAGMA table_info(studio_job_calls)") if r[1] == name)
            if notnull[3] and notnull[4] is None:
                values[name] = 0 if "INT" in (notnull[2] or "").upper() else "x"
        db.execute(f"INSERT INTO studio_job_calls ({','.join(values)}) VALUES ({','.join('?' * len(values))})", tuple(values.values()))
    assert insight.held(store, project.id)
    assert insight_generation.due(pipeline, project.id) is None
    with pytest.raises(ValueError, match="结果不明"):
        insight_generation.run(pipeline, Model({"headline": "x"}), project.id)


def test_removing_a_project_hides_it_stops_work_and_can_be_restored(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    keep = client.post("/api/studio/projects", json={"entity": {"legal_name": "保留公司"}, "consent": True}).json()
    gone = client.post("/api/studio/projects", json={"entity": {"legal_name": "移出公司"}, "consent": True}).json()
    store = app.state.studio_store
    store.enqueue(gone["id"], "external")
    assert client.delete(f"/api/studio/projects/{gone['id']}").status_code == 204
    assert [p["id"] for p in client.get("/api/studio/projects").json()] == [keep["id"]]
    assert all(j["status"] != "queued" for j in store.jobs(gone["id"]))
    assert gone["id"] not in store.due_external_projects(0)
    assert [p["id"] for p in client.get("/api/studio/removed-projects").json()] == [gone["id"]]
    detail = client.get(f"/api/studio/projects/{gone['id']}").json()  # data stays readable
    assert detail["project"]["status"] == "removed"
    restored = client.post(f"/api/studio/projects/{gone['id']}/restore").json()
    assert restored["status"] == "active"
    assert {p["id"] for p in client.get("/api/studio/projects").json()} == {keep["id"], gone["id"]}
    client.patch(f"/api/studio/projects/{keep['id']}", json={"status": "paused"})
    client.delete(f"/api/studio/projects/{keep['id']}")
    assert client.post(f"/api/studio/projects/{keep['id']}/restore").json()["status"] == "paused"  # pause survives


def test_professional_database_records_reach_the_model_only_with_consent(tmp_path, monkeypatch):
    """设置 → 交给 AI 分析 decides; off by default, so the page's promise and the prompt always agree."""
    from project_watch.studio import commercial

    project, _store, pipeline = _setup(tmp_path)
    monkeypatch.setattr(commercial, "targets", lambda *_a: [{"legal_name": "合成细胞治疗"}])
    monkeypatch.setattr(commercial.CommercialStore, "view", lambda *_a, **_k: {"records": [
        {"kind": "funding", "company": "合成细胞治疗", "provider": "xiniu", "date": "2024-12-13", "round": "A轮",
         "amount": "1亿人民币", "investors": "合成资本独家", "title": "", "url": "", "relationship": "company"},
        {"kind": "news", "company": "合成细胞治疗", "provider": "xiniu", "title": "无关新闻"}]})
    off = insight_generation.gather(pipeline, project.id)
    assert not [c for c in off["numbered"] if c.startswith("F")] and "合成资本独家" not in off["text"]
    pipeline.settings_store.update({"commercial": {"model_use": True}})
    on = insight_generation.gather(pipeline, project.id)
    funding = [e for c, e in on["numbered"].items() if c.startswith("F")]
    assert len(funding) == 1 and "合成资本独家" in funding[0]["text"] and "[F1]" in on["text"]


def test_model_use_consent_is_saved_strictly_and_shown(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    client = TestClient(create_studio_app(tmp_path, start_worker=False))
    assert client.get("/api/studio/settings").json()["commercial"]["model_use"] is False
    assert client.put("/api/studio/settings", json={"commercial": {"model_use": "yes"}}).status_code in (400, 422)
    assert client.put("/api/studio/settings", json={"commercial": {"model_use": True}}).json()["commercial"]["model_use"] is True
    # Other commercial edits keep the consent as it was.
    assert client.put("/api/studio/settings", json={"commercial": {"daily_limit": 6}}).json()["commercial"]["model_use"] is True


def test_company_and_chain_judgments_follow_the_same_evidence_rules():
    numbered = {"D1": {"kind": "internal", "text": "现金可支撑 20 个月", "source_id": "s1", "source_name": "BP", "locator": "page 9"},
                "N1": {"kind": "public", "text": "主要客户追加订单", "title": "主要客户追加订单", "url": "https://n.example/n1"}}
    out = insight_generation.normalize({"headline": "合成判断",
                             "operations": {"verdict": "pressured", "confidence": "high", "summary": "亏损扩大",
                                            "for": [], "against": [{"text": "亏损扩大", "refs": ["D1"]}],
                                            "change_if": ["完成新一轮融资"]},
                             "chain": {"verdict": "favorable", "confidence": "medium", "summary": "客户追加",
                                       "for": [{"text": "主要客户追加订单", "refs": ["X9"]}]}}, numbered)
    assert out["operations"]["verdict"] == "pressured" and out["operations"]["confidence"] == "low"  # one source
    assert out["operations"]["change_if"] == ["完成新一轮融资"]
    assert out["chain"]["verdict"] == "unclear" and out["chain"]["for"] == []  # unknown reference → no evidence
    assert "公司运营" in insight_generation.SYSTEM and "产业链" in insight_generation.SYSTEM
    assert insight.VERDICTS["operations"]["pressured"] == "承压" and insight.VERDICTS["chain"]["favorable"] == "有利"
