"""Portfolio digest and group-bot push. Synthetic data, fake transport, no network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from project_watch.studio import digest as dg
from project_watch.studio.http import Http, Response
from project_watch.studio.settings import NotifyConfig, SettingsStore

WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic-key-0001"


class Bot:
    def __init__(self, reply: dict | None = None, status: int = 200) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.reply = reply if reply is not None else {"errcode": 0, "errmsg": "ok"}
        self.status = status

    def __call__(self, method, url, headers, body, timeout):
        self.posts.append((url, json.loads(body)))
        return Response(self.status, {}, json.dumps(self.reply).encode())


@pytest.fixture
def app(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    bot = Bot()
    app.state.studio_pipeline.http_factory = lambda settings: Http(bot)
    app.state.bot = bot
    return app


def _seed(app, name="光启视界", *, importance="major", title="光启视界完成3亿元B轮融资"):
    client = TestClient(app)
    project = client.post("/api/studio/projects", json={"entity": {"legal_name": f"{name}科技有限公司",
                                                                   "short_name": name}, "consent": True}).json()
    store = app.state.studio_store
    with store._connect() as db:
        # Remove only admission metadata in this synthetic queue-reset fixture.
        db.execute("DELETE FROM studio_job_checkpoints WHERE stage='collection_request_v1'")
        db.execute("DELETE FROM studio_jobs")
    store.add_items(project["id"], "company", [{"key": title, "title": title, "url": "https://direct.example/1",
                                                "source": "投资界", "snippet": "", "published_at": None}])
    [item] = store.untriaged(project["id"])
    store.save_triage([{"id": item["id"], "relevant": True, "importance": importance, "summary": title,
                        "impact": "资金可覆盖约两年研发与产线投入"}])
    store.create_event(project["id"], {"title": title, "category": "company", "importance": importance,
                                       "summary": title, "impact": "资金可覆盖约两年研发与产线投入",
                                       "event_at": datetime.now(UTC).isoformat()}, [item["id"]])
    return project


def test_webhook_settings_accept_only_official_hosts_and_are_masked(tmp_path):
    settings = SettingsStore(tmp_path / "s.json")
    with pytest.raises(ValueError):
        settings.update({"notify": {"kind": "wecom", "webhook_url": "https://evil.example/hook"}})
    with pytest.raises(ValueError):
        settings.update({"notify": {"kind": "wecom", "webhook_url": "http://qyapi.weixin.qq.com/x"}})
    with pytest.raises(ValueError):
        settings.update({"notify": {"kind": "feishu", "webhook_url": WECOM}})  # host of another vendor
    saved = settings.update({"notify": {"kind": "wecom", "webhook_url": WECOM, "weekday": 5, "hour": 17}})
    view = SettingsStore.public_view(saved)["notify"]
    assert view["ready"] and view["weekday"] == 5 and "synthetic-key" not in view["webhook_url"]
    # Saving other fields keeps the stored URL; switching vendor without a new URL clears it.
    assert settings.update({"notify": {"weekly": False}}).notify.webhook_url == WECOM
    assert settings.update({"notify": {"kind": "dingtalk"}}).notify.webhook_url == ""


def test_digest_groups_events_by_project_and_lists_quiet_ones(app):
    _seed(app)
    quiet = _seed(app, "合成医药", importance="routine", title="合成医药参加行业会议")
    store = app.state.studio_store
    store.put_plan(quiet['id'], {'last_report': {'company': {'checked': 1, 'added': 1, 'errors': []}}})
    store.touch(quiet['id'], 'last_external_at')
    data = dg.build_digest(app.state.studio_store, 7)
    assert [p["name"] for p in data["projects"]] == ["光启视界"]
    assert data["quiet"] == ["合成医药"] and data["totals"]["major"] == 1
    text = dg.digest_markdown(data)
    assert "## 光启视界" in text and "【重大】公司" in text
    assert "[光启视界完成3亿元B轮融资](https://direct.example/1)" in text and "意义：资金可覆盖" in text
    assert "已收录信息未见重要变化：合成医药" in text
    compact = dg.digest_markdown(data, compact=True)
    assert "意义" not in compact and "无明显变化" not in compact


def test_payload_shapes_and_truncation():
    long = "# t\n" + "\n".join(f"- 第{i}条动态" * 3 for i in range(800))
    wecom = dg.webhook_payload("wecom", "t", long)
    assert wecom["msgtype"] == "markdown"
    assert len(wecom["markdown"]["content"].encode("utf-8")) <= dg.MESSAGE_LIMIT
    assert wecom["markdown"]["content"].endswith("完整版请在 ProjectWatch 中查看）\n")
    assert dg.webhook_payload("dingtalk", "周报", "# x")["markdown"]["title"] == "周报"
    assert dg.webhook_payload("feishu", "t", "# 标题\n**粗体**")["content"]["text"] == "标题\n粗体"


def test_long_single_line_keeps_valid_utf8_and_the_notice():
    text = dg.webhook_payload("wecom", "t", "中文🧪" * 1500)["markdown"]["content"]
    assert len(text.encode("utf-8")) <= dg.MESSAGE_LIMIT
    assert text.startswith("中文🧪") and "完整版" in text and "\ufffd" not in text


@pytest.mark.parametrize("reply", [{}, [], {"errcode": None}, {"errcode": "0"}])
def test_push_requires_a_positive_vendor_acknowledgement(reply):
    with pytest.raises(Exception, match="notify_invalid_response"):
        dg.send(Http(Bot(reply)), NotifyConfig("wecom", WECOM), "t", "hello")


def test_push_errors_never_echo_a_secret_webhook(app):
    _seed(app)
    app.state.studio_pipeline.settings_store.update({"notify": {"kind": "wecom", "webhook_url": WECOM,
                                                              "instant_major": True, "weekly": False}})
    app.state.bot.reply = {"errcode": 93000, "errmsg": f"invalid URL: {WECOM}"}
    assert app.state.studio_pipeline.notify() == []
    error = app.state.studio_store.get_state("notify_last")
    assert "notify_rejected" in error and "synthetic-key" not in error and "https://" not in error

    def broken(*args):
        from project_watch.studio.http import HttpError
        raise HttpError("connection_failed", detail=WECOM)

    with pytest.raises(Exception, match="connection_failed") as caught:
        dg.send(Http(broken), NotifyConfig("wecom", WECOM), "t", "hello")
    assert "synthetic-key" not in str(caught.value)


def test_weekly_schedule_is_once_per_iso_week():
    config = NotifyConfig("wecom", WECOM, weekly=True, weekday=1, hour=9)
    monday_8 = datetime(2026, 10, 5, 8, 0).astimezone()
    monday_9 = datetime(2026, 10, 5, 9, 0).astimezone()
    wednesday = datetime(2026, 10, 7, 12, 0).astimezone()
    assert dg.weekly_due(config, None, monday_8) is None
    assert dg.weekly_due(config, None, monday_9) == "2026-W41"
    assert dg.weekly_due(config, "2026-W41", wednesday) is None  # already sent this week
    assert dg.weekly_due(config, "2026-W40", wednesday) == "2026-W41"  # app was off on Monday
    assert dg.weekly_due(NotifyConfig("wecom", WECOM, weekly=False), None, wednesday) is None


def test_pipeline_pushes_major_events_once_and_weekly_digest_once(app):
    _seed(app)
    app.state.studio_pipeline.settings_store.update({"notify": {"kind": "wecom", "webhook_url": WECOM, "instant_major": True,
                                                 "weekday": 1, "hour": 0}})
    pipeline, bot = app.state.studio_pipeline, app.state.bot
    sent = pipeline.notify(datetime(2026, 10, 7, 10, 0).astimezone())
    assert sent == ["major:1", "weekly:2026-W41"]
    assert "光启视界完成3亿元B轮融资" in bot.posts[0][1]["markdown"]["content"]
    assert bot.posts[1][1]["markdown"]["content"].startswith("# ProjectWatch 组合动态")
    assert pipeline.notify(datetime(2026, 10, 7, 11, 0).astimezone()) == []
    assert len(bot.posts) == 2


def test_rejected_push_is_recorded_and_backs_off(app):
    _seed(app)
    app.state.studio_pipeline.settings_store.update({"notify": {"kind": "wecom", "webhook_url": WECOM, "instant_major": True,
                                                 "weekly": False}})
    app.state.bot.reply = {"errcode": 93000, "errmsg": "invalid webhook url"}
    pipeline, store = app.state.studio_pipeline, app.state.studio_store
    assert pipeline.notify() == []
    assert "notify_rejected" in json.loads(store.get_state("notify_last"))["error"]
    assert len(store.pending_major_events()) == 1  # still pending, retried after the back-off
    app.state.bot.reply = {"errcode": 0}
    assert pipeline.notify() == []  # backing off
    store.set_state("notify_retry_after", (datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    assert pipeline.notify() == ["major:1"]


def test_old_events_upgraded_today_are_pending_but_paused_projects_are_not(app):
    project = _seed(app, importance="notable")
    store = app.state.studio_store
    [event] = store.list_events(project_id=project["id"])
    store.mark_notified([event["id"]])
    with store._connect() as db:
        db.execute("UPDATE studio_events SET event_at=? WHERE id=?",
                   ((datetime.now(UTC) - timedelta(days=100)).isoformat(), event["id"]))
    store.attach_to_event(event["id"], [], importance="major")
    assert [e["id"] for e in store.pending_major_events()] == [event["id"]]
    store.set_status(project["id"], "paused")
    assert store.pending_major_events() == []
    assert dg.build_digest(store)["projects"] == []


def test_events_created_before_push_existed_are_not_pushed(tmp_path):
    import sqlite3

    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    StudioStore(repo.db_path)
    with sqlite3.connect(repo.db_path) as db:
        db.execute("ALTER TABLE studio_events DROP COLUMN notified_at")
        db.execute("""INSERT INTO studio_events (id, project_id, title, category, importance, created_at, updated_at)
                      VALUES ('e', ?, '旧的重大事件', 'company', 'major', ?, ?)""",
                   (project.id, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()))
    assert StudioStore(repo.db_path).pending_major_events() == []


def test_digest_endpoints(app):
    _seed(app)
    client = TestClient(app)
    data = client.get("/api/studio/digest?days=7").json()
    assert data["totals"]["projects"] == 1 and data["markdown"].startswith("# ProjectWatch 组合动态")
    assert data["notify"] == {"ready": False, "last": None}
    assert client.get("/api/studio/digest?days=3").status_code == 400
    file = client.get("/api/studio/digest.md")
    assert file.headers["content-type"].startswith("text/markdown")
    assert "filename*=UTF-8''ProjectWatch-" in file.headers["content-disposition"]
    assert client.post("/api/studio/notify/test").status_code == 400  # not configured
    client.put("/api/studio/settings", json={"notify": {"kind": "wecom", "webhook_url": WECOM}})
    assert client.post("/api/studio/notify/test").json()["ok"] is True
    assert client.post("/api/studio/digest/push").json()["ok"] is True
    assert app.state.bot.posts[0][0] == WECOM
