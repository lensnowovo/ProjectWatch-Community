"""Monitoring depth: website changes, feedback rules, watch points, storylines. Synthetic, no network."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from project_watch.feeds import FeedError
from project_watch.studio import monitor as mon
from project_watch.studio import sitewatch as sw


class Page:
    def __init__(self, body: str, ctype: str = "text/html; charset=utf-8", url: str = "") -> None:
        self.body, self.headers, self.url = body.encode(), {"content-type": ctype}, url


class Site:
    """Fake guarded transport: a dict of url → html (missing → 404 like the real transport)."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages, self.requests = pages, []

    def get_response(self, url):
        self.requests.append(url)
        if url not in self.pages:
            raise FeedError("http_failure", status=404)
        return Page(self.pages[url], url=url)


HOME = """<html><body><nav><a href="/news">新闻动态</a><a href="/product">产品中心</a><a href="/about/team">管理团队</a>
<a href="/join">加入我们</a><a href="https://other.example/x">外部链接</a></nav>
<h1>光启视界 车载激光雷达光学模组</h1><p>专注 905nm 发射模组与接收透镜组。</p><p>2026年10月8日</p></body></html>"""
NEWS_V1 = "<ul><li><a>光启视界亮相上海车展</a></li><li><a>光启视界苏州二期产线开工</a></li><li><a>光启视界完成B轮融资</a></li><li>2026-09-28</li></ul>"
NEWS_V2 = "<ul><li><a>光启视界获某新势力车企量产定点</a></li><li><a>光启视界亮相上海车展</a></li><li><a>光启视界苏州二期产线开工</a></li><li><a>光启视界完成B轮融资</a></li></ul>"


def test_snapshot_diff_and_discovery_are_deterministic():
    blocks, links = sw.snapshot(HOME)
    assert "光启视界 车载激光雷达光学模组" in blocks and "2026年10月8日" not in blocks  # dates are volatile
    found = {p["kind"]: p["url"] for p in sw.discover("https://www.lumina.example/", links)}
    assert found == {"news": "https://www.lumina.example/news", "product": "https://www.lumina.example/product",
                     "team": "https://www.lumina.example/about/team", "careers": "https://www.lumina.example/join"}
    old, _ = sw.snapshot(NEWS_V1)
    new, _ = sw.snapshot(NEWS_V2)
    assert sw.diff(old, new) == (["光启视界获某新势力车企量产定点"], [])
    assert sw.diff(new, list(reversed(new))) == ([], [])  # re-ordering is not a change
    assert sw.normalise_url("lumina.example") == "https://lumina.example/"
    assert sw.normalise_url("ftp://x") is None and sw.normalise_url("https://u:p@x.com") is None


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_robots_http_refusal_never_requests_pages(tmp_path, status):
    from project_watch.feeds import GuardedHTTPS

    _repo, _store, project, m = _setup(tmp_path)
    requests = []
    def connector(host, ip, path):
        requests.append(path)
        return status, {}, b""

    transport = GuardedHTTPS(resolver=lambda _: ["8.8.8.8"], connector=connector)
    mon.check_sites(m, transport, project.id, "https://lumina.example/")
    assert requests == ["/robots.txt"]
    assert m.pages(project.id)[0]["status"] == "robots_disallowed"


def test_page_redirect_checks_destination_robots_before_request(tmp_path):
    from project_watch.feeds import GuardedHTTPS

    _repo, _store, project, m = _setup(tmp_path)
    m.add_page(project.id, "https://lumina.example/news", "新闻", "news", "user")
    requests = []
    def connector(host, ip, path):
        requests.append((host, path))
        if path == "/robots.txt":
            return 200, {}, b"User-agent: *\nDisallow: /" if host == "other.example" else b""
        return 302, {"location": "https://other.example/private"}, b""

    mon.check_sites(m, GuardedHTTPS(resolver=lambda _: ["8.8.8.8"], connector=connector), project.id, None)
    assert requests == [("lumina.example", "/robots.txt"), ("lumina.example", "/news"), ("other.example", "/robots.txt")]
    assert m.pages(project.id)[0]["error"] == "redirect_disallowed"


