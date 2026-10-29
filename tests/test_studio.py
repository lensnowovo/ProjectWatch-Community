"""Studio: settings, model client, collectors and the name/BP -> monitoring journey.

Tests use fake transports and trimmed public live-response replays. No network
or real credentials are used during the test run.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from project_watch.studio.http import Http, HttpError, Response, system_proxies, urllib_transport
from project_watch.studio.llm import LLM, ModelError, _extract_json
from project_watch.studio.settings import ModelConfig, SearchConfig, SettingsStore
from project_watch.studio.sources import Collectors, _iso

GOOGLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>
<item><title>Lumina Optics closes Series B - 36Kr</title><link>https://news.google.com/rss/articles/abc</link>
<guid>abc</guid><pubDate>Mon, 05 Oct 2026 08:00:00 GMT</pubDate><description>x</description></item>
<item><title>Unrelated Lumina bakery opens - Local News</title><link>https://news.google.com/rss/articles/def</link>
<guid>def</guid><pubDate>Sun, 04 Oct 2026 08:00:00 GMT</pubDate><description>y</description></item>
</channel></rss>"""

BING_RSS = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>b</title>
<item><title>Lumina Optics wins automaker nomination</title>
<link>https://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fexample.com%2fnews%2f1</link>
<description>Synthetic news</description><pubDate>Tue, 06 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Lumina Optics closes Series B</title>
<link>https://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2f36kr.example%2fnews%2fseries-b</link>
<description>Synthetic financing news</description><pubDate>Mon, 05 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Unrelated Lumina bakery opens</title>
<link>https://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2flocal.example%2fbakery</link>
<description>Synthetic local news</description><pubDate>Sun, 04 Oct 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

OPENALEX = {"results": [{"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/x",
                         "display_name": "Compact lidar optics", "publication_date": "2026-03-01",
                         "primary_location": {"source": {"display_name": "Optics Letters"}},
                         "authorships": [{"author": {"display_name": "A. Author"},
                                          "institutions": [{"display_name": "Lumina Optics"}]}],
                         "cited_by_count": 3}]}

TRIALS = {"studies": [{"protocolSection": {
    "identificationModule": {"nctId": "NCT01234567", "briefTitle": "Synthetic Phase 1 Study"},
    "statusModule": {"overallStatus": "RECRUITING", "lastUpdatePostDateStruct": {"date": "2026-09-01"}},
    "designModule": {"phases": ["PHASE1"]},
    "conditionsModule": {"conditions": ["Solid tumor"]},
    "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Synthetic Bio"}}}}]}

BP = """光启视界科技有限公司 商业计划书（合成数据）
公司成立于2019年，专注车载激光雷达光学模组。
财务情况（合并口径，人民币万元，实际数）
营业收入：2024年 3200；2025年 5800
净利润：2024年 -1500；2025年 -900
本轮拟融资2亿元，投前估值12亿元。
风险：客户验收延期。
"""


LIVE_FIXTURES = Path(__file__).parent / "fixtures" / "studio_live"


