"""Domestic sources: cninfo announcements, CN drug-trial registry via search, registry changes.

Response shapes are synthetic copies of the documented formats; no network is used.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import RegistryConfig, SearchConfig
from project_watch.studio.sources import Collectors

CNINFO = {"totalAnnouncement": 2, "announcements": [
    {"announcementId": "1", "secCode": "300999", "secName": "合成科技",
     "announcementTitle": "关于对外投资<em>光启视界</em>科技有限公司的公告",
     "announcementTime": 1759276800000, "adjunctUrl": "finalpage/2025-10-01/1224.PDF"},
    {"announcementId": "2", "secCode": "600000", "secName": "合成光电", "announcementTitle": "2025年半年度报告",
     "announcementTime": 1756656000000, "adjunctUrl": ""}]}


def _web(handler):
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body))
        return handler(method, url, headers, body)

    return Http(transport), calls


def test_cninfo_fulltext_mentions_become_announcement_items():
    def handler(method, url, headers, body):
        assert url.startswith("https://www.cninfo.com.cn/new/fulltextSearch/full?")
        return Response(200, {}, json.dumps(CNINFO, ensure_ascii=False).encode())

    http, calls = _web(handler)
    [item] = Collectors(http, SearchConfig()).cninfo("光启视界科技有限公司")
    query = parse_qs(urlsplit(calls[0][1]).query)
    assert query["searchkey"] == ["光启视界科技有限公司"] and query["isfulltext"] == ["true"]
    assert item["title"] == "合成科技：关于对外投资光启视界科技有限公司的公告"
    assert item["url"] == "https://static.cninfo.com.cn/finalpage/2025-10-01/1224.PDF"
    assert item["source"] == "巨潮资讯 · 合成科技" and item["published_at"].startswith("2025-10-01")
    assert item["extra"]["sec_code"] == "300999"


def test_cn_regulatory_uses_search_service_and_keeps_only_official_hosts():
    def handler(method, url, headers, body):
        query = json.loads(body)["query"]
        site = query.split()[0].removeprefix("site:")
        return Response(200, {}, json.dumps({"data": {"webPages": {"value": [
            {"name": f"{site} 登记信息", "url": f"https://www.{site}/info/1", "summary": "CTR2025xxxx 试验登记"},
            {"name": "转载", "url": "https://mirror.example/1", "summary": "无关"}]}}}).encode())

    http, calls = _web(handler)
    items = Collectors(http, SearchConfig("bocha", "k")).cn_regulatory("合成生物医药")
    assert {urlsplit(i["url"]).hostname for i in items} == {
        "www.chinadrugtrials.org.cn", "www.cde.org.cn", "www.nmpa.gov.cn"}
    assert all(i["kind"] == "trial" for i in items) and len(calls) == 3
    assert Collectors(http, SearchConfig()).cn_regulatory("x") == []  # no search key: nothing, no request


def test_cninfo_keeps_fulltext_evidence_when_the_title_does_not_name_the_target():
    record = {**CNINFO["announcements"][0], "announcementTitle": "关于向子公司增资的公告",
              "announcementTime": "1759276800000",
              "announcementContent": "向<em>光启视界</em>科技有限公司增资人民币2000万元。"}
    http, _ = _web(lambda *a: Response(200, {}, json.dumps({"announcements": [record]}).encode()))
    [item] = Collectors(http, SearchConfig()).cninfo("光启视界科技有限公司")
    assert "光启视界科技有限公司" not in item["title"]
    assert "全文检索摘录：向光启视界科技有限公司增资人民币2000万元。" in item["snippet"]
    assert "<em>" not in item["snippet"] and item["published_at"].startswith("2025-10-01")


def test_registry_changes_are_recent_unique_and_need_a_key():
    def handler(method, url, headers, body):
        assert headers["Authorization"] == "ty-key"
        return Response(200, {}, json.dumps({"error_code": 0, "result": {"items": [
            {"changeItem": "投资人(股权)变更", "changeTime": "2026-08-01", "contentBefore": "张三",
             "contentAfter": "张三;<em>红杉</em>中国"},
            {"changeItem": "注册资本变更", "changeTime": 1783900800000, "contentBefore": "1000万",
             "contentAfter": "1200万"},
            {"changeItem": "经营范围变更", "changeTime": "2015-01-01", "contentBefore": "a", "contentAfter": "b"},
        ]}}, ensure_ascii=False).encode())

    http, _ = _web(handler)
    assert Collectors(http, SearchConfig()).registry_changes("光启视界科技有限公司") == []
    items = Collectors(http, SearchConfig(), RegistryConfig("tianyancha", "ty-key")).registry_changes("光启视界科技有限公司")
    assert [i["title"] for i in items] == ["光启视界科技有限公司 工商变更：投资人(股权)变更",
                                           "光启视界科技有限公司 工商变更：注册资本变更"]
    assert items[0]["snippet"] == "变更前：张三；变更后：张三;红杉中国"
    assert len({i["key"] for i in items}) == 2 and items[1]["published_at"].startswith("2026-07")


def test_registry_error_is_reported_not_swallowed():
    http, _ = _web(lambda *a: Response(200, {}, b'{"error_code": 300000, "reason": "no data"}'))
    try:
        Collectors(http, SearchConfig(), RegistryConfig("tianyancha", "k")).registry_changes("x")
    except HttpError as exc:
        assert exc.code == "registry_error"
    else:
        raise AssertionError("expected registry_error")


def test_collect_reports_domestic_sources_separately(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    def handler(method, url, headers, body):
        if "cninfo" in url:
            return Response(200, {}, json.dumps(CNINFO, ensure_ascii=False).encode())
        if "tianyancha" in url:
            return Response(200, {}, json.dumps({"error_code": 0, "result": {"items": [
                {"changeItem": "法定代表人变更", "changeTime": "2026-09-01", "contentBefore": "甲", "contentAfter": "乙"}]}},
                ensure_ascii=False).encode())
        raise HttpError("connection_failed")

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / "settings.json"))
    http, _ = _web(handler)
    collectors = Collectors(http, SearchConfig(), RegistryConfig("tianyancha", "k"))
    plan = {"company_queries": ["光启视界"], "competitors": [{"name": "合成光电", "reason": ""}]}
    report = pipeline.collect(project.id, plan, collectors, lambda _: None,
                              {"legal_name": "光启视界科技有限公司"})
    assert report["filing"]["added"] == 1 and report["filing"]["checked"] == 2  # same notice found twice
    assert report["registry"]["added"] == 1
    titles = {i["title"] for i in store.untriaged(project.id)}
    assert "光启视界科技有限公司 工商变更：法定代表人变更" in titles
    # Announcement PDFs and registry pages are judged on their metadata, never fetched as articles.
    assert pipeline.fetch_bodies(project.id, lambda _: None) == 0


def test_fulltext_filings_without_the_company_in_view_are_downgraded():
    from project_watch.studio.pipeline import company_names, guard_filing

    names = company_names({"legal_name": "光启视界科技有限公司", "short_name": "光启视界", "aliases": []})
    assert names == ["光启视界科技有限公司", "光启视界科技", "光启视界"]
    major = {"id": "x", "relevant": True, "importance": "major", "impact": "上市公司增资。"}
    fulltext = '{"matched": "fulltext"}'
    hidden = {"title": "合成电子：关于使用募集资金向全资子公司增资的公告", "snippet": "300888 合成电子 公告", "extra": fulltext}
    out = guard_filing(hidden, major, names)
    assert out["importance"] == "routine" and out["impact"] == "系统尚未从公告原文定位本公司，暂不据此判断。"
    periodic = {"title": "合成科技：2025年半年度报告", "extra": fulltext,
                "snippet": "全文检索摘录：向参股公司光启视界科技有限公司采购光学模组"}
    assert guard_filing(periodic, major, names)["importance"] == "notable"
    named = {"title": "合成科技：关于对外投资光启视界科技有限公司的公告", "snippet": "", "extra": fulltext}
    assert guard_filing(named, major, names) == major
    # Title-only competitor searches and ordinary news are untouched.
    assert guard_filing({**hidden, "extra": '{"matched": "title"}'}, major, names) == major
    assert guard_filing({"title": "无关", "snippet": "", "extra": "{}"}, major, names) == major


def test_pipeline_triage_applies_the_filing_rule(tmp_path):
    import re

    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    class AllMajor:
        ready = True

        def json(self, system, user, max_tokens=0):
            return {"results": [{"id": i, "relevant": True, "category": "company", "importance": "major",
                                 "summary": "s", "impact": ""} for i in re.findall(r"id=([0-9a-f]+)", user)]}

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / "settings.json"))
    store.add_items(project.id, "company", [
        {"key": "a", "title": "合成电子：关于向全资子公司增资的公告", "url": "https://static.cninfo.com.cn/a.PDF",
         "source": "巨潮资讯", "snippet": "300888 合成电子 公告", "published_at": None, "extra": {"matched": "fulltext"}},
        {"key": "b", "title": "合成科技：关于对外投资光启视界科技有限公司的公告", "url": "https://static.cninfo.com.cn/b.PDF",
         "source": "巨潮资讯", "snippet": "", "published_at": None, "extra": {"matched": "fulltext"}}])
    pipeline.triage(project.id, AllMajor(), {"legal_name": "光启视界科技有限公司", "short_name": "光启视界"},
                    {"competitors": []}, lambda _: None)
    by_key = {i["key"]: i for i in store.list_items(project_id=project.id)}
    assert by_key["a"]["importance"] == "routine" and "系统尚未从公告原文定位本公司" in by_key["a"]["impact"]
    assert by_key["b"]["importance"] == "major"


def _pdf(pages: list[str]) -> bytes:
    import io

    from reportlab.pdfgen.canvas import Canvas

    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    for text in pages:
        canvas.drawString(72, 720, text)
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def test_filing_passages_keep_only_paragraphs_that_name_the_company():
    from project_watch.studio.pipeline import filing_passages

    pdf = _pdf(["Cover page of the announcement", "The issuer will invest 50 million in Lumina Optics Ltd as a subsidiary."])
    text = filing_passages(pdf, ["Lumina Optics"])
    assert text.startswith("[第2页]") and "invest 50 million in Lumina Optics" in text
    assert filing_passages(pdf, ["Nobody Corp"]) == ""


def test_unclear_fulltext_filings_are_verified_against_the_official_pdf(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    pdfs = {"https://static.cninfo.com.cn/a.PDF": _pdf(["Capital increase into Lumina Optics Ltd, a subsidiary."]),
            "https://static.cninfo.com.cn/b.PDF": _pdf(["Annual report of another business."]),
            "https://static.cninfo.com.cn/c.PDF": _pdf(["Lumina Optics Ltd reports 2026Q1 deliveries of 90000 units."])}
    fetched = []

    def transport(method, url, headers, body, timeout):
        fetched.append(url)
        return Response(200, {}, pdfs[url]) if url in pdfs else Response(404, {}, b"")

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("Lumina")
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {"legal_name": "Lumina Optics Ltd", "short_name": "Lumina Optics"}, consent=True)
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / "settings.json"), http_factory=lambda s: Http(transport))
    hits = [{"key": k, "title": f"Issuer: notice {k}", "url": f"https://static.cninfo.com.cn/{k}.PDF", "source": "巨潮资讯",
             "snippet": "", "published_at": None, "extra": {"matched": "fulltext"}} for k in ("a", "b")]
    named = {"key": "c", "title": "Issuer: investment in Lumina Optics Ltd", "url": "https://static.cninfo.com.cn/c.PDF",
             "source": "巨潮资讯", "snippet": "", "published_at": None, "extra": {"matched": "fulltext"}}
    store.add_items(project.id, "company", [*hits, named])
    pipeline.fetch_bodies(project.id, lambda _: None)
    by_key = {i["key"]: i for i in store.untriaged(project.id)}
    assert by_key["a"]["body_status"] == "filing_text" and "Capital increase into Lumina Optics" in by_key["a"]["body"]
    assert by_key["b"]["body_status"] == "filing_no_mention"
    assert by_key["c"]["body_status"] == "filing_text" and "deliveries of 90000 units" in by_key["c"]["body"]
    assert set(fetched) == set(pdfs)
