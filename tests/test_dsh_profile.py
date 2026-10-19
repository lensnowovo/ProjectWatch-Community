"""DSH config generation must not weaken the scoped MCP boundary."""

from pathlib import Path

import pytest

from project_watch.dsh_profile import TOKEN_ENV, main, render_dsh_patch


def test_patch_uses_scoped_http_and_disables_shell_and_log_upload() -> None:
    patch = render_dsh_patch("https://watch.example.org/mcp")
    assert "name: '@deepseek-ai/dsh-mcp-client'" in patch
    assert "serverName: project_watch" in patch
    assert 'url: "https://watch.example.org/mcp"' in patch
    assert f"process.env.{TOKEN_ENV}" in patch
    assert "- id: persistent-bash\n  disabled: true" in patch
    assert "- id: persistent-pwsh\n  disabled: true" in patch
    assert "- id: session-log-deepseek\n  config:\n    enabled: false" in patch
    assert "failOnStartupError: true" in patch
    assert "PROJECT_WATCH_DATABASE_URL" not in patch


@pytest.mark.parametrize(
    "url",
    [
        "http://watch.example.org/mcp",
        "https://user:password@watch.example.org/mcp",
        "https://watch.example.org/other",
        "https://watch.example.org/mcp?token=secret",
        "https://watch.example.org/mcp#fragment",
        "https://watch.example.org/mcp\n-insert: malicious",
    ],
)
def test_patch_rejects_unsafe_endpoint(url: str) -> None:
    with pytest.raises(ValueError):
        render_dsh_patch(url)


def test_cli_writes_once_without_embedding_credential(tmp_path: Path, capsys) -> None:
    target = tmp_path / "watch.patch.yml"
    assert main(["https://watch.example.org/mcp", "--output", str(target)]) == 0
    assert target.exists()
    assert "Bearer ${process.env." in target.read_text(encoding="utf-8")
    assert "secret" not in target.read_text(encoding="utf-8")
    assert "Bearer " not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["https://watch.example.org/mcp", "--output", str(target)])
