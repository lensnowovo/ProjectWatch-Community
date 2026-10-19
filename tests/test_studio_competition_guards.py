"""P0 conservative competition projections. Synthetic, no model/network calls."""

from copy import deepcopy

from fastapi.testclient import TestClient

from project_watch.studio import landscape as ls
from project_watch.studio.app import create_studio_app


class Reply:
    def __init__(self, data):
        self.data = data

    def json(self, *_args, **_kwargs):
        return self.data


def test_plan_is_not_evidence_for_company_admission():
    evidence = ls.Evidence()
    evidence.add(title="行业融资观察", url="https://news.example/a", source="示例",
                 date="2026-10-01", text="投资机构关注产业新机会", kind="competitor")
    ref = [{"ref": "E1", "quote": "投资机构关注产业新机会"}]
    result = ls.competition(Reply({"companies": [
        {"name": "合成候选甲", "threat": "high", "threat_reason": "客户重叠", "refs": ref},
    ]}), evidence, {"name": "合成目标"}, {}, ["合成候选甲"])
    assert result["companies"] == []
    assert result["candidates"] == []  # plan admission happens separately, as pending only
    assert evidence.rejected > 0


def test_valid_financing_quote_does_not_establish_direct_competition():
    evidence = ls.Evidence()
    quote = "合成候选甲完成新一轮融资"
    evidence.add(title=quote, url="https://news.example/a", source="示例",
                 date="2026-10-01", text=quote, kind="competitor")
    ref = [{"ref": "E1", "quote": quote}]
    result = ls.competition(Reply({"summary": "双方主要客户完全重叠", "summary_refs": ref,
        "companies": [{"name": "合成候选甲", "threat": "high",
                       "threat_reason": "双方主要客户完全重叠", "refs": ref,
                       "cells": {"主要客户": {"text": "客户完全重叠", "refs": ref}}}],
    }), evidence, {"name": "合成目标"}, {}, [])
    assert result["companies"] == []
    [candidate] = result["candidates"]
    assert candidate["review_status"] == "pending"
    assert candidate["refs"][0]["quote"] == quote
    assert not candidate.get("threat") and not candidate.get("cells")
    assert not result["summary"]


def test_legacy_rating_and_customer_cells_are_not_projected_as_facts():
    legacy = {"summary": "具有高威胁", "summary_refs": [], "dimensions": list(ls.DIMENSIONS),
              "companies": [{"name": "合成候选甲", "is_self": False, "threat": "high",
                  "threat_reason": "同一客户", "cells": {
                      "技术路线": {"text": "投资银行下调评级", "refs": []},
                      "主要客户": {"text": "合成投资方入股公司", "refs": []}}}]}
    before = deepcopy(legacy)
    result = ls.complete_competitors(legacy, ["合成候选乙"], [
        {"id": "e", "company": "合成候选乙", "title": "融资", "type": "融资"}])
    assert result["companies"] == []
    assert [c["name"] for c in result["candidates"]] == ["合成候选甲", "合成候选乙"]
    assert all(c["review_status"] == "pending" and not c.get("cells") for c in result["candidates"])
    assert not result["summary"]
    assert legacy == before  # keep raw results for later revalidation, not destructive migration
    assert ls.complete_competitors(result, ["合成候选乙"], []) == result


def test_multisubject_news_is_not_assigned_by_plan_order_or_ai_summary():
    events = [{"id": "event", "title": "合成投资方投资合成公司乙，此前曾投资合成公司甲",
               "summary": "合成公司甲完成融资", "event_at": "2026-10-01"}]
    moves = ls.competitor_moves(events, ["合成公司甲", "合成公司乙"])
    assert moves == ls.competitor_moves(events, ["合成公司乙", "合成公司甲"])
    [move] = moves
    assert move["company"] == "" and move["attribution_status"] == "pending"
    assert move["mentioned_companies"] == ["合成公司乙", "合成公司甲"]
    assert move["type"] == "其他" and move["topic"] == "融资"
    assert move["title"] == events[0]["title"]


def test_ai_summary_cannot_create_company_mention():
    [move] = ls.competitor_moves([{"id": "e", "title": "行业交流会召开",
                                 "summary": "合成公司甲融资增长"}], ["合成公司甲"])
    assert move["mentioned_companies"] == [] and not move["company"]


def test_get_projects_conservatively_projects_without_writes_or_model_calls(tmp_path, monkeypatch):
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成目标"}, "consent": True}).json()["id"]
    store = app.state.studio_store
    store.put_plan(pid, {"competitors": [{"name": "合成候选甲"}]})
    legacy = {"generated_at": "2026-10-01", "competition": {"companies": [
        {"name": "合成目标", "is_self": True, "cells": {}},
        {"name": "合成候选乙", "is_self": False, "threat": "high", "cells": {}}]}}
    store.put_landscape(pid, legacy)
    saved = store.get_landscape(pid)
    jobs = store.jobs(pid, 100)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("GET must not write, call models or search")

    monkeypatch.setattr(app.state.studio_pipeline, "tools", forbidden)
    monkeypatch.setattr(store, "put_landscape", forbidden)
    monkeypatch.setattr(store, "enqueue", forbidden)
    detail = client.get(f"/api/studio/projects/{pid}").json()
    comp = detail["landscape"]["competition"]
    assert [c["name"] for c in comp["companies"]] == ["合成目标"]
    assert {c["name"] for c in comp["candidates"]} == {"合成候选甲", "合成候选乙"}
    assert store.get_landscape(pid) == saved and store.jobs(pid, 100) == jobs