@pytest.mark.parametrize("path", sorted(LIVE_FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_public_live_response_replay(path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    meta, response = fixture["provenance"], fixture["response"]
    body = response["body"]
    raw = body.encode() if response["format"] == "xml" else json.dumps(body).encode()
    calls = []

    def replay(method, url, headers, payload, timeout):
        calls.append(url)
        assert url.split("?")[0] == meta["endpoint"]
        return Response(200, {}, raw)

    collectors = Collectors(Http(replay), SearchConfig())
    operation = meta["operation"]
    if operation == "google_news":
        hits = collectors.google_news(meta["company"])
        entries = ET.fromstring(body).findall("./channel/item")
        for hit, entry in zip(hits, entries, strict=True):
            outlet = entry.findtext("source")
            assert hit["source"] == outlet
            assert hit["title"] + f" - {outlet}" == entry.findtext("title")
        assert hits[0]["published_at"].startswith("2026-10-06T")
    elif operation == "bing_news":
        hits = collectors.bing_news(meta["company"])
        entries = ET.fromstring(body).findall("./channel/item")
        for hit, entry in zip(hits, entries, strict=True):
            wrapped = parse_qs(urlsplit(entry.findtext("link")).query)["url"][0]
            assert hit["url"] == wrapped
            assert hit["source"] == urlsplit(wrapped).netloc
            assert hit["published_at"].endswith("+00:00")
        assert "bing.com/news/apiclick" not in hits[0]["url"]
    elif operation.startswith("openalex_"):
        affiliation = operation.endswith("affiliation")
        hits = collectors.openalex("Moderna" if meta["company"] == "Moderna" else "Contemporary Amperex Technology",
                                   affiliation=affiliation)
        query = parse_qs(urlsplit(calls[0]).query)
        assert ("filter" in query) == affiliation
        if affiliation:
            assert query["filter"][0].startswith("raw_affiliation_strings.search:")
            affiliations = [s.lower() for w in body["results"] for a in w["authorships"]
                            for s in a.get("raw_affiliation_strings") or []]
            expected = "moderna" if meta["company"] == "Moderna" else "amperex"
            assert any(expected in s for s in affiliations)
        for hit, work in zip(hits, body["results"], strict=True):
            assert hit["title"] == work["display_name"]
            assert hit["url"] == work["doi"]
            assert hit["published_at"] == work["publication_date"] + "T00:00:00+00:00"
            assert hit["extra"]["cited_by"] == work["cited_by_count"]
    elif operation == "clinical_trials":
        hits = collectors.clinical_trials(sponsor="ModernaTX")
        first = hits[0]
        assert first["url"] == "https://clinicaltrials.gov/study/NCT07279766"
        assert first["published_at"] == "2026-10-06T00:00:00+00:00"
        assert first["extra"]["phase"] == "PHASE4"
        assert first["extra"]["sponsor"] == "Tor Biering-Sørensen"
        assert first["extra"]["collaborators"] == ["ModernaTX, Inc."]
        assert "合作方：ModernaTX, Inc." in first["snippet"]
    elif operation == "bocha":
        collectors.search_config = SearchConfig(operation, "synthetic-replay-key")
        hits = collectors.web_search(meta["company"])
        raw_first = body["data"]["webPages"]["value"][0]
        expected_title = ("AWS Innovator: Moderna Case Studies, Videos and Customer Stories"
                          if meta["company"] == "Moderna" else "宁德时代 _ 数据中心 _ 东方财富网")
        assert hits[0]["title"] == expected_title
        assert hits[0]["source"] == raw_first["siteName"]
        expected_date = ("2025-01-07T16:00:05+00:00" if meta["company"] == "Moderna"
                         else "2024-07-16T20:31:37+00:00")
        assert hits[0]["published_at"] == expected_date
        assert hits[0]["title"] and hits[0]["url"].startswith("https://")
    else:
        raise AssertionError(f"Unexpected live fixture operation: {operation}")
    assert len(calls) == 1
    assert len(hits) == meta["records"] == 4
    assert len({hit["key"] for hit in hits}) == 4


@pytest.mark.parametrize(("value", "expected"), [
    ("Tue, 11 Mar 2025 17:00:00 GMT", "2025-03-11T17:00:00+00:00"),
    ("2024-07-22T00:00:00+08:00", "2024-07-21T16:00:00+00:00"),
    ("2026-10-06", "2026-10-06T00:00:00+00:00"),
    ("2026-99-99", None), ("2 days ago", None), (None, None),
])
def test_source_date_formats(value, expected):
    assert _iso(value) == expected


def test_google_title_uses_source_metadata_and_preserves_internal_hyphens():
    body = b'''<rss><channel>
    <item><title>Moderna - Merck - Yahoo Finance</title><link>https://example.com/one</link>
    <source>Yahoo Finance</source></item>
    <item><title>Moderna - Merck</title><link>https://example.com/two</link>
    <source>Yahoo Finance</source></item>
    <item><title>Moderna</title><link>https://example.com/three</link></item>
    </channel></rss>'''
    hits = Collectors(Http(lambda *a: Response(200, {}, body)), SearchConfig()).google_news("q")
    assert [h["title"] for h in hits] == ["Moderna - Merck", "Moderna - Merck", "Moderna"]
    assert [h["source"] for h in hits] == ["Yahoo Finance", "Yahoo Finance", "Google News"]


def test_windows_system_proxy_survives_pw_proxy_environment(monkeypatch):
    import urllib.request

    registry = {"http": "http://127.0.0.1:17891", "https": "http://127.0.0.1:17891"}
    monkeypatch.setattr(urllib.request, "getproxies_environment",
                        lambda: {"pw": "http://127.0.0.1:1", "no": "localhost"})
    monkeypatch.setattr(urllib.request, "getproxies_registry", lambda: registry, raising=False)
    assert system_proxies() == {**registry, "no": "localhost"}
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {"https": "http://127.0.0.1:2"})
    assert system_proxies() == {"https": "http://127.0.0.1:2"}


def test_explicit_proxy_overrides_system_proxy(monkeypatch):
    import urllib.request

    seen = []
    monkeypatch.setattr(urllib.request, "build_opener", lambda handler, *extra: seen.append(handler.proxies))
    monkeypatch.setattr("project_watch.studio.http.system_proxies", lambda: {"https": "http://system.test:1"})
    urllib_transport("http://explicit.test:2")
    urllib_transport()
    assert seen == [{"http": "http://explicit.test:2", "https": "http://explicit.test:2"},
                    {"https": "http://system.test:1"}]


def test_live_probe_redacts_keys_nested_urls_and_proxy_credentials():
    from scripts.studio_live_probe import Redactor

    redactor = Redactor(["synthetic-private-value"])
    cleaned = redactor.clean({
        "Authorization": "Bearer synthetic-private-value", "Cookie": "private",
        "url": "https://user:password@example.com/article?api_key=private&public=ok",
        "wrapped": "https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fexample.com%2Fx%3Ftoken%3Dprivate",
        "title": "echo synthetic-private-value",
    })
    serialized = json.dumps(cleaned)
    assert "private-value" not in serialized and "password" not in serialized
    assert "private" not in serialized
    assert cleaned["url"] == "https://example.com/article?public=ok"
    assert cleaned["title"] == "echo [REDACTED]"


def test_live_probe_continues_after_failure_and_never_logs_credentials(monkeypatch, capsys):
    from argparse import Namespace

    from scripts.studio_live_probe import probe

    for name in ("PW_MODEL_BASE_URL", "PW_MODEL_PROTOCOL", "PW_MODEL_ID", "PW_MODEL_KEY",
                 "PW_SEARCH_PROVIDER", "PW_SEARCH_KEY", "PW_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PW_MODEL_KEY", "synthetic-sensitive-value")
    monkeypatch.setenv("PW_PROXY", "http://user:password@proxy.test:12")
    calls = []

    def failing(method, url, headers, body, timeout):
        calls.append(url)
        raise HttpError("connection_failed", detail="echo synthetic-sensitive-value user:password")

    monkeypatch.setattr("scripts.studio_live_probe.urllib_transport", lambda proxy: failing)
    args = Namespace(company="Synthetic Company", proxy_mode="env", timeout=1, fixtures_dir=None, resolve=False)
    assert probe(args) == 1
    output = capsys.readouterr().out
    assert "synthetic-sensitive-value" not in output and "password" not in output
    rows = [json.loads(line) for line in output.splitlines()]
    assert len(calls) == 5
    assert [r["status"] for r in rows[1:]] == ["failed"] * 5 + ["skipped"] * 3
    assert all("elapsed_seconds" in r and "count" in r and "first" in r for r in rows[1:])


def test_llm_reasoning_token_limit_has_safe_error_code():
    body = {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "private"}}]}
    llm = LLM(ModelConfig("openai", "https://model.test", "synthetic-model", "synthetic-key"),
              Http(lambda *a: Response(200, {}, json.dumps(body).encode())))
    with pytest.raises(ModelError) as caught:
        llm.complete("s", "u", max_tokens=20)
    assert caught.value.code == "model_output_truncated"
    assert caught.value.status == 200
    assert "private" not in str(caught.value)


