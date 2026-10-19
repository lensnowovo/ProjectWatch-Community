"""Synthetic-only Codex CLI reading probe; NOT a production executor.

Uses the same Office corpus, parser, prompt and evidence validator as the DSH
probe. Two explicit CLI turns at most, no application retry, 180s per turn.
CLI token usage is observed, not a hard enforced output-token budget.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.company_state import (
    SelectedReadingOutput,
    input_packets,
    reading_prompt,
    resolve_reading_selection,
    validate_reading,
)
from project_watch.repository import ProjectRepository


def corpus():
    spec = importlib.util.spec_from_file_location(
        "synthetic_corpus", Path(__file__).with_name("company_reading_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def strict_schema():
    schema = SelectedReadingOutput.model_json_schema()
    # Responses strict schemas require nullable properties to be required too.
    for value in [schema, *schema.get("$defs", {}).values()]:
        if value.get("type") == "object":
            value["required"] = list(value["properties"])
    return schema


def command(executable, root, model):
    args = [str(executable), "exec", "--ignore-user-config", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only", "--model", model,
            "-c", 'model_reasoning_effort="medium"',
            "-c", 'web_search="disabled"',
            "--output-schema", str(root / "schema.json"),
            "--output-last-message", str(root / "answer.json"), "--json"]
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks",
                    "multi_agent", "browser_use", "computer_use", "memories"):
        args.extend(["--disable", feature])
    return [*args, "-"]


def compile_packet(executable, packet, model):
    with tempfile.TemporaryDirectory(prefix="pw-codex-synthetic-") as directory:
        root = Path(directory)
        (root / "schema.json").write_text(json.dumps(strict_schema()), encoding="utf-8")
        started = time.monotonic()
        # Retain local account discovery and networking, not inherited API keys,
        # provider overrides, project secrets or parent task/thread metadata.
        allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
                   "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "CODEX_HOME",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        run = subprocess.run(command(executable, root, model),
                             input=reading_prompt(packet), cwd=root,
                             capture_output=True, text=True, encoding="utf-8",
                             timeout=180, check=False, env=env)
        events = []
        for line in run.stdout.splitlines():
            with contextlib.suppress(ValueError):
                events.append(json.loads(line))
        if run.returncode or not (root / "answer.json").exists():
            # No raw stderr/provider messages: they may contain account details.
            return {"passed": False, "error": "codex_execution_failed",
                    "exit_code": run.returncode,
                    "event_types": sorted({e.get("type", "unknown") for e in events})}
        output = json.loads((root / "answer.json").read_text(encoding="utf-8"))
        resolved = resolve_reading_selection(packet, output)
        validated = validate_reading(packet, resolved)
        usages = [e.get("usage", {}) for e in events if e.get("type") == "turn.completed"]
        usage = {k: v for record in usages for k, v in record.items()
                 if k in {"input_tokens", "cached_input_tokens", "output_tokens"}
                 and type(v) is int and v >= 0}
        tools = [e.get("item", {}).get("type") for e in events
                 if e.get("type") == "item.completed"
                 and e.get("item", {}).get("type") not in {"agent_message", "reasoning"}]
        return {"passed": bool(validated["items"]) and not tools,
                "seconds": round(time.monotonic() - started, 2), "usage": usage,
                "tool_item_types": tools, "reading": validated}


def exercise(executable, model):
    fixture = corpus()
    result = {**fixture.manifest("office"), "executor": "Codex CLI",
              "mode": "compiler_probe", "model_requested": model,
              "max_output_tokens_per_request": None,
              "max_requests": None,
              "max_cli_turns": 2, "cli_transport_retries": "runtime_managed",
              "production_adapter": False, "turns": [], "passed": False}
    with tempfile.TemporaryDirectory(prefix="pw-codex-intake-") as directory:
        db = Path(directory) / "synthetic.sqlite3"
        app = create_app(db, start_worker=False)
        repo = ProjectRepository(db)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            project = client.post("/api/projects", json={"name": "Synthetic Codex acceptance"}).json()
            for stage, (name, data, mime) in zip(("baseline", "update"), fixture.document_payloads("office"), strict=True):
                response = client.post(f'/api/projects/{project["id"]}/files',
                                       files={"file": (name, data, mime)})
                if response.status_code != 202:
                    raise ValueError("synthetic upload failed")
                job = app.state.intake_worker.process_one()
                if job is None or job.status != "completed":
                    raise ValueError("synthetic parser failed")
                source = next(s for s in repo.list_sources(project["id"])
                              if s.content_sha256 == hashlib.sha256(data).hexdigest())
                packets = input_packets(repo, source)
                if len(packets) != 1:
                    raise ValueError("unexpected synthetic packet count")
                turn = compile_packet(executable, packets[0], model)
                result["turns"].append({"stage": stage, **turn})
                if not turn["passed"]:
                    return result
    result["passed"] = True
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--codex-executable", type=Path)
    parser.add_argument("--model", default="gpt-6-astra")
    args = parser.parse_args(argv)
    if not args.run:
        result = {**corpus().manifest("office"), "executor": "Codex CLI",
                  "max_output_tokens_per_request": None, "production_adapter": False}
    elif not args.codex_executable or not args.codex_executable.is_file():
        result = {"passed": False, "error": "explicit_codex_executable_required"}
    else:
        try:
            result = exercise(args.codex_executable, args.model)
        except Exception as exc:
            result = {"passed": False, "error": type(exc).__name__, "details_suppressed": True}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 2 if result.get("passed") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
