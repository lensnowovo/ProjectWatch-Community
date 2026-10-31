"""官网补齐: a project without a website finds its official site during the round. Synthetic, no network."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from project_watch.studio import monitor as mon
from project_watch.studio import sitewatch as sw
from tests.test_studio_monitor import HOME, Site

NAMES = ["光启视界", "光启视界科技有限公司"]


def _hit(url, title, snippet=""):
    return {"url": url, "title": title, "snippet": snippet, "key": url}


def test_candidates_skip_media_directories_and_public_bodies_and_need_the_name():
    hits = [_hit("https://www.qcc.com/firm/abc.html", "光启视界科技有限公司 - 企查查"),
            _hit("https://news.qq.com/a/1", "光启视界完成B轮融资"),
            _hit("https://www.nmpa.gov.cn/x", "光启视界 产品注册"),
            _hit("https://other.example/", "另一家公司官网"),
            _hit("https://www.lumina.example/about", "关于光启视界", "光启视界官网"),
            _hit("https://lumina-cn.example/", "光启视界 首页")]
    assert sw.official_candidates(hits, NAMES) == ["https://lumina-cn.example/", "https://www.lumina.example/"]  # root + 首页 first
    assert sw.site_names("光启", "光启视界", ["光启视界", "Lum", "LuminaTech"]) == ["光启视界", "LuminaTech"]


def _setup(tmp_path, website=""):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {"legal_name": "光启视界科技有限公司", "short_name": "光启视界",
                                      "website": website}, consent=True)
    return store, project, mon.MonitorStore(store)


IMPOSTOR = "<html><title>Lumina Shop</title><body><p>一</p><p>二</p><p>三</p></body></html>"
SITE = {"https://shop.example/robots.txt": "", "https://shop.example/": IMPOSTOR,
        "https://www.lumina.example/robots.txt": "", "https://www.lumina.example/": HOME,
        "https://www.lumina.example/news": "<li>光启视界新闻一</li><li>新闻二标题</li><li>新闻三标题</li>"}


def test_a_candidate_is_adopted_only_when_its_homepage_names_the_company(tmp_path):
    _store, project, m = _setup(tmp_path)
    site = Site(dict(SITE))
    report = mon.check_sites(m, site, project.id, None, candidates=["https://shop.example/", "https://www.lumina.example/"],
                             names=NAMES)
    assert report["found"] == "https://www.lumina.example/" and report["matched"] == "光启视界"
    assert report["tried"] == ["https://shop.example/", "https://www.lumina.example/"]
    assert report["rejected"] == {"https://shop.example/": "no_name"}
    pages = {p["kind"]: p["url"] for p in m.pages(project.id)}
    assert pages["home"] == "https://www.lumina.example/" and "news" in pages
    assert not any("shop.example" in u for u in pages.values())
    # Homepage was read once for the check and reused for discovery and the baseline.
    assert site.requests.count("https://www.lumina.example/") == 1


def test_no_matching_candidate_adds_nothing(tmp_path):
    _store, project, m = _setup(tmp_path)
    report = mon.check_sites(m, Site(dict(SITE)), project.id, None, candidates=["https://shop.example/"], names=NAMES)
    assert "found" not in report and m.pages(project.id) == []


class Search:
    has_search = True

    def __init__(self, hits):
        self.hits, self.queries = hits, []

    def web_search(self, query, *, news=False, count=10, accept=None):
        self.queries.append(query)
        return self.hits


def _pipeline(store, m, site):
    return SimpleNamespace(store=store, monitor=m, reader=lambda: SimpleNamespace(transport=site),
                           _execution_state=threading.local())








def test_an_unreachable_candidate_is_not_an_unknown_outcome(tmp_path):
    from test_studio_agent_instance import make_service

    from project_watch.feeds import FeedError
    from project_watch.studio.execution import JobExecution

    service, pid = make_service(tmp_path / "candidate.sqlite3")
    m = mon.MonitorStore(service.store)
    service.store.enqueue(pid, "public_profile")
    execution = JobExecution(service.store, service.store.claim())

    class Flaky(Site):
        def get_response(self, url):
            if "shop.example" in url:
                self.requests.append(url)
                raise FeedError("timeout")
            return super().get_response(url)

    site = Flaky(dict(SITE))
    with service.store.write_scope(execution._guard):
        report = mon.check_sites(m, site, pid, None, execution=execution, names=NAMES,
                                 candidates=["https://shop.example/", "https://www.lumina.example/"])
    assert report["found"] == "https://www.lumina.example/"
    assert not any(row["unknown"] for row in execution.usage().values())




def test_site_checks_have_their_own_budget_and_leave_article_reads_untouched(tmp_path):
    from test_studio_agent_instance import make_service

    from project_watch.studio.execution import JobExecution

    service, pid = make_service(tmp_path / "budget.sqlite3")
    m = mon.MonitorStore(service.store)
    service.store.enqueue(pid, "public_profile")
    execution = JobExecution(service.store, service.store.claim(), budgets={"page": 0})
    with service.store.write_scope(execution._guard):
        report = mon.check_sites(m, Site(dict(SITE)), pid, "https://www.lumina.example/", execution=execution)
    usage = execution.usage()
    assert report["checked"] >= 1 and "page" not in usage and 0 < usage["site"]["requests"] <= execution.budgets["site"]


def test_a_site_that_forbids_crawlers_is_reported_as_such(tmp_path):
    _store, project, m = _setup(tmp_path)
    site = Site({**SITE, "https://www.lumina.example/robots.txt": "User-agent: *\nDisallow: /\n"})
    report = mon.check_sites(m, site, project.id, None, candidates=["https://www.lumina.example/"], names=NAMES)
    assert report["rejected"] == {"https://www.lumina.example/": "robots"} and m.pages(project.id) == []
    assert "https://www.lumina.example/" not in site.requests


def test_a_candidate_host_in_backoff_is_skipped_not_fatal(tmp_path):
    import hashlib

    from test_studio_agent_instance import make_service

    from project_watch.studio.execution import JobExecution

    service, pid = make_service(tmp_path / "backoff.sqlite3")
    m = mon.MonitorStore(service.store)
    service.store.enqueue(pid, "public_profile")
    execution = JobExecution(service.store, service.store.claim())
    with service.store._connect() as db:
        db.execute("INSERT INTO studio_provider_backoff VALUES (?,?,?)",
                   (hashlib.sha256(b"guarded_site:shop.example").hexdigest(), "2999-01-01T00:00:00+00:00", 403))
    with service.store.write_scope(execution._guard):
        report = mon.check_sites(m, Site(dict(SITE)), pid, None, execution=execution, names=NAMES,
                                 candidates=["https://shop.example/", "https://www.lumina.example/"])
    assert report["rejected"] == {"https://shop.example/": "refused"}
    assert report["found"] == "https://www.lumina.example/"