def test_bocha_live_quota_error_is_not_a_bad_key():
    # Synthetic replay of the observed public service's error shape; no key/body dump.
    response = Response(403, {}, json.dumps({"code": "403", "message":
                                            "You do not have enough money or package quota"}).encode())
    with pytest.raises(HttpError) as caught:
        Collectors._ok(response)
    assert caught.value.code == "search_quota_exhausted"
    assert caught.value.status == 403
    with pytest.raises(HttpError, match="search_access_denied"):
        Collectors._ok(Response(403, {}, b"{}"))


@pytest.mark.parametrize(("provider", "body", "date"), [
    ("brave", {"web": {"results": [{"title": "Synthetic", "url": "https://example.com/one",
                                   "page_age": "2026-10-06", "meta_url": {"hostname": "example.com"}}]}},
     "2026-10-06T00:00:00+00:00"),
    ("tavily", {"results": [{"title": "Synthetic", "url": "https://example.com/one", "content": "Synthetic",
                              "published_date": "Tue, 11 Mar 2025 17:00:00 GMT"}]},
     "2025-03-11T17:00:00+00:00"),
])
def test_documented_search_shapes_are_offline_only(provider, body, date):
    def transport(method, url, headers, payload, timeout):
        if provider == "tavily":
            assert json.loads(payload)["include_published_date"] is True
        return Response(200, {}, json.dumps(body).encode())

    hits = Collectors(Http(transport), SearchConfig(provider, "synthetic-key")).web_search("q")
    assert hits[0]["source"] == "example.com"
    assert hits[0]["published_at"] == date


def test_resolve_falls_back_to_news_when_keyed_search_is_unavailable():
    from project_watch.studio.resolve import resolve_company
    from project_watch.studio.settings import RegistryConfig

    class FakeLLM:
        ready = True

        def json(self, system, user, **kwargs):
            assert "https://example.com/news" in user
            return {"candidates": [{"legal_name": "Synthetic Company"}]}

    class UnavailableSearch:
        has_search = True

        def web_search(self, query, **kwargs):
            raise HttpError("search_quota_exhausted", status=403)

        def bing_news(self, query):
            return [{"title": "Synthetic Company", "source": "Synthetic",
                     "url": "https://example.com/news", "snippet": "Synthetic evidence"}]

    result = resolve_company("Synthetic Company", llm=FakeLLM(), collectors=UnavailableSearch(),
                             http=Http(lambda *a: None), registry=RegistryConfig())
    assert result["candidates"][0]["legal_name"] == "Synthetic Company"
    assert "search_quota_exhausted" in result["notes"]


@pytest.mark.parametrize("bad_result", [[{"legal_name": "Synthetic"}], {"candidates": "Synthetic"}])
def test_resolve_rejects_wrong_model_shape_without_attribute_error(bad_result):
    from project_watch.studio.resolve import resolve_company
    from project_watch.studio.settings import RegistryConfig

    class FakeLLM:
        ready = True

        def json(self, *args, **kwargs):
            return bad_result

    collectors = Collectors(Http(FakeWeb()), SearchConfig())
    with pytest.raises(ValueError, match="candidates"):
        resolve_company("Synthetic", llm=FakeLLM(), collectors=collectors,
                        http=collectors.http, registry=RegistryConfig())


