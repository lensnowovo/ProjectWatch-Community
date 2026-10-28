"""Copy frozen public-only receipts into an isolated monitor probe; never a worker.

The caller must supply a deliberately prepared public-only seed, not a desktop DB.
No automatic retries, no changes to the source DB or original settings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-db", required=True, type=Path)
    parser.add_argument("--public-only-seed", required=True, action="store_true")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--settings-file", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--analysis-only", action="store_true", help="no search/read tools; existing public evidence only")
    args = parser.parse_args()
    destination = args.data_dir.resolve()
    root = Path(__file__).resolve().parents[1]
    if destination.is_relative_to(root) or (destination.exists() and any(destination.iterdir())):
        parser.error("use a new empty data directory outside source control")
    if args.live and (not args.settings_file or not args.runtime):
        parser.error("--live needs settings and pinned runtime")
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.dsh_monitor import PublicMonitor
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.settings import (
        CommercialConfig,
        NotifyConfig,
        RegistryConfig,
        SettingsStore,
    )
    from scripts.dsh_bootstrap_probe import fingerprint

    before = fingerprint(args.settings_file) if args.settings_file else None
    source_hash = hashlib.sha256(args.seed_db.read_bytes()).hexdigest()
    with sqlite3.connect(args.seed_db.resolve().as_uri() + "?mode=ro", uri=True) as source:
        # Refuse internal originals and unresolved calls in the seed.
        if source.execute("SELECT count(*) FROM sources").fetchone()[0]:
            raise ValueError("seed contains documents; public-only seed required")
        if source.execute("SELECT count(*) FROM studio_job_calls WHERE status IN ('started','unknown')").fetchone()[0]:
            raise ValueError("seed contains unknown calls; do not bypass recovery")
        destination.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination / "projectwatch.sqlite3") as target:
            source.backup(target)
    if args.live:
        config = SettingsStore(args.settings_file).load()
        config.commercial, config.registry, config.notify = CommercialConfig(), RegistryConfig(), NotifyConfig()
        (destination / "settings.json").write_text(json.dumps(asdict(config)), encoding="utf-8")
        os.environ["PROJECT_WATCH_DSH_RUNTIME"] = str(args.runtime)
        os.environ["PROJECT_WATCH_DSH_MONITOR"] = "1"
    app = create_studio_app(destination, start_worker=False)
    pipeline = app.state.studio_pipeline
    with pipeline.store._connect() as db:
        ids = [r[0] for r in db.execute("SELECT project_id FROM studio_projects")]
        if len(ids) != 1 or db.execute("SELECT 1 FROM studio_jobs WHERE status IN ('queued','running')").fetchone():
            raise ValueError("seed must have one settled public project")
    pid = ids[0]
    started = datetime.now(UTC).isoformat()
    pipeline.store.enqueue(pid, "insight" if args.analysis_only else "external")
    job = pipeline.store.claim()
    error = None
    if args.live:
        try:
            pipeline.run(job)
        except Exception as exc:
            error = getattr(exc, "code", type(exc).__name__)
    else:
        class Denied:
            def web_search(self, *a, **kw):
                raise AssertionError("offline probe cannot search")
        loop = PublicMonitor(pipeline, JobExecution(pipeline.store, job), Denied(), None, analysis_only=args.analysis_only)
        ctx = loop.tool("pw_context", {})
        assert ctx["numbered"] and ctx["watches"]
        assert not any(e["kind"] != "public" for e in ctx["numbered"].values())
        (destination / "context.private.json").write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
    pipeline.store.finish(job["id"], error=error or "", lease_token=job["lease_token"])
    with pipeline.store._connect() as db:
        calls = [dict(r) for r in db.execute(
            "SELECT kind,status,response_status,input_tokens,output_tokens FROM studio_job_calls WHERE job_id=?", (job["id"],))]
        checkpoints = {r[0]: json.loads(r[1]) for r in db.execute(
            "SELECT stage,data FROM studio_job_checkpoints WHERE job_id=?", (job["id"],))
            if r[0] in ("dsh_runtime", "dsh_state", "dsh_hold")}
    models = [c for c in calls if c["kind"] == "model"]
    result = {"mode": "live" if args.live else "offline-context-only", "analysis_only": args.analysis_only, "started": started,
        "ended": datetime.now(UTC).isoformat(), "error": error, "project_id": pid, "job_id": job["id"],
        "calls": {k: sum(c["kind"] == k for c in calls) for k in sorted({c["kind"] for c in calls})},
        "unknown": sum(c["status"] in ("started", "unknown") for c in calls),
        "tokens": {"input_known": sum(c["input_tokens"] or 0 for c in models),
            "output_known": sum(c["output_tokens"] or 0 for c in models),
            "missing_usage": sum(c["input_tokens"] is None or c["output_tokens"] is None for c in models)},
        "original_settings_pause_unchanged": before == (fingerprint(args.settings_file) if args.settings_file else None),
        "seed_unchanged": source_hash == hashlib.sha256(args.seed_db.read_bytes()).hexdigest(),
        "checkpoints": checkpoints}
    (destination / "result.private.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "checkpoints"}, ensure_ascii=False))
    return int(error is not None)


if __name__ == "__main__":
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "src")]
    raise SystemExit(main())
