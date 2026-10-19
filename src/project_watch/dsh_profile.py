"""Render a secret-free DSH overlay for one Project Watch HTTPS MCP endpoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

TOKEN_ENV = "PROJECT_WATCH_EXECUTOR_TOKEN"


def render_dsh_patch(resource_url: str) -> str:
    """Return an SDK-minimal patch; caller supplies the token through the environment.

    The overlay removes the model-facing persistent shells. It does not provide
    an OS isolation boundary; real institutional material still needs one.
    """
    url = urlsplit(resource_url)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path != "/mcp"
        or url.query
        or url.fragment
        or any(character.isspace() for character in resource_url)
    ):
        raise ValueError("DSH endpoint must be a credential-free HTTPS URL ending in /mcp")
    safe_url = json.dumps(resource_url, ensure_ascii=True)
    return (
        "# Project Watch SDK-minimal overlay. No token is stored in this file.\n"
        "# Apply only to an isolated DSH home; test with synthetic data first.\n"
        "- id: persistent-bash\n"
        "  disabled: true\n"
        "- id: persistent-pwsh\n"
        "  disabled: true\n"
        "- id: session-log-deepseek\n"
        "  config:\n"
        "    enabled: false\n"
        "- insert:\n"
        "    - id: mcp-project-watch\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: project_watch\n"
        "        transport: streamable-http\n"
        f"        url: {safe_url}\n"
        "        headers:\n"
        f"          Authorization: !!js '`Bearer ${{process.env.{TOKEN_ENV}}}`'\n"
        "        failOnStartupError: true\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resource_url", help="HTTPS project-bound endpoint ending in /mcp")
    parser.add_argument("--output", type=Path, required=True, help="new patch file path")
    arguments = parser.parse_args(argv)
    try:
        patch = render_dsh_patch(arguments.resource_url)
        with arguments.output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(patch)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Wrote secret-free DSH overlay: {arguments.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
