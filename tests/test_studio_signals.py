"""Signals beyond single items: cross-checks, silence, cadence, latency, public records. Synthetic, no network."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from project_watch.studio import monitor as mon
from project_watch.studio import signals as sig


def _setup(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {"legal_name": "光启视界科技有限公司", "short_name": "光启视界"}, consent=True)
    return repo, store, project, mon.MonitorStore(store), sig.SignalStore(store)


def _event(store, project_id, title, *, days=1, importance="notable", category="company"):
    at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    key = uuid.uuid4().hex
    store.add_items(project_id, category, [{"key": key, "title": title, "url": f"https://n.example/{key}", "source": "示例",
                                            "snippet": "", "published_at": at}])
    item = next(i for i in store.untriaged(project_id) if i["key"] == key)
    store.save_triage([{"id": item["id"], "relevant": True, "importance": importance, "summary": title}])
    return store.create_event(project_id, {"title": title, "category": category, "importance": importance,
                                           "summary": title, "event_at": at}, [item["id"]])


class CrossModel:
    ready = True

    def __init__(self, checks):
        self.checks, self.prompt = checks, ""

    def json(self, system, user, max_tokens=0):
        self.prompt = user
        return {"checks": self.checks}


def _internal(store, project_id):
    source_id = "src-1"
    with store._connect() as db:
        db.execute("""INSERT INTO studio_metrics (id, project_id, source_id, name, category, period, value, unit, basis, locator, created_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   ("m1", project_id, source_id, "B轮融资额", "financing", "2026", 2.0, "亿元", "actual", "第3页", datetime.now(UTC).isoformat()))
    store.put_profile(project_id, {"milestones": [{"text": "苏州二期产线 2027Q1 开工", "date": "2027Q1", "status": "planned",
                                                   "refs": [{"source_id": source_id, "source_name": "2026Q2 月报.pdf", "locator": "第5页"}]}]})


def test_full_crosscheck_keeps_only_findings_cited_on_both_sides(tmp_path):
    from project_watch.studio import crosscheck_analysis
    _repo, store, project, _m, signals = _setup(tmp_path)
    _event(store, project.id, "光启视界完成3亿元B轮融资", importance="major", days=1)
    _event(store, project.id, "光启视界苏州二期产线开工", days=2)
    _event(store, project.id, "光启视界创始人被列为被执行人", importance="major", days=3)
    _internal(store, project.id)
    model = CrossModel([
        {"kind": "mismatch", "severity": "high", "topic": "B轮金额", "public_says": "3亿元", "internal_says": "2亿元",
         "question": "差额 1 亿是否为后续交割？", "public": ["N1"], "internal": ["M1"],
         "public_quotes": {"N1": "光启视界完成3亿元B轮融资"}, "internal_quotes": {"M1": "B轮融资额 2026 20000万元（实际）"}},
        {"kind": "off_plan", "topic": "二期产线", "public_says": "已开工", "internal_says": "计划 2027Q1",
         "public": ["N2"], "internal": ["K1"], "public_quotes": {"N2": "光启视界苏州二期产线开工"},
         "internal_quotes": {"K1": "计划 2027Q1 苏州二期产线 2027Q1 开工"}},
        {"kind": "undisclosed", "severity": "high", "topic": "被执行", "public_says": "创始人被列为被执行人", "public": ["N3"],
         "public_quotes": {"N3": "光启视界创始人被列为被执行人"}},
        {"kind": "mismatch", "topic": "没有内部依据", "public": ["N1"], "internal": []},  # dropped
        {"kind": "mismatch", "topic": "编号不存在", "public": ["N9"], "internal": ["M1"]},  # dropped
        {"kind": "rumour", "topic": "类型无效", "public": ["N1"], "internal": ["M1"]},  # dropped
    ])
    assert sig.crosscheck(signals, store, model, project.id, policy=crosscheck_analysis) == 3
    assert "[M1] B轮融资额 2026 20000万元（实际）" in model.prompt and "[K1] 计划 2027Q1 苏州二期产线" in model.prompt
    checks = signals.crosschecks(project.id)
    assert [c["severity"] for c in checks] == ["high", "medium", "low"]
    by_topic = {c["topic"]: c for c in checks}
    assert by_topic["B轮金额"]["internal_refs"][0]["refs"][0]["locator"] == "第3页"
    assert "未覆盖完整资料，系统暂不判断是否遗漏" in by_topic["被执行"]["internal_says"] and by_topic["被执行"]["kind_label"] == "已提供片段未见提及"
    # A dismissed finding does not come back on the next run; open ones are replaced.
    signals.set_status(by_topic["B轮金额"]["id"], "dismissed")
    assert sig.crosscheck(signals, store, model, project.id, policy=crosscheck_analysis) == 2
    assert {c["topic"] for c in signals.crosschecks(project.id)} == {"二期产线", "被执行"}


