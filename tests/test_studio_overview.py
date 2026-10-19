"""Company overview and OCR recovery; originals and model responses are synthetic."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from project_watch.domain import FragmentInput
from project_watch.extraction_process import extract_document_subprocess
from project_watch.extractors import ExtractionResult
from project_watch.local_ocr import ocr_capabilities
from project_watch.studio.app import create_studio_app


def test_offline_chinese_scan_in_parser_child(tmp_path):
    if ocr_capabilities()["engine"] != "rapidocr":
        pytest.skip("optional offline OCR extra unavailable")
    from PIL import Image, ImageDraw, ImageFont

    font = Path("C:/Windows/Fonts/msyh.ttc")
    image = Image.new("RGB", (1500, 600), "white")
    draw = ImageDraw.Draw(image)
    chinese = font.exists()
    selected_font = ImageFont.truetype(str(font), 60) if chinese else ImageFont.load_default(size=60)
    draw.text((60, 90), "合成公司 2025年营业收入5800万元" if chinese else "REVENUE 5800", font=selected_font, fill="black")
    draw.text((60, 220), "研发人员126人 客户6家" if chinese else "EMPLOYEES 126", font=selected_font, fill="black")
    pdf = tmp_path / "synthetic.pdf"
    image.save(pdf, "PDF", resolution=150)
    result = extract_document_subprocess(pdf, suffix=".pdf")
    assert not result.needs_ocr
    assert result.fragments[0].locator == "page 1 OCR"
    assert "5800" in result.fragments[0].text
    if chinese:
        assert "营业收入" in result.fragments[0].text


def test_partial_ocr_is_readable_and_retry_replaces_fragments(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    p = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成公司"}, "consent": True}).json()["id"]
    source = client.post(f"/api/studio/projects/{p}/files", files={"file": ("synthetic.txt", b"Synthetic", "text/plain")}).json()["source_id"]
    worker = app.state.intake_worker
    monkeypatch.setattr("project_watch.intake_worker.extract_document_subprocess", lambda *a, **k:
                        ExtractionResult([FragmentInput(ordinal=1, locator="page 1 OCR", text="营业收入5800万元")], True))
    worker.process_one()
    detail = client.get(f"/api/studio/projects/{p}").json()
    assert detail["documents"][0]["fragments"] == 1
    assert detail["pending_documents"] == 1
    assert app.state.studio_pipeline._fragments(p)[1]
    assert client.post(f"/api/studio/projects/{p}/files/{source}/retry").status_code == 202
    assert client.post(f"/api/studio/projects/{p}/files/{source}/retry").status_code == 409
    monkeypatch.setattr("project_watch.intake_worker.extract_document_subprocess", lambda *a, **k:
                        ExtractionResult([FragmentInput(ordinal=1, locator="page 1 OCR", text="营业收入5800万元"),
                                          FragmentInput(ordinal=2, locator="page 2 OCR", text="客户6家")], False))
    worker.process_one()
    detail = client.get(f"/api/studio/projects/{p}").json()
    assert detail["documents"][0]["status"] == "completed"
    assert detail["documents"][0]["fragments"] == 2
    other = client.post("/api/studio/projects", json={"entity": {"legal_name": "另一个合成公司"}, "consent": True}).json()["id"]
    assert client.post(f"/api/studio/projects/{other}/files/{source}/retry").status_code == 404
    store = app.state.studio_store
    store.mark_analyzed(p, [source])
    assert store.pending_documents(p) == 0
    # Completion after an earlier partial analysis must trigger fresh analysis.
    with store._connect() as db:
        db.execute("UPDATE intake_jobs SET updated_at='9999-01-01T00:00:00+00:00' WHERE source_id=?", (source,))
    assert store.pending_documents(p) == 1


def test_public_facts_require_matching_company_and_exact_quotes():
    from project_watch.studio.public_profile import build_public_profile

    class Search:
        has_search = False

    class Model:
        ready = True

        def json(self, system, text, **kwargs):
            assert "同行公司" not in text
            return {"facts": [
                {"kind": "business", "text": "从事光学模组", "refs": [{"ref": "P1", "quote": "合成光学专注车载光学模组"}]},
                {"kind": "person", "text": "某人为创始人", "refs": [{"ref": "P1", "quote": "不存在的文字"}]},
                {"kind": "product", "text": "不可引用", "refs": [{"ref": "P99", "quote": "合成光学专注车载光学模组"}]},
            ]}

    existing = [
        {"title": "合成光学简介", "url": "https://example.com/company", "snippet": "合成光学专注车载光学模组。"},
        {"title": "同行公司", "url": "https://example.com/other", "snippet": "同行公司创始人王某"},
        {"title": "合成光学", "url": "javascript:alert(1)", "snippet": "合成光学专注车载光学模组。"},
    ]
    result = build_public_profile({"name": "合成光学", "legal_name": "合成光学有限公司"}, Search(), Model(), existing)
    assert len(result["facts"]) == 1
    assert result["facts"][0]["refs"][0]["url"] == "https://example.com/company"
    assert result["facts"][0]["refs"][0]["quote"] == "合成光学专注车载光学模组"


def test_public_profile_preserves_internal_dossier_and_is_separate_job(tmp_path):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    p = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成公司"}, "consent": True}).json()["id"]
    store = app.state.studio_store
    store.put_profile(p, {"summary": "内部资料概述"})
    store.put_public_profile(p, {"facts": [{"kind": "business", "text": "公开业务"}]})
    detail = client.get(f"/api/studio/projects/{p}").json()
    assert detail["profile"]["summary"] == "内部资料概述"
    assert detail["public_profile"]["facts"][0]["text"] == "公开业务"
    job = client.post(f"/api/studio/projects/{p}/refresh", json={"kind": "public_profile"})
    assert job.status_code == 202 and job.json()["kind"] == "public_profile"


def test_trial_pipeline_requires_company_party_and_preserves_registered_phase():
    from project_watch.studio.public_profile import company_trials

    items = [{"title": "合成试验", "url": "https://clinicaltrials.gov/study/NCT000001",
              "published_at": "2026-01-01", "extra": {"nct_id": "NCT000001", "phase": "PHASE1/PHASE2",
              "sponsor": "合成生物有限公司", "conditions": ["合成适应症"], "status": "RECRUITING"}},
             {"title": "同行试验", "url": "https://clinicaltrials.gov/study/NCT000002",
              "extra": {"nct_id": "NCT000002", "phase": "PHASE3", "sponsor": "另一家公司"}}]
    [trial] = company_trials({"name": "合成生物", "legal_name": "合成生物有限公司"}, items)
    assert trial["phase"] == "PHASE1/PHASE2" and trial["status"] == "RECRUITING"
    assert trial["updated_at"] == "2026-01-01"


def test_json_object_repair_retains_required_schema():
    import json

    from project_watch.studio.http import Http, Response
    from project_watch.studio.llm import LLM
    from project_watch.studio.settings import ModelConfig

    calls = []

    def fake(method, url, headers, body, timeout):
        payload = json.loads(body)
        calls.append(payload)
        content = '[{"value":12}]' if len(calls) == 1 else '{"metrics":[{"value":12}]}'
        return Response(200, {}, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode())

    model = LLM(ModelConfig("openai", "https://api.deepseek.com", "deepseek-flash", "synthetic-key"), Http(fake))
    assert model.json('输出 JSON 对象，字段 metrics', '合成数据')["metrics"] == [{"value": 12}]
    assert len(calls) == 2
    assert all(p["response_format"] == {"type": "json_object"} for p in calls)
    assert "metrics" in calls[1]["messages"][0]["content"]


def test_truncated_reading_splits_preserves_refs_and_failed_jobs_stop_auto_retry(tmp_path, monkeypatch):
    from project_watch.studio.llm import ModelError

    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    p = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成公司"}, "consent": True}).json()["id"]
    source = client.post(f"/api/studio/projects/{p}/files", files={"file": ("synthetic.txt", ("合成内容 " * 600).encode(), "text/plain")}).json()["source_id"]
    app.state.intake_worker.process_one()
    pipeline, store = app.state.studio_pipeline, app.state.studio_store

    class Model:
        ready = True
        calls = 0
        profile_calls = 0

        def json(self, system, user, **kwargs):
            if "最值得持续跟踪的" in system:
                return {"questions": []}
            if "下面每个问题附带了本轮新命中的事件" in system:
                return {"results": []}
            if "正在阅读" in system:
                self.calls += 1
                assert "[F1]" in user
                if len(user) > 2000:
                    raise ModelError("synthetic truncation", code="model_output_truncated")
                return {"metrics": [{"name": "营业收入", "period": "2025", "value": 5800,
                                      "unit": "万元", "basis": "actual", "ref": "F1"}]}
            self.profile_calls += 1
            if self.profile_calls == 1:
                raise ModelError("synthetic summary truncation", code="model_output_truncated")
            assert kwargs["max_tokens"] == 12000 and "进一步精简" in system
            return {"one_liner": "合成公司", "summary": "合成档案"}

    model = Model()
    monkeypatch.setattr(pipeline, "tools", lambda: (None, None, model, None))
    job = store.enqueue(p, "internal")
    pipeline.run_internal(job)
    store.finish(job["id"])
    assert model.calls == 3
    assert model.profile_calls == 2
    assert all(m["source_id"] == source and m["value"] == 5800 for m in store.list_metrics(p))
    store.reset_analyzed(p)
    failed = store.enqueue(p, "internal")
    store.finish(failed["id"], error="synthetic provider failure")
    assert p not in store.unanalyzed_ready_projects()
    assert store.enqueue(p, "internal")["status"] == "queued"  # explicit retry remains available