class FakeWeb:
    """Routes requests by host; the model endpoint answers by prompt type."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, str]] = []

    def model(self, system: str, user: str) -> dict | str:
        self.prompts.append((system, user))
        if "同一事件" in system:
            pairs = re.findall(r"\[(\d+)\] A：(.*)\n\s+B：(.*)", user)
            return {"results": [{"pair": int(n), "same": "Series B" in a and "Series B" in b}
                                for n, a, b in pairs]}
        if "连接测试" in system:
            return "OK"
        if "公开搜索摘录补充" in system:
            return {"facts": []}
        if "公司资料，不生成竞争判断" in system:
            return {"summary": "", "companies": [], "claims": []}
        if "行业环境" in system:
            return {"industry": "", "heat": "不确定", "facts": [], "metrics": [], "funding": [], "implications": []}
        if "工商主体" in system:
            return {"candidates": [{"legal_name": "光启视界科技有限公司", "short_name": "光启视界",
                                    "credit_code": "", "website": "", "founded": "2019",
                                    "location": "苏州", "industry": "激光雷达光学",
                                    "description": "车载激光雷达光学模组", "stage": "B轮",
                                    "aliases": ["Lumina Optics"], "confidence": 85,
                                    "reason": "新闻与 BP 一致", "evidence": []}],
                    "notes": ""}
        if "从商业计划书" in system:
            return {"name": "光启视界科技有限公司"}
        if "正在阅读" in system:
            markers = re.findall(r"\[(F\d+)\]", user)
            revenue = next(m for m in markers if "营业收入" in user.split(f"[{m}]")[1].split("\n")[0])
            return {"points": [{"section": "业务与产品", "text": "专注车载激光雷达光学模组", "ref": markers[1]}],
                    "metrics": [
                        {"name": "营业收入", "category": "financial", "period": "2024", "value": 3200,
                         "unit": "万元", "basis": "actual", "scope": "合并", "ref": revenue, "quote": "2024年 3200"},
                        {"name": "营业收入", "category": "financial", "period": "2025", "value": "5,800",
                         "unit": "万元", "basis": "actual", "scope": "合并", "ref": revenue, "quote": "2025年 5800"},
                        {"name": "幻觉指标", "period": "2025", "value": 1, "ref": "F999"}],
                    "milestones": [], "risks": [{"text": "客户验收延期", "ref": markers[-1]}],
                    "financing": [{"text": "拟融资2亿元，投前估值12亿元", "ref": markers[-2]}]}
        if "一页纸档案" in system:
            return {"one_liner": "车载激光雷达光学模组供应商", "industry": "激光雷达", "stage": "B轮",
                    "summary": "合成摘要。", "sections": [{"title": "业务与产品", "points": [
                        {"text": "专注车载激光雷达光学模组", "refs": ["F2", "F404"]}]}],
                    "highlights": [], "risks": [{"text": "客户验收延期", "refs": ["F7"]}],
                    "milestones": [], "questions": ["验收时间表？"]}
        if "监控方案" in system:
            return {"company_queries": ["光启视界"], "people": [], "competitors": [
                {"name": "合成竞品甲", "reason": "同类模组"}], "industry_queries": ["激光雷达"],
                "policy_queries": [], "paper_queries": [], "affiliation_names": ["Lumina Optics"],
                "is_biomed": True, "trial_sponsors": ["Synthetic Bio"], "patent_assignees": []}
        if "值得读取正文" in system:
            return {"results": [{"id": i["id"], "read": "bakery" not in i["title"]}
                                for i in json.loads(user)["items"]]}
        if "筛选" in system:
            ids = re.findall(r"id=([0-9a-f]+)", user)
            titles = re.findall(r"标题：(.*)", user)
            return {"results": [
                {"id": i, "source_title": t, "source_quote": t, "relevant": "bakery" not in t, "category": None,
                 "importance": "major" if "Series B" in t else "notable",
                 "summary": f"摘要：{t}", "impact": ""} for i, t in zip(ids, titles, strict=True)]}
        if "动态简报" in system:
            return {"headline": "完成 B 轮融资", "tone": "positive", "summary": "合成。",
                    "changes": [{"text": "完成 B 轮", "refs": ["N1"]}], "watch": []}
        from project_watch.studio import (
            business_profile,
            monitor,
            public_profile,
            signals,
            structured_facts,
        )

        if monitor.thread_system() in system:
            return {'assign': []}
        if signals._CROSS_SYSTEM in system or system.startswith("你在做投后核对："):
            return {'checks': []}
        if business_profile.SYSTEM in system:
            return {'products': []}
        if structured_facts.SYSTEM in system:
            return {}
        if public_profile.SYSTEM in system:
            return {'facts': []}
        from project_watch.studio import question_advisor
        if question_advisor._SUGGEST in system:
            return {'questions': [{'text': '量产交付是否按计划推进', 'label': '量产', 'queries': ['光启视界 量产'],
                                   'criteria': '量产或交付消息'}]}
        if question_advisor._ASSESS in system:
            return {'results': []}
        raise AssertionError(f"unexpected prompt: {system[:40]}")

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        if url.startswith("https://model.test/v1/chat/completions"):
            payload = json.loads(body)
            answer = self.model(payload["messages"][0]["content"], payload["messages"][1]["content"])
            content = answer if isinstance(answer, str) else "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"
            return Response(200, {}, json.dumps({
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}}).encode())
        if url.startswith("https://news.google.com/"):
            return Response(200, {}, GOOGLE_RSS)
        if url.startswith("https://www.bing.com/news/search"):
            return Response(200, {}, BING_RSS)
        if url.startswith("https://api.openalex.org/"):
            return Response(200, {}, json.dumps(OPENALEX).encode())
        if url.startswith("https://clinicaltrials.gov/"):
            return Response(200, {}, json.dumps(TRIALS).encode())
        if url.startswith('https://www.cninfo.com.cn/'):
            return Response(200, {}, b'{"announcements":[]}')
        raise HttpError("connection_failed")


# ---------------------------------------------------------------- units

@pytest.mark.parametrize("patch", [
    {"base_url": "https://model.example/v1"},
    {"base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"protocol": "anthropic"},
])
def test_model_endpoint_switch_does_not_reuse_previous_key(tmp_path, patch):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"model": {"protocol": "openai", "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                            "model": "glm-5.3", "api_key": "synthetic-previous-key"}})
    store.update({"model": patch})
    assert store.load().model.api_key == ""
    assert not store.load().model.ready
    store.update({"model": {"api_key": "synthetic-new-key"}})
    assert store.load().model.api_key == "synthetic-new-key"


def test_coding_preset_is_distinct_from_standard_api():
    from project_watch.studio.settings import MODEL_PRESETS

    presets = {p["id"]: p for p in MODEL_PRESETS}
    assert presets["glm-coding"]["base_url"] == "https://open.bigmodel.cn/api/coding/paas/v4"
    assert presets["glm"]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert presets["glm-coding"]["model"] == "glm-5.3"


def test_settings_masks_and_keeps_secrets(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"model": {"protocol": "openai", "base_url": "https://api.deepseek.com/v1/",
                            "model": "deepseek-chat", "api_key": "synthetic-secret"}})
    store.update({"model": {"model": "deepseek-reasoner", "api_key": ""}})
    loaded = store.load()
    assert loaded.model.api_key == "synthetic-secret"
    assert loaded.model.base_url == "https://api.deepseek.com/v1"
    view = SettingsStore.public_view(loaded)
    assert "secret" not in json.dumps(view)
    assert view["model"]["ready"] is True
    with pytest.raises(ValueError):
        store.update({"model": {"base_url": "http://insecure.example"}})
    with pytest.raises(ValueError):
        store.update({"search": {"provider": "unknown"}})
    with pytest.raises(ValueError):
        store.update({"proxy_url": "ftp://x"})


def test_extract_json_handles_fences_and_prose():
    assert _extract_json('好的\n```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('结果如下 {"a": [1, 2]} 以上') == {"a": [1, 2]}
    with pytest.raises(ModelError):
        _extract_json("没有 JSON")
    with pytest.raises(ModelError):
        _extract_json('[{"name":"顶层数组"}]')
    with pytest.raises(ModelError):
        _extract_json('[]')


@pytest.mark.parametrize("text", ['{"results":[{"id":"a","",""}]}',
                                  '```json\n{"results":[{"id":"a","",""}]}\n```',
                                  'result: {"results":[{"id":"a","",""}]} end'])
def test_complete_malformed_json_has_typed_error(text):
    with pytest.raises(ModelError) as caught:
        _extract_json(text)
    assert caught.value.code == "model_output_invalid_json"


@pytest.mark.parametrize("text", ['[]', '[{"name":"example"}]', 'null', '"text"'])
def test_non_object_json_has_typed_error(text):
    with pytest.raises(ModelError) as caught:
        _extract_json(text)
    assert caught.value.code == "model_output_invalid_shape"


def test_llm_anthropic_protocol_and_auth_error():
    seen = {}

    def transport(method, url, headers, body, timeout):
        seen["url"], seen["headers"] = url, headers
        return Response(200, {}, json.dumps({"content": [{"type": "text", "text": '{"ok": true}'}],
                                             "usage": {"input_tokens": 1, "output_tokens": 1}}).encode())

    llm = LLM(ModelConfig("anthropic", "https://api.anthropic.com", "claude-x", "key"), Http(transport))
    assert llm.json("s", "u") == {"ok": True}
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "key"

    denied = LLM(ModelConfig("openai", "https://m.test/v1", "m", "bad"),
                 Http(lambda *a: Response(401, {}, b"{}")))
    with pytest.raises(ModelError, match="Key"):
        denied.complete("s", "u")
    with pytest.raises(ModelError, match="尚未配置"):
        LLM(ModelConfig(), Http(lambda *a: None)).complete("s", "u")


def test_collectors_parse_free_sources():
    collectors = Collectors(Http(FakeWeb()), SearchConfig())
    news = collectors.google_news("光启视界")
    assert news[0]["title"] == "Lumina Optics closes Series B"
    assert news[0]["source"] == "36Kr"
    assert news[0]["published_at"].startswith("2026-10-05")
    bing = collectors.bing_news("光启视界")
    assert bing[0]["url"] == "https://example.com/news/1"
    papers = collectors.openalex("Lumina Optics", affiliation=True)
    assert papers[0]["kind"] == "paper" and papers[0]["extra"]["institutions"] == ["Lumina Optics"]
    trials = collectors.clinical_trials(sponsor="Synthetic Bio")
    assert trials[0]["url"].endswith("NCT01234567") and "PHASE1" in trials[0]["snippet"]
    assert collectors.patents("x") == []  # needs a search key


def test_bocha_search_parsing():
    def transport(method, url, headers, body, timeout):
        assert url == "https://api.bochaai.com/v1/web-search"
        assert headers["Authorization"] == "Bearer k"
        return Response(200, {}, json.dumps({"data": {"webPages": {"value": [
            {"name": "标题", "url": "https://a.example/1", "summary": "摘要", "siteName": "A",
             "datePublished": "2026-10-01T08:00:00+08:00"}]}}}).encode())

    hits = Collectors(Http(transport), SearchConfig("bocha", "k")).web_search("q")
    assert hits[0]["source"] == "A" and hits[0]["published_at"].startswith("2026-10-01")


class FakeReader:
    """Article reader double: one readable article, everything else behind a paywall."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def read(self, url):
        self.urls.append(url)
        if url == "https://example.com/news/1":
            return {"status": "article_body", "text": "光启视界宣布获得某新势力车企的量产定点，合同金额未披露。"}
        return {"status": "summary_only", "text": "", "error_code": "access_restricted"}


