import json
from datetime import date

from project_watch.studio import techroutes

TODAY = date(2026, 10, 9)
PROJECT = {"name": "合成视界", "short_name": "合成视界", "legal_name": "合成视界科技有限公司"}


def doc(title, day, category="industry", institutions=None, url="https://x.example/1"):
    return {"title": title, "summary": "", "url": url, "source": "某来源", "category": category,
            "event_at": f"{day}T08:00:00+00:00", "created_at": f"{day}T08:00:00+00:00", "published_at": f"{day}T08:00:00+00:00",
            "collected_at": f"{day}T08:00:00+00:00", "extra": {"institutions": institutions or []},
            "sources": [{"title": title, "url": url, "source": "某来源"}]}


def test_term_extraction_units_acronyms_phrases():
    terms = techroutes._terms("905nm与1550nm路线之争：FMCW方案和MEMS架构，CEO称Tier1客户")
    assert {"905nm", "1550nm", "FMCW", "MEMS"} <= terms
    assert not {"CEO", "Tier1"} & terms


def test_suggest_prefers_route_comparisons_and_marks_own():
    industry = {"facts": [{"kind": "技术路线", "text": "905nm 与 1550nm 路线之争，成本仍是核心变量"}]}
    docs = [doc("1550nm激光雷达方案获更多车企采用", "2026-09-01"), doc("905nm发射模组降本", "2026-08-01"),
            doc("合成竞品甲发布新品", "2026-08-02")]
    out = techroutes.suggest(industry=industry, docs=docs, self_texts=["合成视界905nm发射模组获定点"],
                             exclude=["合成视界", "合成竞品甲"])
    assert [r["term"] for r in out][:2] == ["905nm", "1550nm"]
    assert out[0]["own"] is True and out[1]["own"] is False


def test_series_and_insight_share_shift():
    routes = [{"term": "905nm", "own": True}, {"term": "1550nm", "own": False}]
    docs = ([doc("905nm emitter", "2025-08-01", "paper")] * 3 + [doc("1550nm receiver", "2025-08-02", "paper")]
            + [doc("905nm emitter", "2026-08-01", "paper")] + [doc("1550nm FMCW", "2026-08-02", "paper")] * 4)
    rows = techroutes.series(routes, docs, TODAY)
    assert rows[0]["values"] == [3, 0, 0, 0, 1, 0] and rows[1]["values"] == [1, 0, 0, 0, 4, 0]
    assert rows[1]["kinds"] == {"论文": 5}
    text = techroutes.insight(rows)
    assert "已收录材料中，1550nm 的提及占比从前三个季度的 25% 升到近三个季度的 80%" in text
    assert "上个完整季度，1550nm 的提及次数首次高于 905nm" in text
    assert not any(word in text for word in ("超过公司", "优势", "领先", "共识"))


def test_more_mentions_do_not_infer_company_cost_or_performance_risk():
    rows = [{"term": "A", "own": True, "values": [12, 0, 0, 0, 12, 0], "total": 24},
            {"term": "B", "own": False, "values": [1, 0, 0, 0, 8, 0], "total": 9}]
    text = techroutes.insight(rows)
    assert "已收录材料中" in text
    assert not any(word in text for word in ("成本", "性能", "优势", "维持", "超过公司"))


def test_partial_quarter_does_not_trigger_mention_crossover():
    rows = [{"term": "A", "own": True, "values": [3, 0, 0, 0, 3, 0], "total": 6},
            {"term": "B", "own": False, "values": [1, 0, 0, 0, 1, 8], "total": 10}]
    assert "首次高于" not in techroutes.insight(rows)


def test_insight_needs_enough_data():
    rows = [{"term": "A", "own": True, "values": [1, 0, 0, 0, 0, 0], "total": 1},
            {"term": "B", "own": False, "values": [0, 0, 0, 0, 1, 0], "total": 1}]
    assert techroutes.insight(rows) == ""


def test_entrants_first_seen_within_90_days():
    papers = [doc("old", "2025-01-01", "paper", ["Old Lab"]), doc("new", "2026-09-01", "paper", ["New Lab"]),
              doc("again", "2026-09-05", "paper", ["Old Lab"])]
    deals = [doc("合成丁完成数千万元天使轮融资", "2026-09-19"), doc("合成竞品甲完成5亿元C轮融资", "2026-09-01", "competitor")]
    out = techroutes.entrants(papers=papers, deals_docs=deals, known=["合成视界", "合成竞品甲"], today=TODAY)
    names = [e["name"] for e in out]
    assert names == ["合成丁", "New Lab"]
    assert out[0]["what"] == "首次出现在赛道融资：天使轮 数千万元"


class FakeStore:
    def __init__(self):
        self.state = {}

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, value):
        self.state[key] = value


def test_user_routes_saved_and_used():
    store = FakeStore()
    techroutes.save(store, "p1", ["905nm", " 1550nm ", "905nm", "FMCW", "MEMS", "OPA"], "905nm")
    saved = techroutes.load(store, "p1")
    assert [r["term"] for r in saved["routes"]] == ["905nm", "1550nm", "FMCW", "MEMS"]
    view = techroutes.view(project=PROJECT, saved=saved, industry=None, docs=[], self_texts=[], papers=[], peers=[], today=TODAY)
    assert view["source"] == "user" and view["routes"][0]["own"] is True
    store.set_state(techroutes.STATE_KEY.format("p1"), json.dumps({}))
    view = techroutes.view(project=PROJECT, saved=techroutes.load(store, "p1"), industry=None, docs=[], self_texts=[],
                           papers=[], peers=[], today=TODAY)
    assert view["source"] == "auto" and view["routes"] == []
