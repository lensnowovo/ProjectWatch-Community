"""Opt-in isolated live public bootstrap. Receipts/config stay outside source control.

This command creates ONE project, runs its first external job and exits.
It never starts a background worker or retries failed/ambiguous requests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path


def fingerprint(settings_file):
    """Read-only snapshot, never initialize the observed live database."""
    path = Path(settings_file)
    db_path = path.parent / "projectwatch.sqlite3"
    result = {"settings_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if db_path.exists():
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute("SELECT project_id,status FROM studio_projects ORDER BY project_id").fetchall()
            result["pause_sha256"] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--settings-file", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--confirmed-legal-name", required=True)
    parser.add_argument("--live", action="store_true", help="explicitly use configured model/search quota")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    destination = args.data_dir.resolve()
    if not args.live:
        parser.error("--live is required; use pytest for synthetic replay")
    if destination.is_relative_to(repo_root) or destination == args.settings_file.resolve().parent:
        parser.error("data directory must be isolated and outside the source repository")
    if destination.exists() and any(destination.iterdir()):
        parser.error("new empty data directory required; do not bypass unknown outcomes by rerunning")
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.dsh_runtime import DSH_VERSION, runtime_entry
    from project_watch.studio.settings import (
        CommercialConfig,
        NotifyConfig,
        RegistryConfig,
        SettingsStore,
    )

    runtime_entry(args.runtime)
    original = fingerprint(args.settings_file)
    settings = SettingsStore(args.settings_file).load()
    settings.commercial = CommercialConfig()
    settings.registry = RegistryConfig()
    settings.notify = NotifyConfig()
    destination.mkdir(parents=True, exist_ok=True)
    # Save only to the new private directory. The original settings are never written.
    from dataclasses import asdict
    (destination / "settings.json").write_text(json.dumps(asdict(settings)), encoding="utf-8")
    os.environ["PROJECT_WATCH_DSH_RUNTIME"] = str(args.runtime.resolve())
    app = create_studio_app(destination, start_worker=False)
    pipeline = app.state.studio_pipeline
    started = datetime.now(UTC).isoformat()
    with TestClient(app) as client:
        created = client.post("/api/studio/projects", json={
            "entity": {"legal_name": args.confirmed_legal_name, "short_name": args.name},
            "consent": True, "watchpoints": []})
        if created.status_code != 201:
            raise RuntimeError(f"isolated creation failed: HTTP {created.status_code}")
        project_id = created.json()["id"]
        job = pipeline.store.claim()
        if not job or job["kind"] != "external":
            raise RuntimeError("initial external job not queued")
        error = None
        try:
            pipeline.run(job)
        except Exception as exc:
            error = getattr(exc, "code", type(exc).__name__)
        pipeline.store.finish(job["id"], error=error or "", lease_token=job["lease_token"])
        with pipeline.store._connect() as db:
            calls = [dict(row) for row in db.execute(
                "SELECT kind,status,response_status,input_tokens,output_tokens,elapsed_ms FROM studio_job_calls WHERE job_id=?",
                (job["id"],))]
            runtime_row = db.execute("SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage='dsh_runtime'",
                                     (job["id"],)).fetchone()
        usage = {kind: {"http_requests": sum(c["kind"] == kind for c in calls),
                       "unknown": sum(c["kind"] == kind and c["status"] != "completed" for c in calls)}
                 for kind in sorted({c["kind"] for c in calls})}
        model_calls = [c for c in calls if c["kind"] == "model"]
        token_usage = {"input_tokens_known": sum(c["input_tokens"] or 0 for c in model_calls),
                       "output_tokens_known": sum(c["output_tokens"] or 0 for c in model_calls),
                       "calls_missing_usage": sum(c["input_tokens"] is None or c["output_tokens"] is None for c in model_calls)}
        after = fingerprint(args.settings_file)
        result = {"started": started, "ended": datetime.now(UTC).isoformat(), "company": args.name,
                  "identity": "previously user-confirmed legal entity; bootstrap starts after confirmation",
                  "project_id": project_id, "job_id": job["id"], "error": error, "dsh_version": DSH_VERSION,
                  "usage": usage, "tokens": token_usage, "commercial_calls": 0, "internal_material_inputs": 0,
                  "source_settings_and_pause_unchanged": original == after, "before": original, "after": after,
                  "runtime": json.loads(runtime_row[0]) if runtime_row else None,
                  "profile": pipeline.store.get_public_profile(project_id),
                  "plan": pipeline.store.get_plan(project_id),
                  "items": pipeline.store.list_items(project_id=project_id),
                  "monitoring_scope": pipeline.monitoring_plan.items(project_id)}
        (destination / "result.private.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: result[k] for k in ("error", "usage", "tokens", "source_settings_and_pause_unchanged")},
                         ensure_ascii=False))
    return 1 if error else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    raise SystemExit(main())
