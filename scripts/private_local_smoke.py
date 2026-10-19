"""Synthetic end-to-end acceptance against an already running isolated local pilot.

Creates a clearly labelled synthetic project; never reads user documents or
confirms records in an existing project. Requires the dev httpx dependency.
"""
from __future__ import annotations

import argparse
import json
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


def run(url: str) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("synthetic acceptance only targets the explicit local pilot")
    with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        def post(path, **kwargs):
            response = client.post(path, **kwargs)
            response.raise_for_status()
            return response.json()

        assert get("/api/runtime")["private_local_intake"] is True
        run_id = uuid4().hex[:12]
        project = post("/api/projects", json={"name": f"增量闭环验收（合成数据）-{run_id}"})
        route = f"/api/projects/{project['id']}"

        def upload(name, content):
            result = post(f"{route}/files", files={"file": (name, content.encode(), "text/plain")})
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                job = next(row for row in get(f"{route}/intake") if row["id"] == result["job"]["id"])
                if job["status"] in {"completed", "failed", "needs_ocr"}:
                    assert job["status"] == "completed", job
                    return result
                time.sleep(0.3)
            raise TimeoutError("synthetic intake did not finish")

        first = upload("synthetic-first.txt", "营业收入待核对。")
        observation = get(f"{route}/observations")[0]
        assert observation["status"] == "candidate" and observation["source_locator"] == "line 1"
        post(f"/api/observations/{observation['id']}/confirm", json={
            "reviewer": "synthetic-acceptance-only", "expected_version": observation["version"],
        })
        second = upload("synthetic-update.txt", "营业收入发生变化，仍需核对。")
        duplicate = upload("synthetic-update.txt", "营业收入发生变化，仍需核对。")
        assert duplicate["source"]["id"] == second["source"]["id"]
        assert duplicate["job"]["id"] == second["job"]["id"]
        summary = get(f"{route}/summary")
        assert summary["source_count"] == 2 and summary["confirmed_count"] == 1
        report = get(f"{route}/report/current")
        assert report["pending_review_count"] == 1
        assert get(f"/api/sources/{first['source']['id']}/fragments")[0]["text"] == "营业收入待核对。"
        assert len(get(f"{route}/monitoring/domains")["domains"]) == 6
        other = post("/api/projects", json={"name": f"跨项目边界验收（合成）-{run_id}"})
        denied = client.get(f"/api/projects/{other['id']}/sources/{first['source']['id']}/fragments")
        assert denied.status_code == 404
        return {"project_id": project["id"], "source_count": 2, "confirmed_synthetic_count": 1,
                "pending_review_count": 1, "duplicate_upload": "deduplicated", "cross_project": "denied"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8796")
    args = parser.parse_args()
    print(json.dumps(run(args.url), ensure_ascii=False))
