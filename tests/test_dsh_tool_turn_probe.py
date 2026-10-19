"""A scripted provider must never silently accept unexpected DSH tool scope."""

import pytest

from scripts.dsh_tool_turn_probe import ScriptedProvider


def test_scripted_provider_requires_bounded_tools_and_stops_after_three_stages() -> None:
    provider = ScriptedProvider("source-1", "a" * 64)
    with pytest.raises(ValueError, match="expected bounded MCP tool"):
        provider.response({"tools": [], "stream": True})

    # Failed request is still consumed: repeating a broken protocol step cannot
    # silently progress to a later scripted tool call.
    assert len(provider.stages) == 1

    provider = ScriptedProvider("source-1", "a" * 64)
    tools = [
        {"function": {"name": name}}
        for name in (
            "mcp__project_watch__source_excerpts",
            "mcp__project_watch__propose_candidate",
        )
    ]
    first, streamed = provider.response({"tools": tools, "stream": True})
    assert streamed
    assert first["chunks"][0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == (
        "mcp__project_watch__source_excerpts"
    )
    second, _ = provider.response({"tools": tools, "stream": True})
    assert second["chunks"][1]["choices"][0]["finish_reason"] == "tool_calls"
    third, _ = provider.response({"tools": tools, "stream": True})
    assert third["chunks"][1]["choices"][0]["finish_reason"] == "stop"
    with pytest.raises(ValueError, match="more than three"):
        provider.response({"tools": tools, "stream": True})
