"""The live acceptance harness is exercised offline; these are not model receipts."""

import io
import json
from contextlib import redirect_stdout

import pytest

from scripts import company_reading_probe as probe


def test_preview_never_reads_credentials_or_starts_model(monkeypatch, capsys):
    monkeypatch.setattr(probe, "load_model", lambda *_: pytest.fail("preview opened model"))
    assert probe.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "preview_no_network"
    assert result["max_requests"] == 2
    assert result["max_output_tokens_per_request"] == 3072
    assert 1500 < result["documents"][0]["chars"] <= 9000


def test_preview_survives_windows_legacy_console_encoding():
    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding="cp1252") as stream, redirect_stdout(stream):
        assert probe.main([]) == 0
        stream.flush()
        result = json.loads(buffer.getvalue().decode("cp1252"))
        assert result["documents"][0]["name"] == probe.DOCUMENTS[0][0]


def test_invalid_stdin_does_not_echo_credentials(capsys):
    assert probe.main(["--run", "--config-stdin"], stream=io.StringIO(
        '{"api_key":"DO-NOT-PRINT-THIS", "unexpected": true}'
    )) == 2
    output = capsys.readouterr().out
    assert "DO-NOT-PRINT-THIS" not in output
    assert json.loads(output)["passed"] is False


def test_synthetic_only_full_lifecycle(monkeypatch):
    from scripts.smoke_company_reading import SyntheticCompiler

    class Compiler(SyntheticCompiler):
        def __init__(self):
            super().__init__()
            self.max_runs = 2
            self.receipt = {}

        def compile_reading(self, packet, *, before_send):
            output = super().compile_reading(packet, before_send=before_send)
            self.receipt = {"requests_admitted": 1, "observed_prompt_tokens": 500,
                            "observed_completion_tokens": 400, "raw_provider_error": "do-not-leak"}
            return output

    monkeypatch.setenv("PROJECT_WATCH_SETTINGS_TOKEN", "existing-operator-secret")
    model = Compiler()
    result = probe.exercise(model)
    assert result["passed"] is True
    assert model.calls == 2
    assert result["semantic_review"] == "required"
    assert result["checks"]["no_dispatch_after_restart"]
    assert result["checks"]["duplicate_not_billed"]
    assert result["checks"]["unused_budget_after_duplicate"]
    assert result["checks"]["human_judgment_preserved"]
    assert len(result["reading"]["batches"]) == 2
    assert result["turns"][0]["usage"]["observed_completion_tokens"] == 400
    assert "do-not-leak" not in json.dumps(result)
    import os
    assert os.environ["PROJECT_WATCH_SETTINGS_TOKEN"] == "existing-operator-secret"


def test_failure_stops_without_retry(monkeypatch):
    from scripts.smoke_company_reading import SyntheticCompiler

    class FailingCompiler(SyntheticCompiler):
        max_runs = 2

        def __init__(self):
            super().__init__()
            self.receipt = {}

        def compile_reading(self, packet, *, before_send):
            super().compile_reading(packet, before_send=before_send)
            raise RuntimeError("provider-body-and-secret-must-not-leak")

    model = FailingCompiler()
    result = probe.exercise(model)
    assert result["passed"] is False
    assert model.calls == 1
    assert "provider-body" not in json.dumps(result)
    assert result["failed_check"] == "baseline_published"


def test_request_budget_cannot_be_widened():
    config = {"base_url": "https://example.test", "protocol": "anthropic-messages",
              "model": "fake", "api_key": "not-a-real-key", "max_runs": 3}
    with pytest.raises(ValueError):
        probe.load_model(io.StringIO(json.dumps(config)))


def test_synthetic_probe_explains_rejected_output_without_retry():
    from scripts.smoke_company_reading import SyntheticCompiler

    class InvalidCompiler(SyntheticCompiler):
        def compile_reading(self, packet, *, before_send):
            output = super().compile_reading(packet, before_send=before_send)
            output['items'][0]['as_of'] = '2099'
            return output

    model = InvalidCompiler()
    result = probe.exercise(model)
    assert result['passed'] is False and model.calls == 1
    diagnosis = result['turns'][0]['validation']
    assert diagnosis['code'] == 'reading period not present in supporting quote'
    assert diagnosis['candidate']['items'][0]['as_of'] == '2099'


def test_schema_diagnostic_does_not_echo_unrecognized_fields():
    diagnosis = probe.diagnose_output({}, {'items': [], 'secret': 'DO-NOT-ECHO'})
    assert diagnosis['code'] == 'schema_invalid'
    assert diagnosis['error_types'] == ['extra_forbidden']
    assert 'DO-NOT-ECHO' not in json.dumps(diagnosis)


def test_office_preview_is_explicit_and_never_opens_model(monkeypatch, capsys):
    monkeypatch.setattr(probe, 'load_model', lambda *_: pytest.fail('preview opened model'))
    assert probe.main(['--format', 'office']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['formats'] == ['pptx', 'docx']
    assert result['max_requests'] == 2
    assert result['documents'][0]['name'].endswith('.pptx')


def test_real_office_containers_follow_same_bounded_lifecycle():
    from scripts.smoke_company_reading import SyntheticCompiler

    model = SyntheticCompiler()
    result = probe.exercise(model, document_format='office')
    assert result['passed'] is True and model.calls == 2
    assert result['checks']['office_locations_0']
    assert result['checks']['office_locations_1']
    assert result['checks']['all_text_preserved_0']
    assert result['checks']['all_text_preserved_1']
    assert result['checks']['duplicate_not_billed']
    assert result['checks']['human_judgment_preserved']
    assert result['formats'] == ['pptx', 'docx']
    assert result['semantic_review'] == 'required'
