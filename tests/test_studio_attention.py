"""Portfolio attention: cross-checks, silence, watch hits and site changes across projects. Synthetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from project_watch.studio import attention, digest


def test_initial_watchpoints_use_one_call_and_survive_model_failure(tmp_path, monkeypatch):
    from project_watch.studio.app import create_studio_app

    class Model:
        ready = True
        calls = 0
        fail = False

        def json(self, *_args, **_kwargs):
            self.calls += 1
            if self.fail:
                raise RuntimeError("synthetic failure")
            return {"watchpoints": [{"id": "W2", "label": "临床", "queries": ["合成公司 临床"], "criteria": "临床进展"}]}

    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    model = Model()
    monkeypatch.setattr(pipeline, "tools", lambda _settings=None: (None, None, model, None))
    c = TestClient(app)
    body = {"entity": {"legal_name": "合成公司"}, "consent": True, "watchpoints": ["融资进展", "临床进展", "产品获批"]}
    for fail in (False, True):
        model.fail = fail
        # Separate creation intent; identical short-term clicks now replay.
        body['entity']['legal_name'] = f'合成公司{fail}'
        response = c.post("/api/studio/projects", json=body)
        assert response.status_code == 201
        points = pipeline.monitor.watchpoints(response.json()["id"])
        assert {p["text"] for p in points} == set(body["watchpoints"])
        assert all(p["queries"] and p["criteria"] for p in points)
    assert model.calls == 2
    body["watchpoints"] *= 2
    assert c.post("/api/studio/projects", json=body).status_code == 422
    assert model.calls == 2


def test_dismissed_watch_hit_does_not_need_attention(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    c = TestClient(app)
    busy, _quiet, _calm = _seed(app, c)
    store = app.state.studio_store
    store.set_event_status(store.list_events(project_id=busy)[0]["id"], "dismissed")
    data = c.get("/api/studio/attention").json()
    assert data["totals"]["watch"] == 0


def test_long_digest_keeps_attention_and_explicit_truncation():
    entry = {"kind": "crosscheck", "title": "需要核对" * 30, "detail": "合成差额", "question": "原因？"}
    portfolio = {"projects": [{"name": f"合成公司{n}", "entries": [entry] * 6} for n in range(30)]}
    markdown = attention.markdown(portfolio, compact=True) + "\n经营动态\n" + "合成进展\n" * 2000
    for provider in ("wecom", "feishu"):
        payload = digest.webhook_payload(provider, "合成周报", markdown)
        text = payload["markdown"]["content"] if provider == "wecom" else payload["content"]["text"]
        assert len(text.encode("utf-8")) <= digest.MESSAGE_LIMIT
        assert "需要核对与异常信号" in text and "完整" in text


def _project(c, name):
    return c.post("/api/studio/projects", json={"entity": {"legal_name": name}, "consent": True}).json()["id"]


def _event(store, project_id, title):
    store.add_items(project_id, "company", [{"key": title, "title": title, "url": f"https://n.example/{title}",
                                             "source": "示例", "snippet": "", "published_at": None}])
    item = next(i for i in store.untriaged(project_id) if i["title"] == title)
    store.save_triage([{"id": item["id"], "relevant": True, "importance": "notable", "summary": title}])
    store.create_event(project_id, {"title": title, "category": "company", "importance": "notable", "summary": title,
                                    "event_at": datetime.now(UTC).isoformat()}, [item["id"]])
    return item["id"]


def _seed(app, c):
    from test_studio_silence_coverage import healthy_history

    store, pipeline = app.state.studio_store, app.state.studio_pipeline
    busy, quiet, calm = _project(c, "光启视界"), _project(c, "合成储能"), _project(c, "平静公司")
    pipeline.signals.replace_crosschecks(busy, [{"fingerprint": "f", "kind": "mismatch", "severity": "high", "topic": "B轮金额",
                                                 "public_says": "3亿元", "internal_says": "2亿元", "question": "差额从哪来？",
                                                 "public_refs": [], "internal_refs": []}])
    w = pipeline.monitor.add_watchpoint(busy, "盯紧比亚迪", "比亚迪合作", ["q"], "c", True)
    pipeline.monitor.add_hits([(w["id"], _event(store, busy, "光启视界与比亚迪签约"))])
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    healthy_history(store, quiet)
    pipeline.monitor.save_thread({"id": "t", "project_id": quiet, "kind": "融资", "title": "C轮", "next_step": "官宣",
                                  "steps": [{"stage": "传闻", "event_id": "e", "date": old[:10], "title": "传闻", "url": ""}]})
    return busy, quiet, calm


def test_portfolio_ranks_projects_and_links_each_entry_to_its_tab(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    c = TestClient(app)
    busy, quiet, calm = _seed(app, c)
    data = c.get("/api/studio/attention").json()
    assert [p["id"] for p in data["projects"]] == [busy, quiet]  # the calm project is not listed
    first = data["projects"][0]
    assert [e["kind"] for e in first["entries"]] == ["crosscheck", "watch"]
    assert first["entries"][0]["tab"] == "overview" and first["entries"][1]["tab"] == "external"
    assert first["counts"]["crosscheck"] == 1 and data["totals"]["silence"] == 1
    assert data["projects"][1]["entries"][0]["title"].startswith("融资线索停滞")
    # Closing a cross-check removes it from the portfolio view; paused projects disappear.
    app.state.studio_pipeline.signals.set_status(first["entries"][0]["ref"], "resolved")
    c.patch(f"/api/studio/projects/{quiet}", json={"status": "paused"})
    data = c.get("/api/studio/attention").json()
    assert [(p["id"], [e["kind"] for e in p["entries"]]) for p in data["projects"]] == [(busy, ["watch"])]
    assert calm not in {p["id"] for p in data["projects"]}


def test_digest_includes_attention_section(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    c = TestClient(app)
    _seed(app, c)
    data = c.get("/api/studio/digest?days=7").json()
    assert data["attention"]["totals"]["crosscheck"] == 1
    assert "## 需要核对与异常信号" in data["markdown"] and "可以问：差额从哪来？" in data["markdown"]
    compact = digest.digest_markdown(data, compact=True)
    assert "内外对照 · 数字或事实不一致：B轮金额" in compact and "可以问" not in compact
    assert attention.markdown({"projects": []}) == ""


def test_mark_all_read_respects_project_and_window(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    c = TestClient(app)
    store = app.state.studio_store
    a, b = _project(c, "甲公司"), _project(c, "乙公司")
    _event(store, a, "甲公司签约")
    _event(store, b, "乙公司签约")
    starred = store.list_events(project_id=a)[0]["id"]
    _event(store, a, "甲公司扩产")
    store.set_event_status(starred, "starred")
    assert c.post("/api/studio/events/read-all", json={"project_id": a}).json() == {"updated": 1}
    statuses = {e["title"]: e["status"] for e in store.list_events(limit=10)}
    assert statuses == {"甲公司签约": "starred", "甲公司扩产": "read", "乙公司签约": "new"}
    assert c.post("/api/studio/events/read-all", json={"days": 7}).json() == {"updated": 1}
