"""Installed package acceptance with synthetic material and a no-network compiler.

Exercises the real upload/parser/authorization/reading APIs. --serve exposes the
temporary synthetic workspace for UI inspection only; never uses the pilot DB.
This is not an acceptance of DSH/provider output quality or public collection.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.company_state import READING_OUTPUT_TOKENS
from project_watch.model_provider import ModelProviderConfig
from project_watch.repository import ProjectRepository


class SyntheticCompiler:
    """Deterministic contract fixture. Never connects to the configured test URL."""
    def __init__(self):
        self.config = ModelProviderConfig("https://example.test", "anthropic-messages", "synthetic-no-network")
        self.calls = 0

    def authorization_id(self):
        return "b" * 64

    def disclosure(self):
        return {"remaining_runs": 20 - self.calls}

    def compile_reading(self, packet, *, before_send):
        before_send(json.dumps({"model": self.config.model, "stream": True,
                                "max_tokens": READING_OUTPUT_TOKENS, "messages": []}).encode())
        self.calls += 1
        items = []
        for fragment in packet["fragments"]:
            for text in fragment["text"].splitlines():
                if text.startswith("业务："):
                    topic, headline = "business", "工业检测设备与客户验证"
                elif text.startswith("财务："):
                    topic, headline = "financial", "收入口径与回款"
                elif text.startswith("客户："):
                    topic, headline = "customers", "客户试用进展"
                elif text.startswith("风险："):
                    topic, headline = "risks", "尚待核实的事项"
                else:
                    continue
                items.append({"topic": topic, "headline": headline, "text": text,
                              "evidence": [{"ref_id": fragment["ref_id"], "quote": text}], "as_of": None})
        return {"items": items[:8]}


def exercise(root):
    os.environ["PROJECT_WATCH_SETTINGS_TOKEN"] = "synthetic-local-only"
    model = SyntheticCompiler()
    db = Path(root) / "watch.sqlite3"
    app = create_app(db, proposal_model=model)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"Origin": "http://127.0.0.1", "X-Settings-Token": "synthetic-local-only"}
    project = client.post("/api/projects", json={"name": "合成光学 · 内部整理验收"}).json()
    project_id = project["id"]
    route = f"/api/workspace/projects/{project_id}"
    upload = f"/api/projects/{project_id}/files"
    content = ("业务：合成光学公司提供工业检测设备，当前处于客户验证阶段。\n"
               "客户：设备正在客户产线试用，验收及采购合同尚未取得。\n"
               "财务：公司材料列示2025年收入100万元，属于未经审计的实际数，回款尚未核实。\n"
               "风险：客户试用不等于订单，需核实验收条件与收入确认依据。")
    first = client.post(upload, files={"file": ("合成公司BP.txt", content.encode(), "text/plain")})
    assert first.status_code == 202, first.status_code
    assert app.state.intake_worker.process_one().status == "completed"
    assert app.state.reading_worker.process_one() is None and model.calls == 0
    before = client.get(route + "/reading-authorization").json()
    grant = client.post(route + "/reading-authorization", headers=headers, json={
        "consent": True, "config_id": before["model"]["config_id"], "request_key": "synthetic-installed-compile",
        "rule_version": before["rule_version"], "max_output_tokens": before["model"]["max_output_tokens"],
        "include_future": True, "max_requests": 5,
    })
    assert grant.status_code == 200, grant.status_code
    assert app.state.reading_worker.process_one()["state"] == "succeeded"
    first_reading = client.get(route + "/reading").json()
    assert len(first_reading["compiled"]["batches"]) == 1
    assert len(first_reading["compiled"]["batches"][0]["items"]) >= 3
    focus = client.get(route + '/profile').json()['focus']
    assert len(focus) == 1 and focus[0]['origin'] == 'ai_synthesis'
    assert focus[0]['source_refs'][0]['locator'] == 'line 4'
    assert '客户试用不等于订单' in focus[0]['text']
    repo = ProjectRepository(db)
    repo.edit_monitor_profile(project_id, expected_version=0, fields={"thesis": "合成验收：先核实客户验收，再判断收入持续性。"})
    client.post(upload, files={"file": ("合成经营更新.txt", "客户：新增客户完成试用，但尚未签署采购合同。".encode(), "text/plain")})
    assert app.state.intake_worker.process_one().status == "completed"
    assert app.state.reading_worker.process_one()["state"] == "succeeded"
    assert model.calls == 2
    assert len(client.get(route + "/reading").json()["compiled"]["batches"]) == 2
    updates = client.get(route + '/reading').json()['updates']
    assert len(updates['items']) == 1
    update = updates['items'][0]
    assert update['topic'] == 'customers' and not update['business_change_verified']
    assert '尚未签署采购合同' in update['current'][0]['text']
    assert '验收及采购合同尚未取得' in update['previous'][0]['text']
    assert update['current_source']['id'] != update['previous_source']['id']
    assert client.get(route + "/profile").json()["fields"]["thesis"]["origin"] == "human"
    duplicate = client.post(upload, files={"file": ("合成公司BP.txt", content.encode(), "text/plain")})
    assert duplicate.status_code == 202
    assert app.state.reading_worker.process_one() is None and model.calls == 2
    reboot = create_app(db, proposal_model=model, start_worker=True)
    assert reboot.state.reading_worker.process_one() is None
    assert len(TestClient(reboot).get(route + "/reading").json()["compiled"]["batches"]) == 2
    assert TestClient(reboot).get(route + '/reading').json()['updates'] == updates
    assert TestClient(reboot).get(route + '/profile').json()['focus'] == focus
    assert repo.list_observations(project_id, status="confirmed") == []
    print("Installed synthetic smoke passed: upload, parse, consent, compiled reading, update, duplicate, restart, human note; no network.", flush=True)
    return reboot, project_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=int, default=0)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pw-company-reading-") as root:
        app, project_id = exercise(root)
        if args.serve:
            import uvicorn
            print(f"Synthetic UI only: http://127.0.0.1:{args.serve}/#project/{project_id}/overview", flush=True)
            uvicorn.run(app, host="127.0.0.1", port=args.serve)


if __name__ == "__main__":
    main()