# ---------------------------------------------------------------- journey

@pytest.fixture
def studio(tmp_path):
    from project_watch.studio.app import create_studio_app

    web = FakeWeb()
    app = create_studio_app(tmp_path, start_worker=False)
    app.state.studio_pipeline.http_factory = lambda settings: Http(web)
    app.state.studio_pipeline.article_reader = FakeReader()
    client = TestClient(app)
    client.put("/api/studio/settings", json={"model": {
        "protocol": "openai", "base_url": "https://model.test/v1", "model": "m", "api_key": "k-123456789"}})
    return app, client, web


def drain(app):
    intake = app.state.intake_worker
    worker = app.state.studio_worker
    for _ in range(50):
        progressed = intake.process_one() is not None
        worker.schedule()
        progressed = worker.step() or progressed
        if not progressed:
            return
    raise AssertionError("jobs did not settle")


def test_name_or_bp_to_active_monitoring(studio):
    app, client, web = studio
    assert client.post("/api/studio/settings/test").json()["model"]["ok"] is True

    draft = client.post("/api/studio/drafts", files={"file": ("bp.txt", BP.encode(), "text/plain")}).json()
    assert draft["readable"] is True

    resolved = client.post("/api/studio/resolve", json={"name": "", "draft_id": draft["draft_id"]}).json()
    assert resolved["query"] == "光启视界科技有限公司"
    candidate = resolved["candidates"][0]
    assert candidate["legal_name"] == "光启视界科技有限公司"

    refused = client.post("/api/studio/projects", json={
        "entity": {**candidate, "entity_source": "ai_search"}, "consent": False})
    assert refused.status_code == 422
    entity = {k: candidate[k] for k in ("legal_name", "short_name", "founded", "location",
                                        "industry", "description", "stage", "aliases")}
    created = client.post("/api/studio/projects", json={
        "entity": {**entity, "entity_source": "ai_search"}, "consent": True,
        "draft_ids": [draft["draft_id"]]})
    assert created.status_code == 201
    project = created.json()
    assert project["activated"] and project["name"] == "光启视界"

    drain(app)
    detail = client.get(f"/api/studio/projects/{project['id']}").json()
    assert detail["documents"][0]["status"] == "completed"
    profile = detail["profile"]
    assert profile["one_liner"] == "车载激光雷达光学模组供应商"
    refs = profile["sections"][0]["points"][0]["refs"]
    assert len(refs) == 1 and refs[0]["source_name"] == "bp.txt"  # invented F404 dropped
    revenue = sorted((m["period"], m["value"]) for m in detail["metrics"] if m["name"] == "营业收入")
    assert revenue == [("2024", 3200.0), ("2025", 5800.0)]
    assert all(m["name"] != "幻觉指标" for m in detail["metrics"])
    assert detail["brief"]["headline"] == "完成 B 轮融资"
    assert detail["brief"]["changes"][0]["refs"][0]["title"]
    assert detail["plan"]["competitors"][0]["name"] == "合成竞品甲"
    assert detail['project']['last_external_at'], [(j['kind'], j['status'], j['error']) for j in detail['jobs']]
    # M2: the first complete round suggests tracked questions (BP + name entry, one advisor call).
    assert [q['origin'] for q in client.get(f"/api/studio/projects/{project['id']}/questions").json()['questions']] == ['ai']
    assert {"company", "paper", "trial"} <= set(detail["counts"])
    assert detail["project"]["industry"] == "激光雷达光学"

    feed = client.get("/api/studio/feed").json()
    titles = [i["title"] for i in feed]
    assert "Unrelated Lumina bakery opens" not in titles  # namesake filtered by triage
    major = client.get("/api/studio/feed", params={"importance": "major"}).json()
    assert major and all(i["importance"] == "major" for i in major)

    item = feed[0]["id"]
    assert client.patch(f"/api/studio/items/{item}", json={"status": "dismissed"}).status_code == 200
    assert item not in [i["id"] for i in client.get("/api/studio/feed").json()]

    listing = client.get("/api/studio/projects").json()
    assert listing[0]["headline"] == "完成 B 轮融资" and listing[0]["documents"] == 1

    # Article text reached triage; Google News is off by default (v30) and its
    # redirect links are never fetched; Bing direct publisher links are read.
    reader = app.state.studio_pipeline.article_reader
    assert sorted(reader.urls) == ["https://36kr.example/news/series-b", "https://example.com/news/1"]
    assert not any("news.google.com" in u for u in reader.urls)
    triage_prompts = [u for s_, u in web.prompts if "筛选" in s_]
    assert any("正文摘录：光启视界宣布获得" in u for u in triage_prompts)

    events = client.get("/api/studio/events", params={"project_id": project["id"]}).json()
    assert events and all(e["source_count"] >= 1 for e in events)
    assert sum(e["source_count"] for e in events) == len(client.get(
        "/api/studio/feed", params={"project_id": project["id"], "limit": 200}).json()) + 1  # +1 dismissed
    target = events[0]
    assert client.patch(f"/api/studio/events/{target['id']}", json={"status": "dismissed"}).status_code == 200
    assert target["id"] not in [e["id"] for e in client.get("/api/studio/events").json()]

    usage = client.get("/api/studio/usage").json()["total"]
    assert usage["calls"] >= 5 and usage["input_tokens"] >= 500
    assert not any("secret" in url for _, url in web.calls)


