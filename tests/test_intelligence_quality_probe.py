import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("quality_probe", Path(__file__).resolve().parents[1] / "scripts/intelligence_quality_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def case():
    return dict(id="c01", profile="connector", title="合成公司拟扩产", relevance="DO_NOT_SEND_LABEL",
                boundary="DO_NOT_SEND_REVIEW", read_first=True)


def test_expected_labels_not_in_model_evidence():
    packet = probe.packet_for("合成公司：生产汽车连接器。", case())
    serialized = str(packet)
    assert "DO_NOT_SEND" not in serialized
    assert packet["source_url"].startswith("https://example.org/synthetic/")


def test_failed_call_retained_and_not_retried():
    class Model:
        calls = 0

        def analyze_external(self, packet, before_send):
            self.calls += 1
            before_send(b"synthetic request")
            raise TimeoutError("private exception not logged")

    model, rows = Model(), []
    result = probe.evaluate({"profiles": {"connector": "合成公司：生产汽车连接器。"}}, [case()], model, rows.append)
    assert model.calls == 1 and len(rows) == 1
    assert rows[0]["status"] == "failed" and rows[0]["dispatched"] == 1
    assert "private exception" not in str(rows)
    assert result["independent_reviewer_gate"] == "not_evaluated"


def test_duplicate_send_gate_fails_closed():
    class Model:
        def analyze_external(self, packet, before_send):
            before_send(b"synthetic request")
            before_send(b"synthetic request")

    rows = []
    probe.evaluate({"profiles": {"connector": "合成公司：生产汽车连接器。"}}, [case()], Model(), rows.append)
    assert rows[0]["status"] == "failed" and rows[0]["dispatched"] == 1


def test_compact_probe_uses_same_executor_gates_without_retry(tmp_path, monkeypatch):
    from test_external_analysis import inputs
    from test_external_analysis_compact import compact_output

    from project_watch.model_provider import ModelProviderConfig
    _, _, _, packet = inputs(tmp_path)
    config = ModelProviderConfig(base_url='https://example.org', protocol='anthropic-messages', model='synthetic')
    model = probe.CompactProbeModel(config, 'synthetic-key', max_runs=2)
    calls, sends = [], []

    def run(evidence, **kw):
        calls.append(kw)
        kw['before_send'](b'synthetic')
        return compact_output(packet)

    monkeypatch.setattr(model, '_run', run)
    result = model.analyze_external(packet, before_send=sends.append)
    assert result['review_status'] == 'unreviewed'
    assert len(calls) == len(sends) == 1
    assert calls[0]['max_output_tokens'] == 3072
    assert 'internal_evidence' not in calls[0]['prompt_override']

    def malformed(evidence, **kw):
        calls.append(kw)
        kw['before_send'](b'synthetic')
        result = compact_output(packet)
        result['implications'][0]['text'] = 'do not repair'
        return result

    monkeypatch.setattr(model, '_run', malformed)
    import pytest

    from project_watch.analysis_failures import AnalysisFailure
    with pytest.raises(AnalysisFailure):
        model.analyze_external(packet, before_send=sends.append)
    assert len(calls) == len(sends) == 2


def test_baseline_is_explicit_and_default_matches_measured_candidate(tmp_path):
    from test_external_analysis import inputs
    from test_external_analysis_compact import compact_output
    from test_external_analysis_selection import selection_output

    from project_watch.dsh_executor import DshProposalModel
    _, _, _, packet = inputs(tmp_path)
    assert DshProposalModel._external_analysis_prompt(packet) == probe.CompactProbeModel._external_analysis_prompt(packet)
    expected = DshProposalModel._resolve_external_analysis(packet, compact_output(packet))
    assert probe.SelectionProbeModel._resolve_external_analysis(packet, selection_output(packet)) == expected
    assert probe.SelectionProbeModel._external_analysis_prompt(packet) != DshProposalModel._external_analysis_prompt(packet)


def test_monitoring_candidate_changes_instruction_not_default_or_evidence(tmp_path):
    from test_external_analysis import inputs
    from test_external_analysis_compact import compact_output

    from project_watch.dsh_executor import DshProposalModel

    _, _, _, packet = inputs(tmp_path)
    baseline = probe.CompactProbeModel._external_analysis_prompt(packet)
    candidate = probe.MonitoringProbeModel._external_analysis_prompt(packet)
    assert 'Monitoring relevance is not verification' in candidate
    assert 'Monitoring relevance is not verification' not in baseline
    assert candidate.split('OUTPUT_SCHEMA:\n')[1] == baseline.split('OUTPUT_SCHEMA:\n')[1]
    assert probe.MonitoringProbeModel._resolve_external_analysis(packet, compact_output(packet)) == (
        DshProposalModel._resolve_external_analysis(packet, compact_output(packet)))
    assert DshProposalModel._external_analysis_prompt(packet) == baseline