def test_host_worker_without_per_redirect_robots_guard_fails_closed(tmp_path, monkeypatch):
    from project_watch.host_network import HostArticleTransport

    _repo, _store, project, m = _setup(tmp_path)
    requests = []
    transport = HostArticleTransport("synthetic")
    monkeypatch.setattr(transport, "get_response", lambda url: requests.append(url))
    mon.check_sites(m, transport, project.id, "https://lumina.example/")
    assert requests == []
    assert m.pages(project.id)[0]["status"] == "robots_disallowed"


def test_snapshot_ignores_hidden_text_and_gb18030_preserves_chinese():
    html = '<meta charset="gb18030"><p>合成公司发布产品</p><div hidden><a href="/news">虚构新闻</a></div>' \
           '<section style="display: none"><p>随机隐藏轮播</p></section><p>车载光学模组量产</p>'
    decoded = sw.decode(html.encode("gb18030"), "text/html")
    blocks, links = sw.snapshot(decoded)
    assert blocks == ["合成公司发布产品", "车载光学模组量产"] and links == []
    assert sw.discover("https://company.co.uk/", [("新闻", "https://unrelated.co.uk/news")]) == []
    assert sw.normalise_url("https://[broken/news") is None


def _setup(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {"legal_name": "光启视界科技有限公司", "short_name": "光启视界",
                                      "website": "www.lumina.example"}, consent=True)
    return repo, store, project, mon.MonitorStore(store)


def test_site_check_discovers_baselines_then_reports_changes(tmp_path):
    _repo, store, project, m = _setup(tmp_path)
    site = Site({"https://www.lumina.example/robots.txt": "User-agent: *\nDisallow: /join\n",
                 "https://www.lumina.example/": HOME, "https://www.lumina.example/news": NEWS_V1,
                 "https://www.lumina.example/product": "<p>产品 A</p><p>产品 B 介绍</p><p>产品 C 规格</p>",
                 "https://www.lumina.example/about/team": "<p>创始人 张三</p><p>CTO 李四</p><p>COO 王五</p>"})
    first = mon.check_sites(m, site, project.id, "www.lumina.example")
    pages = {p["kind"]: p for p in m.pages(project.id)}
    assert set(pages) == {"home", "news", "product", "team", "careers"}
    assert pages["careers"]["status"] == "robots_disallowed" and first["changed"] == 0
    assert "https://www.lumina.example/join" not in site.requests  # never fetched
    # Next day: a new headline on the news page and the CTO gone from the team page.
    site.pages["https://www.lumina.example/news"] = NEWS_V2
    site.pages["https://www.lumina.example/about/team"] = "<p>创始人 张三</p><p>COO 王五</p><p>CFO 赵六</p>"
    with m._db() as db, db:
        db.execute("UPDATE studio_site_pages SET last_checked='2000-01-01T00:00:00+00:00'")
    second = mon.check_sites(m, site, project.id, "www.lumina.example")
    assert second["changed"] == 2
    changes = {c["label"]: c for c in m.changes(project.id)}
    assert changes["新闻动态"]["added"] == ["光启视界获某新势力车企量产定点"]
    assert changes["管理团队"]["removed"] == ["CTO 李四"] and changes["管理团队"]["added"] == ["CFO 赵六"]
    items = {i["title"]: i for i in store.untriaged(project.id)}
    news = items["官网新闻动态更新：光启视界获某新势力车企量产定点"]
    assert news["body_status"] == "site_diff" and "+ 光启视界获某新势力车企量产定点" in news["body"]
    assert news["source"] == "公司官网"
    # A third run the same day reads nothing (checked at most once per CHECK_HOURS).
    before = len(site.requests)
    mon.check_sites(m, site, project.id, "www.lumina.example")
    assert len(site.requests) == before


def test_script_only_pages_are_reported_not_diffed(tmp_path):
    _repo, _store, project, m = _setup(tmp_path)
    m.add_page(project.id, "https://app.example/", "首页", "home", "user")
    site = Site({"https://app.example/robots.txt": "", "https://app.example/": "<div id=root></div>"})
    mon.check_sites(m, site, project.id, None)
    [page] = m.pages(project.id)
    assert page["status"] == "unreadable" and "脚本" in page["error"]


