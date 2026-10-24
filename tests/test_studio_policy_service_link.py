"""Synthetic attributable service-policy links, never broad sector aliases."""
import pytest

from project_watch.studio.questions import QuestionLedger


def watch(**overrides):
    return {"scope_kind": "question", "relation_type": "policy", "axis": "rules",
            "relation_target": "合成接口相关价格项目", "relation_aliases": [],
            "text": "合成接口的基本医保支付范围如何？", **overrides}


def match(text, **overrides):
    return QuestionLedger.public_row_match(watch(**overrides), {},
        {"body_status": "search_summary", "body": text})


@pytest.mark.parametrize("text", [
    "按规定将合成接口相关服务纳入医疗服务价格项目，将符合条件的医疗服务项目纳入医保支付范围。",
    "合成接口医疗服务价格项目明确最高限价，并不意味着已纳入医保支付。",
    "合成接口相关诊疗服务项目暂不纳入基本医保支付范围。",
    "合成接口相关服务拟纳入医保支付范围，仍须符合条件。",
])
def test_explicit_service_policy_link_is_attributable(text):
    found = match(text)
    assert found and found["basis"] == "search_summary"
    assert found["match_rule"] == "policy_service_link"
    assert found["matched_term"] == "合成接口相关价格项目"
    assert found["excerpt"] in text
    assert found["matched_text"] in found["excerpt"]
    assert QuestionLedger.public_focus_matches(watch(), {}, text)


@pytest.mark.parametrize("text", [
    "合成接口获得融资。另一治疗医疗服务项目纳入医保支付。",
    "另一治疗相关服务纳入医疗服务价格项目，将符合条件的项目纳入医保支付。",
    "合成接口相关服务获得融资，另一治疗纳入医保支付。",
    "合成接口相关服务。纳入医疗服务价格项目，并进入医保支付。",
    "合成接口相关服务" + "背景信息" * 100 + "纳入医保支付范围。",
    "合成接口相关服务纳入医疗服务价格项目。其他治疗项目进入医保支付。",
])
def test_sector_or_distant_policy_does_not_route(text):
    assert not match(text)


def test_relation_routes_price_link_but_payment_question_needs_payment():
    text = "合成接口相关服务纳入医疗服务价格项目。"
    assert match(text, scope_kind="relation")
    assert not match(text)


def test_scope_alias_and_ascii_boundaries():
    text = "示例治疗相关服务纳入医保支付范围。"
    assert match(text, relation_aliases=["示例治疗相关价格项目"])
    assert not match(text)
    assert not match("合成接口相关服务纳入医保支付范围。", relation_type="product")
    assert match("ABC 服务纳入医保支付范围。", relation_target="ABC相关价格项目")
    assert not match("XABC 服务纳入医保支付范围。", relation_target="ABC相关价格项目")
    assert not match("ABC-PLUS 服务纳入医保支付范围。", relation_target="ABC相关价格项目")


@pytest.mark.parametrize("status", ["unavailable", "read_failed", ""])
def test_unread_body_and_generated_summary_do_not_create_structural_links(status):
    text = "合成接口相关服务纳入医疗服务价格项目，并纳入医保支付范围。"
    assert not QuestionLedger.public_row_match(watch(), {},
        {"title": "行业材料", "summary": text, "body": text, "body_status": status})


def test_do_not_join_separate_header_fields_into_a_relationship():
    assert not QuestionLedger.public_row_match(watch(), {},
        {"title": "合成接口相关服务", "summary": "纳入医保支付范围。"})


def test_source_scope_is_not_upgraded_and_legacy_child_matches():
    for status, basis in [("article_body", "full_body"), ("partial_body", "partial_body"),
                          ("search_summary", "search_summary")]:
        found = QuestionLedger.public_row_match(watch(relation_type=""), {},
            {"body_status": status, "body": "合成接口相关服务拟纳入医保支付范围，仍须符合条件。"})
        assert found["basis"] == basis
        assert "拟" in found["excerpt"] and "符合条件" in found["excerpt"]
