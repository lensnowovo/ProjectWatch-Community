"""Neutral source coverage, separate from company events or business silence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from project_watch.studio.store import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_source_health (
    project_id TEXT NOT NULL REFERENCES projects(id), source_kind TEXT NOT NULL,
    version INTEGER NOT NULL, record TEXT NOT NULL, PRIMARY KEY(project_id,source_kind)
);
CREATE TABLE IF NOT EXISTS studio_source_health_history (
    project_id TEXT NOT NULL REFERENCES projects(id), source_kind TEXT NOT NULL,
    version INTEGER NOT NULL, record TEXT NOT NULL, PRIMARY KEY(project_id,source_kind,version)
);
"""
SOURCE_KINDS = {"company", "competitor", "industry", "policy", "paper", "trial", "patent", "registry",
                "filing", "cn_trial", "site", "watch", "thread", "tender", "license", "xiniu", "pharmcube", "wind"}


def silence_coverage(store, project_id, *, base_hours=24, window_days=60):
    """Require continuous saved checks, not backfilled news or a single recent success."""
    current = datetime.now(UTC)
    allowance = timedelta(hours=max(1, base_hours) * 2)
    since = current - timedelta(days=window_days)
    try:
        with closing(store._connect()) as db:
            rows = db.execute("SELECT record FROM studio_source_health_history WHERE project_id=? "
                              "AND source_kind='company' ORDER BY version DESC LIMIT 4096", (project_id,)).fetchall()
    except sqlite3.OperationalError:
        return False  # Legacy data without health history cannot establish silence.
    checks = sorted((json.loads(r[0]) for r in rows), key=lambda r: r['checked_at'])
    if checks and checks[-1]['status'] != 'available':
        return False
    # A first pass is neither a failed complete check nor a complete healthy
    # interval. Until its followup succeeds the latest status above blocks silence.
    checks = [r for r in checks if r.get('collection_phase') != 'first']
    relevant = [r for r in checks if datetime.fromisoformat(r['checked_at']) >= since-allowance]
    if len(relevant) < 3 or any(r['status'] != 'available' for r in relevant):
        return False
    times = [datetime.fromisoformat(r['checked_at']) for r in relevant]
    return bool(times[0] <= since and current-times[-1] <= allowance and times[-1] <= current
                and all(b-a <= allowance for a, b in pairwise(times)))


class HealthStore:
    def __init__(self, store):
        self.store = store
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)

    def record(self, project_id, report):
        if len(report) > 30:
            raise ValueError("health report exceeds source bounds")
        prepared = []
        for key, row in report.items():
            if key not in SOURCE_KINDS:
                continue
            checked, added = row.get("checked", 0), row.get("added", 0)
            if type(checked) is not int or type(added) is not int or not 0 <= checked <= 10000 or not 0 <= added <= 100000:
                raise ValueError("invalid health counts")
            failures = len(row.get("errors") or [])
            state = "partial" if checked and (failures or row.get('deferred')) else "available" if checked else "unavailable" if failures else "not_observed"
            prepared.append((key, {"source_kind": key, "status": state, "successful_checks": checked,
                "collection_phase": "first" if row.get('deferred') else "complete",
                "added": added, "failure_count": failures, "data_scope": "metadata", "checked_at": now()}))
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for key, row in prepared:
                old = db.execute("SELECT version FROM studio_source_health WHERE project_id=? AND source_kind=?", (project_id, key)).fetchone()
                version = old[0] + 1 if old else 1
                row["version"] = version
                values = (project_id, key, version, json.dumps(row))
                db.execute("INSERT INTO studio_source_health VALUES (?,?,?,?) ON CONFLICT(project_id,source_kind) "
                           "DO UPDATE SET version=excluded.version,record=excluded.record", values)
                db.execute("INSERT INTO studio_source_health_history VALUES (?,?,?,?)", values)
                self.store.evidence.dependencies._change(db, project_id, "public", "source_health_checked", {
                    "source_kind": key, "status": row["status"], "version": version})

    def read(self, project_id, *, include_internal=True):
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError("project not found")
        with closing(self.store._connect()) as db:
            sources = [json.loads(r[0]) for r in db.execute("SELECT record FROM studio_source_health WHERE project_id=? ORDER BY source_kind", (project_id,))]
            jobs = [dict(r) for r in db.execute("SELECT id,kind,status,attempts,retry_after FROM studio_jobs WHERE project_id=? "
                "AND (? OR kind NOT IN ('internal','brief','business','details')) ORDER BY created_at DESC,id DESC LIMIT 20", (project_id, include_internal))]
        statuses = {r["status"] for r in sources}
        coverage = "not_observed" if not sources or statuses == {"not_observed"} else "unavailable" if statuses == {"unavailable"} else "partial" if statuses - {"available"} else "observed_sources_available"
        return {"project_id": project_id, "schema_version": 1, "monitoring": project["status"],
                "coverage": coverage, "sources": sources, "jobs": jobs, "jobs_truncated": len(jobs) == 20,
                "business_silence": "not_evaluated", "full_coverage": False}
