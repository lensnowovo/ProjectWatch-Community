"""Explicit isolated public acceptance. Never run against a desktop data folder.

The DeepSeek key is read once from an environment variable, never printed or
passed to DSH. Only public company identity is sent. No retry or background worker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--settings-file", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--confirmed-legal-name", required=True)
    parser.add_argument("--key-env", default="PW_DEEPSEEK_PROBE_KEY")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.dsh_runtime import runtime_entry
    from project_watch.studio.settings import (
        CommercialConfig,
        NotifyConfig,
        RegistryConfig,
        SettingsStore,
    )
    from scripts.dsh_bootstrap_probe import fingerprint

    destination = args.data_dir.resolve()
    root = Path(__file__).resolve().parents[1]
    if not args.live or destination.is_relative_to(root) or destination == args.settings_file.resolve().parent:
        parser.error("--live and a new private data directory outside the repository are required")
    if destination.exists() and any(destination.iterdir()):
        parser.error("nonempty data directory: do not rerun or bypass uncertain requests")
    runtime_entry(args.runtime)
    key = os.environ.pop(args.key_env, "")
    if not key:
        parser.error("missing search key environment variable")
    original = fingerprint(args.settings_file)
    settings = SettingsStore(args.settings_file).load()
    settings.commercial, settings.registry, settings.notify = CommercialConfig(), RegistryConfig(), NotifyConfig()
    keys = settings.search.credentials()
    keys["deepseek"] = key
    settings.search.keys = keys
    settings.search.providers = ["deepseek"] + [p for p in settings.search.enabled() if p != "deepseek"]
    settings.search.mode = "auto"
    destination.mkdir(parents=True)
    SettingsStore(destination / "settings.json").save(settings)
    os.environ["PROJECT_WATCH_DSH_RUNTIME"] = str(args.runtime.resolve())
    os.environ["PROJECT_WATCH_DSH_MONITOR"] = "1"
    app = create_studio_app(destination, start_worker=False)
    pipeline = app.state.studio_pipeline
    result = {"started": datetime.now(UTC).isoformat(), "company": args.name,
              "jobs": [], "commercial_calls": 0, "internal_material_inputs": 0}
    with TestClient(app) as client:
        created = client.post("/api/studio/projects", json={
            "entity": {"legal_name": args.confirmed_legal_name, "short_name": args.name},
            "consent": True, "watchpoints": []})
        if created.status_code != 201:
            raise RuntimeError(f"creation failed: HTTP {created.status_code}")
        pid = created.json()["id"]
        result["project_id"] = pid
        # Run only the name bootstrap and its automatically queued monitoring child.
        # Claim success is not a completed cycle; checkpoints below determine that.
        for expected in ("external", "external_followup"):
            job = pipeline.store.claim()
            if not job:
                result["uncovered"] = f"{expected} not automatically queued"
                break
            if job["project_id"] != pid or job["kind"] != expected:
                raise RuntimeError("unexpected isolated queue route")
            entry = {"job_id": job["id"], "kind": job["kind"], "started": datetime.now(UTC).isoformat()}
            (destination / "progress.private.json").write_text(json.dumps(entry), encoding="utf-8")
            error = None
            try:
                pipeline.run(job)
            except Exception as exc:
                error = getattr(exc, "code", type(exc).__name__)
            pipeline.store.finish(job["id"], error=error or "", lease_token=job["lease_token"])
            with pipeline.store._connect() as db:
                checkpoints = {r[0]: json.loads(r[1]) for r in db.execute(
                    "SELECT stage,data FROM studio_job_checkpoints WHERE job_id=?", (job["id"],))
                    if r[0] in ("dsh_state", "dsh_runtime", "dsh_hold")}
            entry.update(error=error, ended=datetime.now(UTC).isoformat(), checkpoints=checkpoints)
            result["jobs"].append(entry)
            (destination / f"{expected}.private.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
            if error:
                break
        with pipeline.store._connect() as db:
            calls = [dict(r) for r in db.execute(
                "SELECT kind,provider,status,response_status,input_tokens,output_tokens,elapsed_ms FROM studio_job_calls")]
        models = [c for c in calls if c["kind"] == "model"]
        search_hash = hashlib.sha256(("api.deepseek.comsearch" + key).encode()).hexdigest()
        fetch_hash = hashlib.sha256(("api.deepseek.compage" + key).encode()).hexdigest()
        result.update(
            ended=datetime.now(UTC).isoformat(),
            requests={kind: sum(c["kind"] == kind for c in calls) for kind in sorted({c["kind"] for c in calls})},
            deepseek={"search": sum(c["provider"] == search_hash for c in calls),
                      "fetch": sum(c["provider"] == fetch_hash for c in calls)},
            unknown=sum(c["status"] in ("started", "unknown") for c in calls),
            http_errors=sum((c["response_status"] or 0) >= 400 for c in calls),
            tokens={"input_known": sum(c["input_tokens"] or 0 for c in models),
                    "output_known": sum(c["output_tokens"] or 0 for c in models),
                    "missing_usage": sum(c["input_tokens"] is None or c["output_tokens"] is None for c in models)},
            original_settings_pause_unchanged=original == fingerprint(args.settings_file),
            profile=pipeline.store.get_public_profile(pid), plan=pipeline.store.get_plan(pid),
            scope=pipeline.monitoring_plan.items(pid), events=pipeline.store.list_events(project_id=pid))
        (destination / "result.private.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: result[k] for k in ("requests", "deepseek", "unknown", "http_errors", "tokens",
                                               "original_settings_pause_unchanged")}, ensure_ascii=False))
    return int(any(j["error"] for j in result["jobs"]) or len(result["jobs"]) != 2)


if __name__ == "__main__":
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "src")]
    raise SystemExit(main())
