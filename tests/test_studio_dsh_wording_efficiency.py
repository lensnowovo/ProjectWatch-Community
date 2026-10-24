"""Synthetic regression tests; no model/search network or production state."""
import json

import pytest

from project_watch.studio.dsh_bootstrap import RecordValidationError, validate_record
from project_watch.studio.dsh_runtime import compact_context_messages


def test_quote_feedback_points_to_contiguous_supported_window():
    quote = "合成项目收费范围已明确。"
    source = {"id": "S1", "published_at": "2026-10-21",
              "excerpt": "标题。\n2025年8月26日发布。\n" + quote + "\n无关尾注。"}
    with pytest.raises(RecordValidationError) as exc:
        validate_record(source, quote, "2025年8月26日发布合成项目收费范围。", "2025年项目")
    feedback = exc.value.feedback()
    candidate = feedback["suggested_quote"]
    assert quote in candidate and candidate in source["excerpt"] and len(candidate) <= 1500
    assert "2025年8月26日" in candidate
    validate_record(source, candidate, "2025年8月26日发布合成项目收费范围。", "2025年项目")


def test_quote_feedback_does_not_turn_provider_date_into_event_date():
    source = {"id": "S1", "published_at": "2026-10-21", "excerpt": "合成项目收费范围已明确。"}
    with pytest.raises(RecordValidationError) as exc:
        validate_record(source, source["excerpt"], "2026年10月21日合成项目获批。", "获批")
    assert "suggested_quote" not in exc.value.feedback()
    assert exc.value.feedback()["next_action"] == "remove_unsupported_or_split"


def test_quote_feedback_does_not_join_distant_or_fuzzy_fragments():
    quote = "合成项目收费范围已明确。"
    source = {"id": "S1", "published_at": "", "excerpt": "2025年8月26日发布。" + "背景。" * 600 + quote}
    with pytest.raises(RecordValidationError) as exc:
        validate_record(source, quote, "2025年8月26日合成项目发布。", "项目")
    assert "suggested_quote" not in exc.value.feedback()
    assert exc.value.feedback()["next_action"] == "remove_unsupported_or_split"


def turn(identifier, name, data):
    return [{"role": "assistant", "tool_calls": [{"id": identifier, "function": {"name": name}}]},
            {"role": "tool", "tool_call_id": identifier, "content": json.dumps(data, ensure_ascii=False)}]


def test_search_read_context_excerpt_sharing_is_lossless_and_prefix_stable():
    body = "2025年合成项目意向订单不等于确认收入。" * 80
    source = {"id": "S1", "url": "https://example.com/one", "excerpt": body, "read_scope": "summary"}
    messages = turn("s", "pw_search", {"sources": [source], "untrusted_data": True})
    original_prefix = compact_context_messages(messages)
    messages += turn("r", "pw_read", {**source, "read_scope": "partial", "window_offset": 0})
    fragment = body[20:600]
    messages += turn("c", "pw_context", {"fingerprint": "a", "numbered": {
        "N1": {"url": source["url"], "context": fragment, "read_scope": "search_summary"},
        "N2": {"url": "https://example.com/other", "context": body}}})
    original = json.dumps(messages)
    compact = compact_context_messages(messages)
    assert json.dumps(messages) == original
    assert compact[:2] == original_prefix
    assert len(json.dumps(compact)) < len(original) * .75
    resolved = {}
    for raw, actual in zip(messages, compact, strict=True):
        if raw["role"] != "tool":
            assert raw == actual
            continue
        data = json.loads(actual["content"])
        def restore(node):
            if isinstance(node, dict):
                for field in ("context", "excerpt"):
                    if field + "_same_as" in node:
                        pointer = node.pop(field + "_same_as")
                        target = resolved[pointer["tool_call_id"]]
                        for part in pointer["path"]:
                            target = target[part]
                        node[field] = target[pointer["start"]:pointer["end"]]
                for value in node.values():
                    restore(value)
            elif isinstance(node, list):
                for value in node:
                    restore(value)
        restore(data)
        assert data == json.loads(raw["content"])
        resolved[raw["tool_call_id"]] = data
    assert json.loads(compact[-1]["content"])["numbered"]["N2"]["context"] == body
