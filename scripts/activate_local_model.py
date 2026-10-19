"""Operator-only WSL helper: receive config via stdin, restart, then forget manager secrets.

Run as the existing private-pilot service user. Never prints configuration or keys.
This is not a web endpoint and does not enable scheduled model calls.
"""

import json
import os
import subprocess
import sys

from project_watch.model_provider import ModelProviderConfig

NAMES = {
    "PROJECT_WATCH_ENABLE_MODEL",
    "PROJECT_WATCH_MODEL_BASE_URL",
    "PROJECT_WATCH_MODEL_PROTOCOL",
    "PROJECT_WATCH_MODEL_ID",
    "PROJECT_WATCH_MODEL_API_KEY",
    "PROJECT_WATCH_MODEL_THINKING",
    "PROJECT_WATCH_MODEL_MAX_RUNS",
    "PROJECT_WATCH_SETTINGS_TOKEN",
    "PATH",
    "PROJECT_WATCH_HOST_PYTHON",
}


def main():
    config = json.loads(sys.stdin.read(16384))
    if (
        not isinstance(config, dict)
        or not (NAMES - {"PROJECT_WATCH_SETTINGS_TOKEN", "PROJECT_WATCH_HOST_PYTHON"}) <= set(config) <= NAMES
        or any(not isinstance(v, str) for v in config.values())
    ):
        raise SystemExit("Invalid activation configuration")
    provider = ModelProviderConfig.from_environment(config)
    provider.resolve_key(config)
    if "PROJECT_WATCH_HOST_PYTHON" in config:
        from project_watch.host_network import validate_host_python
        validate_host_python(config["PROJECT_WATCH_HOST_PYTHON"])
    if (
        config["PROJECT_WATCH_ENABLE_MODEL"] != "1"
        or not 1 <= int(config["PROJECT_WATCH_MODEL_MAX_RUNS"]) <= 100
    ):
        raise SystemExit("Explicit bounded opt-in required")
    if (
        "PROJECT_WATCH_SETTINGS_TOKEN" in config
        and len(config["PROJECT_WATCH_SETTINGS_TOKEN"]) < 32
    ):
        raise SystemExit("A strong settings token is required")
    env = {**os.environ, **config}
    try:
        subprocess.run(
            ["systemctl", "--user", "import-environment", *sorted(config)],
            env=env,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "restart", "project-watch"],
            check=True,
            capture_output=True,
            timeout=170,
        )
    finally:
        subprocess.run(
            ["systemctl", "--user", "unset-environment", *sorted(NAMES)],
            capture_output=True,
            check=False,
        )
    print("Local model activated for current service process; credentials were not saved to disk.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Activation failed; configuration details were suppressed.") from None
