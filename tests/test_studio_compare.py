"""Planned names stay pending; legacy fields are never upgraded by news topics."""

from __future__ import annotations

from project_watch.studio import landscape as ls


def _self():
    return {"name": "光启视界", "is_self": True, "cells": {d: {"text": "", "refs": []} for d in ls.DIMENSIONS}}


def test_planned_competitors_get_candidates_not_comparison_columns():
    comp = {"companies": [_self(), {"name": "合成竞品甲科技有限公司", "is_self": False, "threat": "high", "threat_reason": "r", "refs": [],
                                    "cells": {d: {"text": "", "refs": []} for d in ls.DIMENSIONS}}]}
    moves = ls.competitor_moves([
        {"id": "e1", "title": "合成竞品甲完成5亿元C轮融资", "summary": "合成竞品甲完成5亿元C轮融资", "url": "https://n.example/1",
         "source": "36氪", "event_at": "2026-10-01", "importance": "major"},
        {"id": "e2", "title": "合成竞品乙获某车企定点", "summary": "", "url": "https://n.example/2", "source": "盖世",
         "event_at": "2026-09-20", "importance": "notable"},
    ], ["合成竞品甲", "合成竞品乙", "合成竞品丙"])
    result = ls.complete_competitors(comp, ["合成竞品甲", "合成竞品乙", "合成竞品丙"], moves)
    assert [c["name"] for c in result["companies"]] == ["光启视界"]
    # Legal suffix/name similarity alone does not prove an alias. Both source
    # leads remain visible until identity research can resolve them.
    assert [c["name"] for c in result["candidates"]] == ["合成竞品甲科技有限公司", "合成竞品甲", "合成竞品乙", "合成竞品丙"]
    assert all(c["review_status"] == "pending" and not c.get("cells") for c in result["candidates"])
    assert not result["companies"][0]["cells"]["最近动作"]["text"]


def test_existing_cells_are_not_overwritten():
    comp = {"companies": [{"name": "合成竞品甲", "is_self": False, "cells": {"最近动作": {"text": "原有", "refs": [{"id": "x"}]}}}]}
    moves = [{"id": "e", "title": "合成竞品甲发布新品", "company": "合成竞品甲", "type": "产品与价格", "date": "2026-10-01"}]
    ls.complete_competitors(comp, ["合成竞品甲"], moves)
    assert comp["companies"][0]["cells"]["最近动作"]["text"] == "原有"


def test_source_profiles_are_available_without_bilateral_support_and_survive_projection():
    cell = {"text": "合成筛选产品", "refs": [{"id": "r", "title": "合成公司介绍", "quote": "合成筛选产品"}]}
    comp = {"companies": [{"name": "合成公司", "is_self": False, "cells": {"产品": cell}, "threat": "high"}]}
    result = ls.complete_competitors(comp, [], [])
    assert result["profiles"] == [{"name": "合成公司", "cells": {"产品": cell}}]
    assert not result["companies"] and not result["summary"]
    assert ls.complete_competitors(result, [], []) == result