def test_crosscheck_needs_both_public_news_and_internal_material(tmp_path):
    _repo, store, project, _m, signals = _setup(tmp_path)
    model = CrossModel([])
    assert sig.crosscheck(signals, store, model, project.id) == 0 and model.prompt == ""
    _event(store, project.id, "光启视界完成融资", importance="major")
    assert sig.crosscheck(signals, store, model, project.id) == 0 and model.prompt == ""


def test_silence_signals_are_explained(tmp_path):
    from test_studio_silence_coverage import healthy_history

    _repo, store, project, m, _s = _setup(tmp_path)
    healthy_history(store, project.id)
    for n in range(7):
        _event(store, project.id, f"光启视界旧闻 {n}", days=80 + n * 10, importance="routine")
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    with store._connect() as db:
        db.execute("UPDATE studio_projects SET created_at=? WHERE project_id=?", (old, project.id))
    news = m.add_page(project.id, "https://lumina.example/news", "新闻动态", "news", "auto")
    careers = m.add_page(project.id, "https://lumina.example/join", "加入我们", "careers", "auto")
    m.save_check(news["id"], status="ok", blocks=["a", "b", "c"])
    with m._db() as db, db:
        db.execute("UPDATE studio_site_pages SET created_at=? WHERE id=?", (old, news["id"]))
    m.add_change(project.id, careers, [], ["算法工程师", "光学工程师", "销售经理", "测试工程师"])
    m.save_thread({"id": "t1", "project_id": project.id, "kind": "融资", "title": "C轮", "next_step": "官宣",
                   "steps": [{"stage": "传闻", "event_id": "e", "date": "2026-01-02", "title": "传闻", "url": ""}]})
    kinds = {s["kind"]: s for s in sig.silence(store, m, project.id, documents=[{"created_at": old}])}
    assert set(kinds) == {"news_quiet", "site_frozen", "hiring_cut", "thread_stalled", "materials_quiet"}
    assert "此前 180 天有 7 条" in kinds["news_quiet"]["detail"]
    assert "“传闻”后的进展" in kinds["thread_stalled"]["detail"] and "官宣" in kinds["thread_stalled"]["detail"]
    # A brand-new project with nothing yet raises no alarms.
    (tmp_path / "fresh").mkdir()
    _repo2, store2, project2, m2, _s2 = _setup(tmp_path / "fresh")
    assert sig.silence(store2, m2, project2.id, documents=[]) == []