def test_without_model_monitoring_still_collects(tmp_path):
    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    app.state.studio_pipeline.http_factory = lambda settings: Http(FakeWeb())
    app.state.studio_pipeline.article_reader = FakeReader()
    client = TestClient(app)
    resolved = client.post("/api/studio/resolve", json={"name": "光启视界"}).json()
    assert resolved["method"] == "manual"
    project = client.post("/api/studio/projects", json={
        "entity": {"legal_name": "光启视界"}, "consent": True}).json()
    drain(app)
    detail = client.get(f"/api/studio/projects/{project['id']}").json()
    assert detail["jobs"][0]["status"] == "done"
    assert detail["counts"].get("company", 0) >= 2
    assert detail["profile"] is None


def test_spa_routes_and_classic_ui(studio):
    _app, client, _ = studio
    assert client.get("/").status_code == 200
    assert client.get("/projects/abc").status_code == 200
    assert "Project Watch" in client.get("/classic").text
    assert client.get("/api/runtime").json()["parser"] == "subprocess"


# ---------------------------------------------------------------- events

def _item(i, title, *, when="2026-10-05T08:00:00+00:00", category="company", importance="notable",
          summary=""):
    return {"id": f"i{i}", "title": title, "published_at": when, "collected_at": when,
            "category": category, "importance": importance, "summary": summary, "impact": ""}


def test_three_outlets_reporting_one_financing_become_one_event():
    from project_watch.studio import events as ev

    items = [
        _item(1, "光启视界完成3亿元B轮融资，红杉领投", importance="major"),
        _item(2, "光启视界完成3亿元B轮融资 红杉中国领投", when="2026-10-05T12:00:00+00:00"),
        _item(3, "36氪首发｜光启视界完成3亿元B轮融资", when="2026-10-06T02:00:00+00:00"),
        _item(4, "光启视界发布新一代接收透镜组", when="2026-10-06T03:00:00+00:00"),
    ]
    groups, _ = ev.plan_groups(items, [])
    sizes = sorted(len(g.items) for g in groups)
    assert sizes == [1, 3]
    merged = next(g for g in groups if len(g.items) == 3)
    summary = ev.summarise(merged)
    assert summary["importance"] == "major" and summary["title"].startswith("光启视界完成3亿元")


def test_different_amounts_rounds_or_far_apart_dates_never_merge():
    from project_watch.studio import events as ev

    two = [_item(1, "光启视界完成2亿元B轮融资"), _item(2, "光启视界完成5亿元B轮融资")]
    assert len(ev.plan_groups(two, [])[0]) == 2
    rounds = [_item(1, "光启视界完成B轮融资"), _item(2, "光启视界完成C轮融资")]
    assert len(ev.plan_groups(rounds, [])[0]) == 2
    far = [_item(1, "光启视界完成3亿元B轮融资"),
           _item(2, "光启视界完成3亿元B轮融资", when="2026-11-20T08:00:00+00:00")]
    assert len(ev.plan_groups(far, [])[0]) == 2
    other_category = [_item(1, "激光雷达价格战持续"), _item(2, "激光雷达价格战持续", category="industry")]
    assert len(ev.plan_groups(other_category, [])[0]) == 2
    assert ev.conflicting("融资3亿元", "融资 300 million") is False  # same amount, different units


def test_new_report_joins_existing_event_and_upgrades_importance(tmp_path):
    from datetime import datetime, timedelta

    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(tmp_path / "db.sqlite3")
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / "settings.json"))
    llm = LLM(ModelConfig(), Http(lambda *a: None))  # no model: deterministic grouping only

    def add(key, title, importance, when):
        store.add_items(project.id, "company", [{"key": key, "title": title, "url": f"https://a.example/{key}",
                                                 "source": "A", "snippet": "", "published_at": when}])
        item = next(i for i in store.untriaged(project.id) if i["key"] == key)
        store.save_triage([{"id": item["id"], "relevant": True, "importance": importance,
                            "summary": title, "impact": "影响" if importance == "major" else ""}])

    # Keep both reports inside the production lookback window as the calendar advances.
    today = datetime.now(UTC)
    add("a", "光启视界获某车企量产定点", "notable", (today - timedelta(days=2)).isoformat())
    pipeline.cluster(project.id, llm, lambda _s: None)
    [event] = store.list_events(project_id=project.id)
    store.set_event_status(event["id"], "read")
    add("b", "光启视界获某车企量产定点，首年订单可期", "major", (today - timedelta(days=1)).isoformat())
    pipeline.cluster(project.id, llm, lambda _s: None)
    [event] = store.list_events(project_id=project.id)
    assert event["source_count"] == 2 and event["importance"] == "major"
    assert event["status"] == "new" and event["impact"] == "影响"


