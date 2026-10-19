"""Synthetic events only. Grouping is a reversible reading suggestion."""

from copy import deepcopy

import pytest

from project_watch.external_events import group_reports


def report(
    id,
    *,
    actor="示例器械",
    date="9月22日",
    stage="签约",
    amount="",
    project="医用精密导管及介入器械制造项目",
):
    return dict(
        id=id,
        project_id="p",
        category="competitors",
        match_term=actor,
        statement=f"{actor}推进{project}",
        summary=f"{date}，{actor}的{project}在示例高新区{stage}{amount}。其他背景。",
        published_at="2026-09-24T00:00:00Z",
    )


def test_same_actor_date_object_stage_groups_without_changing_sources():
    rows = [report("a"), report("b")]
    rows[1]["statement"] = "示例器械新项目签约"
    original = deepcopy(rows)
    groups = group_reports("p", rows)
    assert len(groups) == 1 and groups[0]["member_ids"] == ["a", "b"]
    assert groups[0]["status"] == "candidate" and not groups[0]["event_verified"]
    assert rows == original and group_reports("p", list(reversed(rows))) == groups


@pytest.mark.parametrize(
    "changes",
    [
        dict(actor="另一器械"),
        dict(date="9月23日"),
        dict(stage="拟签约"),
        dict(stage="否认签约"),
        dict(stage="投产"),
        dict(amount="，投资5亿元"),
        dict(project="医用超声诊断及成像设备生产项目"),
    ],
)
def test_conflicting_identity_time_stage_or_figures_stay_separate(changes):
    assert len(group_reports("p", [report("a"), report("b", **changes)])) == 2


def test_publication_is_not_event_date_and_policy_never_merged_as_applicable():
    a, b = report("a"), report("b")
    a["summary"] = a["summary"].replace("9月22日，", "近日，")
    assert len(group_reports("p", [a, b])) == 2
    a, b = report("a"), report("b")
    a["category"] = b["category"] = "policy"
    assert len(group_reports("p", [a, b])) == 2
    b["project_id"] = "another"
    assert [g["member_ids"] for g in group_reports("p", [a, b])] == [["a"]]


def test_conflicting_location_or_counterparty_is_not_grouped():
    a, b = report("a"), report("b")
    b["summary"] = b["summary"].replace("示例高新区", "另一高新区")
    assert len(group_reports("p", [a, b])) == 2
    a, b = report("a"), report("b")
    a["summary"] = a["summary"].replace("的", "与甲方公司合作的", 1)
    b["summary"] = b["summary"].replace("的", "与乙方公司合作的", 1)
    assert len(group_reports("p", [a, b])) == 2