def test_cadence_follows_activity_and_drives_scheduling(tmp_path):
    from test_studio_silence_coverage import healthy_history

    _repo, store, project, m, _s = _setup(tmp_path)
    assert sig.cadence(store, m, project.id, 24)["mode"] == "normal"
    with store._connect() as db:
        db.execute("UPDATE studio_projects SET created_at=? WHERE project_id=?",
                   ((datetime.now(UTC) - timedelta(days=100)).isoformat(), project.id))
    assert sig.cadence(store, m, project.id, 24)['mode'] == 'normal'  # Missing history must not slow monitoring.
    healthy_history(store, project.id)
    assert sig.cadence(store, m, project.id, 24) == {"hours": 48, "mode": "quiet", "reason": "近 90 天没有公司自身动态"}
    assert sig.cadence(store, m, project.id, 168)["hours"] == 168  # a weekly setting is never sped up
    _event(store, project.id, "光启视界获定点", importance="major")
    _event(store, project.id, "光启视界产线开工")
    busy = sig.cadence(store, m, project.id, 24)
    assert busy["hours"] == 12 and busy["mode"] == "active"
    assert sig.cadence(store, m, project.id, 168)["hours"] == 168
    with store._connect() as db:
        db.execute("UPDATE studio_projects SET last_external_at=? WHERE project_id=?",
                   ((datetime.now(UTC) - timedelta(hours=167)).isoformat(), project.id))
    assert sig.due_external(store, m, 168) == []
    with store._connect() as db:
        db.execute("UPDATE studio_projects SET last_external_at=? WHERE project_id=?",
                   ((datetime.now(UTC) - timedelta(hours=169)).isoformat(), project.id))
    assert sig.due_external(store, m, 168) == [project.id]
    with store._connect() as db:
        db.execute("UPDATE studio_projects SET status='active', last_external_at=? WHERE project_id=?",
                   ((datetime.now(UTC) - timedelta(hours=13)).isoformat(), project.id))
    assert sig.due_external(store, m, 24) == [project.id]  # 13h > 12h for an active project
    with store._connect() as db:
        db.execute("UPDATE studio_projects SET last_external_at=? WHERE project_id=?",
                   ((datetime.now(UTC) - timedelta(hours=8)).isoformat(), project.id))
    assert sig.due_external(store, m, 24) == []


def test_new_projects_backfilled_news_and_same_day_threads(tmp_path):
    _repo, store, project, m, _s = _setup(tmp_path)
    for n in range(7):
        _event(store, project.id, f"历史动态{n}", days=80 + n * 10, importance="routine")
    assert sig.silence(store, m, project.id, []) == []
    m.add_watchpoint(project.id, "融资进度", "融资", ["融资"], "融资", True)
    m.save_thread({"id": "today", "project_id": project.id, "kind": "融资", "title": "B轮",
                   "steps": [{"stage": "官宣", "event_id": "e", "date": datetime.now(UTC).date().isoformat(), "title": "融资", "url": ""}]})
    assert sig.cadence(store, m, project.id, 24)["mode"] == "active"


def test_crosscheck_rejects_fabricated_quotes_and_different_scopes(tmp_path):
    _repo, store, project, _m, signals = _setup(tmp_path)
    _event(store, project.id, "光启视界2026年合同金额3亿元人民币", importance="major")
    with store._connect() as db:
        db.execute("INSERT INTO studio_metrics (id,project_id,source_id,name,category,period,value,unit,basis,scope,locator,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("m", project.id, "synthetic", "营业收入", "revenue", "2026", 2,
                   "亿元", "actual", "子公司 不含税", "第1页", datetime.now(UTC).isoformat()))
    finding = {"kind": "mismatch", "public": ["N1"], "internal": ["M1"], "public_quotes": {"N1": "合同金额3亿元人民币"},
               "internal_quotes": {"M1": "营业收入 2026 20000万元（实际）"}}
    assert sig.crosscheck(signals, store, CrossModel([finding]), project.id) == 0
    finding["public_quotes"]["N1"] = "编造的数字999亿元"
    assert sig.crosscheck(signals, store, CrossModel([finding]), project.id) == 0
    assert not sig._same_scope("集团2026年营业收入3亿元", "子公司2026年营业收入2亿元")
    assert not sig._same_scope("2025年营业收入3亿元", "2026年营业收入2亿元")
    assert sig._equivalent_amounts("营业收入1亿元", "营业收入10000万元")


def test_latency_uses_only_dated_recent_company_items(tmp_path):
    _repo, store, project, _m, _s = _setup(tmp_path)
    assert sig.latency(store, project.id) == {"median_hours": None, "n": 0}
    for hours in (2, 6, 30):
        published = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        store.add_items(project.id, "company", [{"key": f"k{hours}", "title": f"t{hours}", "url": f"https://n.example/{hours}",
                                                 "source": "示例", "snippet": "", "published_at": published}])
    for i in store.untriaged(project.id):
        store.save_triage([{"id": i["id"], "relevant": True, "importance": "routine", "summary": "s"}])
    result = sig.latency(store, project.id)
    assert result["n"] == 3 and 5 <= result["median_hours"] <= 7