def test_ambiguous_pair_is_merged_only_when_model_confirms(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("Lumina")
    store = StudioStore(tmp_path / "db.sqlite3")
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / "settings.json"))
    web = FakeWeb()
    llm = LLM(ModelConfig("openai", "https://model.test/v1", "m", "k"), Http(web))
    titles = ["Lumina Optics closes Series B led by Sequoia", "Lumina raises Series B to scale lidar optics",
              "Lumina Optics opens new office in Shanghai"]
    store.add_items(project.id, "company", [
        {"key": f"k{n}", "title": t, "url": f"https://a.example/{n}", "source": "A", "snippet": "",
         "published_at": "2026-10-05T08:00:00+00:00"} for n, t in enumerate(titles)])
    store.save_triage([{"id": i["id"], "relevant": True, "importance": "notable", "summary": i["title"],
                        "impact": ""} for i in store.untriaged(project.id)])
    pipeline.cluster(project.id, llm, lambda _s: None)
    events = store.list_events(project_id=project.id)
    assert sorted(e["source_count"] for e in events) == [1, 2]
    assert any("同一事件" in s_ for s_, _ in web.prompts)


def test_article_reader_refuses_private_addresses():
    from project_watch.feeds import GuardedHTTPS
    from project_watch.public_article import ArticleReader

    reader = ArticleReader(transport=GuardedHTTPS(resolver=lambda host: ["10.0.0.7"],
                                                  connector=lambda *a: (200, {}, b"<p>x</p>")))
    result = reader.read("https://intranet.example.com/a")
    assert result["status"] == "unavailable" and result["error_code"] == "disallowed_address"


@pytest.mark.parametrize("first,second", [
    ("融资2亿元B轮，估值20亿元", "融资5亿元B轮，估值20亿元"),
    ("融资2亿元人民币", "融资2亿美元"),
    ("完成Series B融资", "完成Series B+融资"),
    ("完成Pre-A轮融资", "完成A轮融资"),
])
def test_conflicting_facts_in_summaries_block_both_item_orders(first, second):
    from project_watch.studio import events as ev

    for a, b in ((first, second), (second, first)):
        groups, ambiguous = ev.plan_groups([
            _item(1, "光启视界宣布融资进展", summary=a),
            _item(2, "光启视界宣布融资进展", summary=b),
        ], [])
        assert len(groups) == 2 and not ambiguous
    assert not ev.conflicting("融资1,000万元", "融资10 million")


def test_existing_report_summary_still_guards_amounts(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio import events as ev
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    store.add_items(project.id, "company", [{"key": "old", "title": "光启视界融资进展",
                     "url": "https://a.example/old", "snippet": "", "source": "A", "published_at": None}])
    item = store.untriaged(project.id)[0]
    store.save_triage([{"id": item["id"], "relevant": True, "summary": "完成2亿元B轮融资"}])
    groups, _ = ev.plan_groups(store.unclustered(project.id), [])
    event = ev.summarise(groups[0])
    # Stored event may have a shorter headline/summary than its underlying report.
    event["summary"] = "融资进展"
    store.create_event(project.id, event, [item["id"]])
    groups, ambiguous = ev.plan_groups([
        _item(2, "光启视界融资进展", summary="完成5亿元B轮融资", when=item["collected_at"])
    ], store.recent_events(project.id))
    assert len(groups) == 2 and not ambiguous


def test_boilerplate_patent_headlines_do_not_merge_different_inventions():
    from project_watch.studio import events as ev

    items = [
        _item(1, '光启视界获得发明专利授权：“合成电池装置和用电装置”', category="patent"),
        _item(2, '光启视界获得实用新型专利授权：“合成电池单体和用电装置”', category="patent"),
        _item(3, '光启视界获得实用新型专利授权：“合成焊接装置及电池系统”', category="patent"),
        _item(4, '光启视界获得发明专利授权：“合成电池装置和用电装置”', category="patent"),
    ]
    groups, _ = ev.plan_groups(items, [])
    assert sorted(len(g.items) for g in groups) == [1, 1, 2]
    assert ev.conflicting("patent CN1234567B", "patent CN7654321B")


def test_timezone_variations_do_not_crash_or_expand_merge_window():
    from project_watch.studio import events as ev

    assert ev.close_in_time("2026-10-05T08:00:00", "2026-10-05T16:00:00+08:00")
    assert not ev.close_in_time("2026-10-05", "2026-10-09T00:00:00Z")
    assert ev.parse_time("not a date") is None


@pytest.mark.parametrize("base,model,disabled", [
    ("https://api.deepseek.com", "deepseek-flash", True),
    ("https://api.deepseek.com/v1", "deepseek-v4-pro", True),
    ("https://model.test/v1", "deepseek-flash", False),
    ("https://api.deepseek.com", "other", False),
])
def test_only_documented_deepseek_json_calls_disable_thinking(base, model, disabled):
    payloads = []

    def transport(method, url, headers, body, timeout):
        payloads.append(json.loads(body))
        return Response(200, {}, json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode())

    llm = LLM(ModelConfig("openai", base, model, "synthetic-key"), Http(transport))
    assert llm.json("Extract facts", "Synthetic report") == {"ok": True}
    assert (payloads[0].get("thinking") == {"type": "disabled"}) is disabled
    llm.complete("General completion", "Synthetic question")
    assert "thinking" not in payloads[1]


@pytest.mark.parametrize("response", ["all", [], {"results": [{"pair": 21, "same": True}]}])
def test_ambiguous_chains_never_lose_reports_or_accept_wrong_pair_numbers(tmp_path, response):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("Lumina")
    store = StudioStore(repo.db_path)
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / "settings.json"))
    # B overlaps A, C overlaps B but has no overlap with A: both are model candidates.
    titles = ["abcdefgh", "efghijkl", "ijklmnop"]
    store.add_items(project.id, "company", [
        {"key": str(n), "title": t, "url": f"https://a.example/{n}", "snippet": "", "source": "A", "published_at": None}
        for n, t in enumerate(titles)])
    store.save_triage([{"id": i["id"], "relevant": True, "summary": i["title"]}
                       for i in store.untriaged(project.id)])

    class Judge:
        ready = True

        def json(self, *_args, **_kwargs):
            return {"results": [{"pair": 1, "same": True}, {"pair": 2, "same": True}]} if response == "all" else response

    pipeline.cluster(project.id, Judge(), lambda _: None)
    events = store.list_events(project_id=project.id)
    assert sum(e["source_count"] for e in events) == 3
    assert not store.unclustered(project.id)
    assert len(events) == (1 if response == "all" else 3)