def test_rules_exclude_other_entities_and_blocked_sources_but_never_the_company():
    rules = [{"id": "1", "kind": "exclude_entity", "value": "光启技术"},
             {"id": "2", "kind": "block_source", "value": "spam.example"}]
    names = ["光启视界科技有限公司", "光启视界"]
    assert mon.rule_match({"title": "光启技术发布超材料新品", "url": "https://n.example/1"}, rules, names)["id"] == "1"
    assert mon.rule_match({"title": "光启技术与光启视界合作", "url": "https://n.example/2"}, rules, names) is None
    assert mon.rule_match({"title": "光启视界融资", "url": "https://m.spam.example/3"}, rules, names)["id"] == "2"


class Model:
    ready = True

    def __init__(self) -> None:
        self.users: list[str] = []

    def json(self, system, user, max_tokens=0):
        self.users.append(user)
        if "事件线索" in system:
            ids = re.findall(r"\[(N\d+)\]", user)
            return {"assign": [{"event": ids[0], "thread": "new", "kind": "客户与订单", "stage": "定点/中标",
                                "title": "新势力车企定点", "next_step": "量产", "query": "光启视界 定点 量产"},
                               {"event": ids[-1], "thread": "new", "kind": "不存在的类型", "stage": "x"}]}
        if "关注点" in system or "监控设置" in system:
            return {"label": "比亚迪合作", "queries": ["光启视界 比亚迪"], "criteria": "与比亚迪的合作或订单"}
        ids = re.findall(r"id=([0-9a-f]+)", user)
        return {"results": [{"id": i, "source_title": t, "source_quote": t, "relevant": True, "category": "company", "importance": "routine",
                             "summary": "s", "impact": "", "watch": ["W1"]}
                            for i, t in zip(ids, re.findall(r"标题：(.*)", user), strict=True)]}


def test_triage_applies_rules_feedback_and_watch_points(tmp_path):
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore

    repo, store, project, m = _setup(tmp_path)
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / "s.json"))
    m.add_rule(project.id, "exclude_entity", "光启技术")
    m.add_feedback(project.id, "e", "importance", "光启视界参加论坛", "notable", "routine")
    watch = m.add_watchpoint(project.id, "盯紧与比亚迪的合作", "比亚迪合作", ["光启视界 比亚迪"], "与比亚迪的合作", True)
    store.add_items(project.id, "company", [
        {"key": "a", "title": "光启技术发布超材料新品", "url": "https://n.example/a", "source": "示例", "snippet": "", "published_at": None},
        {"key": "b", "title": "光启视界与比亚迪签署合作", "url": "https://n.example/b", "source": "示例", "snippet": "", "published_at": None}])
    model = Model()
    pipeline.triage(project.id, model, store.get_project(project.id), {"competitors": []}, lambda _: None)
    items = {i["key"]: i for i in store.list_items(project_id=project.id, include_dismissed=True)}
    assert "a" not in items  # excluded before the model, never shown
    assert items["b"]["importance"] == "notable"  # a watch-point hit is at least notable
    prompt = model.users[0]
    assert "光启技术发布" not in prompt and "用户过往校正" in prompt and "[W1] 盯紧与比亚迪的合作" in prompt
    assert m.rules(project.id)[0]["hits"] == 1
    assert [h["label"] for h in m.pending_watch_alerts()] == ["比亚迪合作"] and watch["hits"] == 0


def test_storylines_take_only_valid_stages_and_expose_next_step_queries(tmp_path):
    _repo, store, project, m = _setup(tmp_path)
    for n, title in enumerate(("光启视界获某新势力车企定点", "光启视界创始人接受采访")):
        store.add_items(project.id, "company", [{"key": f"k{n}", "title": title, "url": f"https://n.example/{n}",
                                                 "source": "示例", "snippet": "", "published_at": None}])
    for i in store.untriaged(project.id):
        store.save_triage([{"id": i["id"], "relevant": True, "importance": "major", "summary": i["title"]}])
        store.create_event(project.id, {"title": i["title"], "category": "company", "importance": "major",
                                        "summary": i["title"], "event_at": datetime.now(UTC).isoformat()}, [i["id"]])
    assert mon.update_threads(m, store, Model(), project.id, store.get_project(project.id)) == 1
    [thread] = m.threads(project.id)
    assert thread["kind"] == "客户与订单" and thread["reached"] == 1 and thread["status"] == "open"
    assert thread["stages"] == ["接触/试用", "定点/中标", "小批量", "量产/交付"]
    assert mon.thread_queries(m, project.id) == ["光启视界 定点 量产"]
    # Placed events are not re-sent; an old open thread reads as stalled.
    assert mon.update_threads(m, store, Model(), project.id, store.get_project(project.id)) in (0, 1)
    old = (datetime.now(UTC) - timedelta(days=120)).date().isoformat()
    stalled = mon.thread_view({**thread, "steps": [{**thread["steps"][0], "date": old}]})
    assert stalled["status"] == "stalled"
    done = mon.thread_view({**thread, "steps": [{**thread["steps"][0], "stage": "量产/交付"}]})
    assert done["status"] == "done"
    assert done['support'] == 'legacy_unverified' and done['business_silence'] == 'not_evaluated'
    reversed_stage = mon.thread_view({**thread, 'steps': [
        {**thread['steps'][0], 'stage': '量产/交付', 'date': '2026-01-01'},
        {**thread['steps'][0], 'stage': '接触/试用', 'date': '2026-02-01'}]})
    assert reversed_stage['reached'] == 0 and reversed_stage['current_stage'] == '接触/试用'
    conflict = mon.thread_view({**thread, 'steps': [
        {**thread['steps'][0], 'stage': '量产/交付', 'date': '2026-01-01'},
        {**thread['steps'][0], 'stage': '接触/试用', 'date': '2026-01-01'}]})
    assert conflict['stage_conflict'] and conflict['current_stage'] is None