class Search:
    has_search = True

    def __init__(self):
        self.queries = []

    def web_search(self, query, *, news=False, count=10):
        self.queries.append(query)
        site = query.split()[0].removeprefix("site:")
        return [{"key": f"{site}-1", "title": "光启视界中标某市智能网联示范项目", "url": f"https://www.{site}/a/1", "source": site,
                 "snippet": "", "published_at": None},
                {"key": f"{site}-2", "title": "无关公司公告", "url": f"https://www.{site}/a/2", "source": site, "snippet": "", "published_at": None},
                {"key": f"{site}-3", "title": "光启视界 转载", "url": "https://mirror.example/x", "source": "mirror", "snippet": "", "published_at": None}]


def test_public_records_only_keep_official_hosts_naming_the_company():
    search = Search()
    records = sig.public_records(search, ["光启视界科技有限公司", "光启视界"])
    assert {r["url"] for r in records["tender"]} == {f"https://www.{s}/a/1" for s in sig.TENDER_SITES}
    assert all(r["source"].startswith("资质/许可 · ") for r in records["license"])
    assert all(q.startswith("site:") for q in search.queries)

    class NoSearch:
        has_search = False

    assert sig.public_records(NoSearch(), ["光启视界"]) == {"tender": [], "license": []}


def test_signals_api(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    c = TestClient(app)
    project = c.post("/api/studio/projects", json={"entity": {"legal_name": "光启视界"}, "consent": True}).json()
    data = c.get(f"/api/studio/projects/{project['id']}/signals").json()
    assert data["crosschecks"] == [] and data["silence"] == [] and data["cadence"]["mode"] == "normal"
    assert data["latency"] == {"median_hours": None, "n": 0}
    signals = app.state.studio_pipeline.signals
    signals.replace_crosschecks(project["id"], [{"fingerprint": "f", "kind": "mismatch", "severity": "high", "topic": "金额",
                                                 "public_says": "3亿", "internal_says": "2亿", "question": "?",
                                                 "public_refs": [], "internal_refs": []}])
    check = c.get(f"/api/studio/projects/{project['id']}/signals").json()["crosschecks"][0]
    assert c.patch(f"/api/studio/crosschecks/{check['id']}", json={"status": "resolved"}).json()["status"] == "resolved"
    assert c.get(f"/api/studio/projects/{project['id']}/signals").json()["crosschecks"] == []
    assert c.patch("/api/studio/crosschecks/nope", json={"status": "resolved"}).status_code == 404


def test_public_records_count_calls_preserve_partial_results_and_stop_access_refusals():
    from project_watch.studio.http import HttpError

    class Intermittent(Search):
        def web_search(self, query, **kwargs):
            if "ggzy.gov.cn" in query:
                raise HttpError("http_failure", status=500)
            return super().web_search(query, **kwargs)

    report = {}
    result = sig.public_records(Intermittent(), ["光启视界"], report=report, biomed=True)
    assert len(result["tender"]) == 2 and len(result["license"]) == 4
    assert report["tender"]["checked"] == 3 and report["license"]["checked"] == 4
    assert report["tender"]["errors"] == ["ggzy.gov.cn: http_failure"]
    class Denied(Search):
        def web_search(self, query, **kwargs):
            self.queries.append(query)
            raise HttpError("search_access_denied", status=403)

    denied = Denied()
    assert sig.public_records(denied, ["光启视界", "光启视界有限公司"]) == {"tender": [], "license": []}
    assert len(denied.queries) == 1


def test_a_failed_attempt_paces_the_next_round_without_claiming_an_update(tmp_path):
    _repo, store, project, m, _s = _setup(tmp_path)
    assert sig.due_external(store, m, 24) == [project.id]  # never observed
    store.set_state(f'external_attempt:{project.id}', datetime.now(UTC).isoformat())
    assert sig.due_external(store, m, 24) == []
    assert store.get_project(project.id)['last_external_at'] is None
    store.set_state(f'external_attempt:{project.id}', (datetime.now(UTC) - timedelta(hours=25)).isoformat())
    assert sig.due_external(store, m, 24) == [project.id]
