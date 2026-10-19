import json

import pytest

from project_watch.dsh_executor import parse_proposal_stream
from project_watch.executors import ExecutorUnavailable


def test_child_has_no_model_keys_or_custom_dsh_settings(monkeypatch, tmp_path):
    from project_watch.dsh_executor import _child_environment
    from project_watch.model_provider import ModelProviderConfig

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "never-inherit")
    monkeypatch.setenv("PROJECT_WATCH_MODEL_API_KEY", "never-inherit")
    monkeypatch.setenv("DSH_SYSTEM_PROMPT", "never-inherit")
    env = _child_environment(
        ModelProviderConfig("https://example.test", "anthropic-messages", "test"),
        tmp_path,
        "local-only",
    )
    assert "never-inherit" not in env.values()
    assert env["PROJECT_WATCH_MODEL_PROXY_TOKEN"] == "local-only"


def test_model_quota_and_busy_fail_before_run(monkeypatch):
    from project_watch.dsh_executor import DshProposalModel
    from project_watch.executors import EvidencePacket
    from project_watch.model_provider import ModelProviderConfig

    model = DshProposalModel(
        ModelProviderConfig("https://example.test", "anthropic-messages", "test"),
        "secret",
        max_runs=1,
    )
    monkeypatch.setattr(model, "_run", lambda evidence: {})
    evidence = EvidencePacket("p", "s", "hash", "line 1", "synthetic")
    model._lock.acquire()
    with pytest.raises(ExecutorUnavailable, match="busy"):
        model.propose(evidence)
    model._lock.release()
    model.propose(evidence)
    with pytest.raises(ExecutorUnavailable, match="quota"):
        model.propose(evidence)
    assert "secret" not in json.dumps(model.disclosure())


def test_complete_json_only_no_reasoning():
    stream = b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"PRIVATE"}}\n'
    stream += b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"subject\\":\\"test\\"}"}}\n'
    stream += b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n'
    assert parse_proposal_stream(stream, "anthropic-messages") == {"subject": "test"}


@pytest.mark.parametrize("reason", ["max_tokens", "tool_use", None])
def test_incomplete_output_rejected(reason):
    stream = (
        "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": reason}})
    ).encode()
    with pytest.raises(ExecutorUnavailable):
        parse_proposal_stream(stream, "anthropic-messages")


def test_openai_stream_and_bad_json():
    payload = {"choices": [{"delta": {"content": '{"subject":"test"}'}, "finish_reason": "stop"}]}
    assert parse_proposal_stream(
        ("data: " + json.dumps(payload)).encode(), "openai-completions"
    ) == {"subject": "test"}
    payload["choices"][0]["delta"]["content"] = "not JSON"
    with pytest.raises(ExecutorUnavailable):
        parse_proposal_stream(("data: " + json.dumps(payload)).encode(), "openai-completions")


def test_company_compiler_has_bounded_separate_prompt_and_budget(monkeypatch):
    from project_watch.dsh_executor import DshProposalModel
    from project_watch.model_provider import ModelProviderConfig
    model = DshProposalModel(ModelProviderConfig("https://example.test", "anthropic-messages", "test"), "secret", max_runs=1)
    calls = []
    monkeypatch.setattr(model, "_run", lambda evidence, **kwargs: calls.append((evidence, kwargs)) or {"items": []})
    packet = {"fragments": [{"text": "Only synthetic document content", "ref_id": "1:0"}]}
    def gate(body):
        return None
    assert model.compile_reading(packet, before_send=gate) == {"items": []}
    assert calls[0][0] is None
    assert calls[0][1]["before_send"] is gate
    assert calls[0][1]["max_output_tokens"] == 3072
    assert "Only synthetic document content" in calls[0][1]["prompt_override"]
    assert "untrusted data" in calls[0][1]["prompt_override"]
    with pytest.raises(ExecutorUnavailable, match="quota"):
        model.compile_reading(packet, before_send=gate)
    with pytest.raises(ExecutorUnavailable, match="limit"):
        model.compile_reading({"fragments": [{"text": "x" * 9001}]}, before_send=gate)


@pytest.mark.parametrize('selection', [['e1'], ['e999'], [{'ref_id': '1:0', 'quote': '职务标题'}]])
def test_company_compiler_resolves_ids_against_frozen_original(monkeypatch, selection):
    from project_watch.dsh_executor import DshProposalModel
    from project_watch.model_provider import ModelProviderConfig
    model = DshProposalModel(ModelProviderConfig('https://example.test', 'anthropic-messages', 'test'), 'secret', max_runs=1)
    packet = {'fragments': [{'ref_id': '1:0', 'text': '不能依据职务名称推断人员已全职到岗。'}]}

    def response(*args, **kwargs):
        assert '不能依据职务名称' in kwargs['prompt_override']
        packet['fragments'][0]['text'] = '尝试在调用期间替换材料。'
        return {'items': [{'topic': 'team', 'headline': '任职情况待核实', 'text': '不能仅凭职务判断全职。',
                           'evidence': selection, 'as_of': None}]}

    monkeypatch.setattr(model, '_run', response)
    if selection == ['e1']:
        result = model.compile_reading(packet, before_send=lambda _: None)
        assert result['items'][0]['evidence'][0]['quote'] == '不能依据职务名称推断人员已全职到岗。'
    else:
        with pytest.raises(ExecutorUnavailable, match='reading failed'):
            model.compile_reading(packet, before_send=lambda _: None)
    assert model._runs == 1  # no repair/retry call
def test_runtime_prefers_pinned_cache_without_disabling_first_install():
    from project_watch.dsh_executor import DSH_PACKAGE, _runtime_command

    command = _runtime_command('/trusted/npx', '/temporary/model.patch.yml')
    assert command == ['/trusted/npx', '--prefer-offline', '-y', DSH_PACKAGE,
                       '--profile', 'sdk-minimal', '--patch', '/temporary/model.patch.yml']
    assert DSH_PACKAGE == '@deepseek-ai/dsh@0.1.5-rc.3'
