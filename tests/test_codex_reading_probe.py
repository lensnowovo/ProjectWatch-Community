"""Offline checks for the bounded, synthetic-only CLI probe."""
import json
from types import SimpleNamespace

from scripts import codex_reading_probe as probe


def test_preview_never_launches_codex(monkeypatch, capsys):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError()))
    assert probe.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["corpus"] == "built_in_synthetic_only"
    assert result["production_adapter"] is False
    assert result["max_output_tokens_per_request"] is None


def test_command_is_readonly_ephemeral_and_has_no_shell(tmp_path):
    args = probe.command("codex.exe", tmp_path, "test-model")
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in args and "--ignore-user-config" in args
    assert "shell_tool" in args and "plugins" in args and "multi_agent" in args
    assert args[-1] == "-"
    schema = probe.strict_schema()
    assert "as_of" in schema["$defs"]["SelectedReadingItem"]["required"]


def test_cli_failure_suppresses_provider_body(monkeypatch):
    monkeypatch.setattr(probe, "reading_prompt", lambda _: "synthetic")
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout='{"type":"turn.failed"}',
                                        stderr="ACCOUNT-SECRET-do-not-print"))
    result = probe.compile_packet("codex.exe", {}, "test-model")
    assert result["passed"] is False
    assert "ACCOUNT-SECRET" not in json.dumps(result)
    assert result["event_types"] == ["turn.failed"]
