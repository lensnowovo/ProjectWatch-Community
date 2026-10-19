"""Installed-wheel smoke: synthetic temporary state only, no model/network calls."""

import json
import tempfile
from pathlib import Path

from project_watch.reading_jobs import ReadingDenied, ReadingJobs, ReadingLimits
from project_watch.repository import ProjectRepository


def main():
    with tempfile.TemporaryDirectory(prefix="project-watch-reading-smoke-") as root:
        repo = ProjectRepository(Path(root) / "watch.sqlite3")
        project = repo.create_project("Synthetic reading acceptance")
        source = repo.add_source(project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64)
        ledger = ReadingJobs(repo)
        grant = ledger.authorize(
            project.id, config_id="b" * 64, model="synthetic", rule_version="smoke-v1",
            source_hashes=[source.content_sha256], include_future=False, limits=ReadingLimits(max_requests=1),
            actor="synthetic-acceptance", recipient={"base_url": "https://example.test", "protocol": "anthropic-messages", "model": "synthetic"},
        )
        job = ledger.enqueue(grant["id"], sources={source.id: source.content_sha256}, input_digest="c" * 64)
        claim = ledger.claim(job["id"])
        callback = ledger.before_send(job["id"], claim["lease_token"], config_id="b" * 64)
        body = json.dumps({"model": "synthetic", "stream": True, "max_tokens": 64, "messages": []}).encode()
        ledger.set_status(grant["id"], "paused", actor="synthetic-acceptance")
        try:
            callback(body)
        except ReadingDenied:
            pass
        else:
            raise AssertionError("paused egress was admitted")
        ledger.set_status(grant["id"], "active", actor="synthetic-acceptance")
        callback(body)  # Reserves locally; not attached to an HTTP client.
        ledger.finish(job["id"], claim["lease_token"], succeeded=False)
        reopened = ReadingJobs(ProjectRepository(repo.db_path))
        assert reopened.status(grant["id"])["requests_used"] == 1
        assert reopened.get(job["id"])["state"] == "uncertain"
        assert reopened.claim(job["id"]) is None
        assert repo.list_observations(project.id) == []
        print("Installed consent smoke passed: pause, restart, persistent quota, no uncertain retry; no network.")


if __name__ == "__main__":
    main()