def test_google_redirects_do_not_starve_direct_article_reading(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    reader = FakeReader()
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / "settings.json"), article_reader=reader)
    store.add_items(project.id, "company", [
        {"key": f"g{n}", "title": "光启视界新消息", "url": f"https://news.google.com/rss/articles/{n}",
         "source": "A", "snippet": "", "published_at": "2026-10-06T00:00:00Z"} for n in range(40)])
    store.add_items(project.id, "company", [{"key": "direct", "title": "光启视界报道",
        "url": "https://example.com/news/1", "source": "B", "snippet": "",
        "published_at": "2026-10-05T00:00:00Z"}])
    store.add_items(project.id, "company", [{"key": "malformed", "title": "损坏链接",
        "url": "https://[broken", "source": "Synthetic", "snippet": "", "published_at": None}])
    assert pipeline.fetch_bodies(project.id, lambda _: None) == 1
    assert reader.urls == ["https://example.com/news/1"]


def legacy_report_database(tmp_path):
    import sqlite3

    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import SCHEMA

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    # SCHEMA defines the pre-events items table; upgrade adds the new columns.
    with sqlite3.connect(repo.db_path) as db:
        db.executescript(SCHEMA)
        for status in ("new", "read", "starred", "dismissed"):
            db.execute("""INSERT INTO studio_items (id,project_id,key,category,title,collected_at,
                       triaged,relevant,status) VALUES (?,?,?,'company',?, ?,1,1,?)""",
                       (status, project.id, status, f"光启视界 {status} 进展", "2026-10-05T00:00:00Z", status))
    return repo, project


def test_legacy_reports_survive_upgrade_without_network_or_status_changes(tmp_path):
    from project_watch.studio.store import StudioStore

    repo, project = legacy_report_database(tmp_path)
    store = StudioStore(repo.db_path)
    events = store.list_events(project_id=project.id, include_dismissed=True)
    assert {e["status"] for e in events} == {"new", "read", "starred", "dismissed"}
    assert sum(e["source_count"] for e in events) == 4
    assert store.category_counts(project.id) == {"company": 3}
    # Once the schema is current, opening must not promote a new ungrouped item.
    with store._connect() as db:
        db.execute("INSERT INTO studio_items (id,project_id,key,category,title,collected_at,triaged,relevant) "
                   "VALUES ('pending',?,'pending','company','Unpublished candidate','2026-10-29',1,1)", (project.id,))
        db.commit()
    assert len(StudioStore(repo.db_path).list_events(project_id=project.id, include_dismissed=True)) == 4
    with store._connect() as db:
        assert db.execute("SELECT event_id FROM studio_items WHERE id='pending'").fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM studio_event_revisions').fetchone()[0] == 4
        assert db.execute('SELECT COUNT(*) FROM studio_event_revision_heads').fetchone()[0] == 4


def test_interrupted_legacy_event_migration_retries_atomically_without_touching_jobs(tmp_path, monkeypatch):
    import sqlite3

    from project_watch.studio import event_revisions
    from project_watch.studio.execution import SCHEMA as EXECUTION_SCHEMA
    from project_watch.studio.store import StudioStore

    repo, project = legacy_report_database(tmp_path)
    with sqlite3.connect(repo.db_path) as db:
        db.executescript(EXECUTION_SCHEMA)
        for name, ddl in [('lease_token', 'TEXT'), ('lease_until', 'TEXT'), ('attempts', 'INTEGER')]:
            db.execute(f'ALTER TABLE studio_jobs ADD COLUMN {name} {ddl}')
        db.execute("INSERT INTO studio_jobs (id,project_id,kind,status,created_at,lease_token,lease_until,attempts) "
                   "VALUES ('saved-job',?,'external','running','2026-10-05','saved-lease','2099-01-01',3)",
                   (project.id,))
        db.execute("INSERT INTO studio_job_calls (job_id,call_key,kind,provider,status,created_at) "
                   "VALUES ('saved-job','saved-request','model','synthetic','unknown','2026-10-05')")
        jobs = db.execute('SELECT id,status,lease_token,lease_until,attempts FROM studio_jobs').fetchall()
        calls = db.execute('SELECT * FROM studio_job_calls').fetchall()
    original = event_revisions.record

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('interrupted after first legacy revision')

    with monkeypatch.context() as patch:
        patch.setattr(event_revisions, 'record', interrupted)
        with pytest.raises(RuntimeError, match='interrupted after first legacy revision'):
            StudioStore(repo.db_path)
    with sqlite3.connect(repo.db_path) as db:
        assert 'event_id' not in {r[1] for r in db.execute('PRAGMA table_info(studio_items)')}
        for table in ['studio_events', 'studio_event_revisions', 'studio_event_revision_heads']:
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert db.execute('SELECT id,status,lease_token,lease_until,attempts FROM studio_jobs').fetchall() == jobs
        assert db.execute('SELECT * FROM studio_job_calls').fetchall() == calls
    first = StudioStore(repo.db_path)
    events = first.list_events(project_id=project.id, include_dismissed=True)
    assert {e['status'] for e in events} == {'new', 'read', 'starred', 'dismissed'}
    second = StudioStore(repo.db_path)
    assert {e['id'] for e in second.list_events(project_id=project.id, include_dismissed=True)} == {e['id'] for e in events}
    with sqlite3.connect(repo.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM studio_event_revisions').fetchone()[0] == 4
        assert db.execute('SELECT id,status,lease_token,lease_until,attempts FROM studio_jobs').fetchall() == jobs
        names = ['job_id', 'call_key', 'kind', 'provider', 'status', 'response_status', 'response_headers',
                 'response_body', 'created_at']
        assert db.execute('SELECT '+','.join(names)+' FROM studio_job_calls').fetchall() == calls