@pytest.fixture
def client(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    return app, TestClient(app)


def _event(app, project_id, title="光启技术发布超材料新品", url="https://spam.example/1"):
    store = app.state.studio_store
    store.add_items(project_id, "company", [{"key": title, "title": title, "url": url, "source": "示例", "snippet": "",
                                             "published_at": None}])
    [item] = [i for i in store.untriaged(project_id) if i["title"] == title]
    store.save_triage([{"id": item["id"], "relevant": True, "importance": "notable", "summary": title}])
    return store.create_event(project_id, {"title": title, "category": "company", "importance": "notable",
                                           "summary": title, "event_at": None}, [item["id"]])


def test_feedback_api_turns_corrections_into_visible_rules(client):
    app, c = client
    project = c.post("/api/studio/projects", json={"entity": {"legal_name": "光启视界"}, "consent": True}).json()
    e1 = _event(app, project["id"])
    r = c.post(f"/api/studio/events/{e1}/feedback", json={"kind": "wrong_entity", "value": "光启技术"}).json()
    assert r["rule"]["kind"] == "exclude_entity" and "光启技术" in r["message"]
    e2 = _event(app, project["id"], "光启视界相关软文", "https://www.spam.example/2")
    r = c.post(f"/api/studio/events/{e2}/feedback", json={"kind": "bad_source"}).json()
    assert r["rule"]["value"] == "spam.example"
    e3 = _event(app, project["id"], "光启视界参加论坛", "https://ok.example/3")
    assert c.post(f"/api/studio/events/{e3}/feedback", json={"kind": "importance", "value": "routine"}).json()["ok"]
    state = c.get(f"/api/studio/projects/{project['id']}/monitor").json()
    assert {r["kind"] for r in state["rules"]} == {"exclude_entity", "block_source"}
    statuses = {e["id"]: e for e in app.state.studio_store.list_events(project_id=project["id"], include_dismissed=True)}
    assert statuses[e1]["status"] == "dismissed" and statuses[e3]["importance"] == "routine"
    assert c.delete(f"/api/studio/rules/{state['rules'][0]['id']}").status_code == 204
    assert c.post(f"/api/studio/events/{e3}/feedback", json={"kind": "wrong_entity"}).status_code == 422


def test_watchpoint_and_page_api(client):
    _app, c = client
    project = c.post("/api/studio/projects", json={"entity": {"legal_name": "光启视界"}, "consent": True}).json()
    w = c.post(f"/api/studio/projects/{project['id']}/watchpoints", json={"text": "盯紧与比亚迪的合作"}).json()
    assert w["queries"] == ["光启视界 盯紧与比亚迪的合作"] and w["alert"] is True  # no model: plain fallback query
    assert c.patch(f"/api/studio/watchpoints/{w['id']}", json={"alert": False}).json()["ok"]
    assert c.get(f"/api/studio/watchpoints/{w['id']}/events").json() == []
    page = c.post(f"/api/studio/projects/{project['id']}/pages", json={"url": "lumina.example/news", "label": "新闻"}).json()
    assert page["url"] == "https://lumina.example/news" and page["added_by"] == "user"
    assert c.post(f"/api/studio/projects/{project['id']}/pages", json={"url": "ftp://x"}).status_code == 422
    state = c.get(f"/api/studio/projects/{project['id']}/monitor").json()
    assert [p["label"] for p in state["pages"]] == ["新闻"] and state["watchpoints"][0]["alert"] is False
    assert "客户与订单" in state["stages"]
    assert c.delete(f"/api/studio/site-pages/{page['id']}").status_code == 204
    assert c.delete(f"/api/studio/watchpoints/{w['id']}").status_code == 204


def test_watch_hits_are_pushed_once(client, monkeypatch):
    from project_watch.studio.http import Http, Response

    app, c = client
    posts = []
    app.state.studio_pipeline.http_factory = lambda s: Http(lambda m, u, h, b, t: (posts.append(json.loads(b)), Response(200, {}, b'{"errcode":0}'))[1])
    app.state.studio_pipeline.settings_store.update({"notify": {"kind": "wecom", "weekly": False,
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic"}})
    project = c.post("/api/studio/projects", json={"entity": {"legal_name": "光启视界"}, "consent": True}).json()
    m = app.state.studio_pipeline.monitor
    w = m.add_watchpoint(project["id"], "盯紧比亚迪", "比亚迪", ["q"], "c", True)
    e = _event(app, project["id"], "光启视界与比亚迪签约", "https://n.example/9")
    item = app.state.studio_store.list_events(event_ids=[e])[0]["sources"][0]
    m.add_hits([(w["id"], item["id"])])
    assert "watch:1" in app.state.studio_pipeline.notify()
    assert "比亚迪" in posts[-1]["markdown"]["content"]
    assert app.state.studio_pipeline.notify() == []


def test_watch_push_backlog_and_paused_projects(client):
    from project_watch.studio.http import Http, Response

    app, c = client
    pipeline = app.state.studio_pipeline
    pipeline.http_factory = lambda _: Http(lambda *_: Response(200, {}, b'{"errcode":0}'))
    pipeline.settings_store.update({"notify": {"kind": "wecom", "weekly": False,
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic"}})
    project = c.post("/api/studio/projects", json={"entity": {"legal_name": "合成公司"}, "consent": True}).json()
    m = pipeline.monitor
    w = m.add_watchpoint(project["id"], "订单", "订单", ["订单"], "订单", True)
    for n in range(11):
        e = _event(app, project["id"], f"合成公司订单{n}", f"https://n.example/{n}")
        item = app.state.studio_store.list_events(event_ids=[e])[0]["sources"][0]
        m.add_hits([(w["id"], item["id"])])
    with app.state.studio_store._connect() as db:
        db.execute("UPDATE studio_projects SET status='paused' WHERE project_id=?", (project["id"],))
    assert pipeline.notify() == []
    with app.state.studio_store._connect() as db:
        db.execute("UPDATE studio_projects SET status='active' WHERE project_id=?", (project["id"],))
    assert pipeline.notify() == ["watch:10"]
    assert len(m.pending_watch_alerts()) == 1
    assert pipeline.notify() == ["watch:1"]
    assert m.pending_watch_alerts() == []


def test_model_cannot_merge_different_financing_rounds_into_one_thread(tmp_path):
    _repo, store, project, m = _setup(tmp_path)
    m.save_thread({"id": "b", "project_id": project.id, "kind": "融资", "title": "B轮融资", "steps": [
        {"stage": "官宣", "event_id": "old", "date": datetime.now(UTC).date().isoformat(),
         "title": "合成公司完成1亿元B轮融资", "url": "https://n.example/b"}]})
    store.create_event(project.id, {"title": "合成公司完成2亿元C轮融资", "summary": "合成公司完成2亿元C轮融资",
                                   "importance": "major", "category": "company", "event_at": datetime.now(UTC).isoformat()}, [])
    class WrongModel:
        ready = True

        def json(self, *args, **kwargs):
            return {"assign": [{"event": "N1", "thread": "T1", "kind": "融资", "stage": "官宣", "title": "C轮融资"}]}

    assert mon.update_threads(m, store, WrongModel(), project.id, store.get_project(project.id)) == 1
    assert len(m.threads(project.id)) == 2
    assert len(next(t for t in m.threads(project.id) if t["id"] == "b")["steps"]) == 1
