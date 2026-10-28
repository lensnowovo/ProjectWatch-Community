"""Synthetic DeepSeek fixtures only; no live services or credentials."""
import json

import pytest

from project_watch.studio.app import create_studio_app
from project_watch.studio.dsh_bootstrap import PublicBootstrap
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import ModelConfig, SearchConfig, SettingsStore, StudioSettings
from project_watch.studio.sources import Collectors


def response(data, status=200):
    return Response(status, {}, json.dumps(data, ensure_ascii=False).encode())


def page(**updates):
    return {"title": "Synthetic Optics 产品", "url": "https://example.com/product#page2",
            "summary": "Synthetic Optics 开发光学模组。", "content": "2025年意向订单500万元。",
            "date": "2026-01-01T00:00:00Z", **updates}


def collector(handler, mode="auto"):
    seen = []
    def send(method, url, headers, body, timeout):
        payload = json.loads(body or b"{}")
        seen.append((url, payload))
        return handler(url, payload)
    config = SearchConfig(providers=["bocha", "tavily", "deepseek"],
                          keys={p: "synthetic-key" for p in ("bocha", "tavily", "deepseek")}, mode=mode)
    return Collectors(Http(send), config), seen


def test_deepseek_preferred_content_retained_date_not_promoted():
    c, seen = collector(lambda *a: response({"results": [page()]}))
    hit = c.web_search("Synthetic Optics", count=10)[0]
    assert len(seen) == 1 and seen[0][0].endswith("/tools/search")
    assert seen[0][1] == {"query": "Synthetic Optics", "count": 10, "need_content": True}
    assert hit["extra"]["search_content"] == "2025年意向订单500万元。"
    assert hit["published_at"] is None
    assert hit["extra"]["provider_date"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("query,expected", [("中文", "bochaai"), ("English", "tavily")])
def test_empty_deepseek_falls_back_by_language(query, expected):
    c, seen = collector(lambda url, _: response({"results": []}) if "deepseek" in url else
        response({"results": [{"title": "Synthetic", "url": "https://example.com"}],
                  "data": {"webPages": {"value": [{"name": "Synthetic", "url": "https://example.com"}]}}}))
    assert c.web_search(query)
    assert len(seen) == 2 and expected in seen[1][0]


@pytest.mark.parametrize("code", ["request_outcome_unknown", "connection_failed", "response_too_large", "unsafe_redirect"])
def test_ambiguous_search_never_falls_back(code):
    def fail(*_):
        raise HttpError(code)
    c, seen = collector(fail)
    with pytest.raises(HttpError, match=code):
        c.web_search("test")
    assert len(seen) == 1


@pytest.mark.parametrize("status,code", [(401, "search_key_invalid"), (402, "search_quota_exhausted"),
                                       (429, "http_failure"), (504, "http_failure")])
def test_known_error_codes(status, code):
    c, seen = collector(lambda *_: response({}, status))
    c.search_config.providers = ["deepseek"]
    with pytest.raises(HttpError, match=code):
        c.web_search("test")
    assert len(seen) == 1


def test_bad_query_is_not_dispatched_and_malformed_response_is_not_empty_success():
    c, seen = collector(lambda *_: response({"results": "wrong"}))
    c.search_config.providers = ["deepseek"]
    with pytest.raises(HttpError, match="search_query_invalid"):
        c.web_search("字" * 129)
    assert not seen
    with pytest.raises(HttpError, match="invalid_search_response"):
        c.web_search("test")


def test_malformed_field_is_a_known_source_error_not_attribute_crash():
    c, _ = collector(lambda *_: response({"results": [page(title={"wrong": "shape"})]}))
    c.search_config.providers = ["deepseek"]
    with pytest.raises(HttpError, match="invalid_search_response"):
        c.web_search("test")


def test_combined_and_legacy_modes_preserved():
    c, seen = collector(lambda *_: response({"results": []}), mode="combined")
    c.web_search("test")
    assert len(seen) == 3
    c.search_config.providers = ["bocha", "tavily"]
    c.search_config.mode = "auto"
    seen.clear()
    c.web_search("中文")
    assert "bochaai" in seen[0][0]


def make_loop(tmp_path, content, *, no_content=False):
    app = create_studio_app(tmp_path, start_worker=False)
    pipe = app.state.studio_pipeline
    p = pipe.repository.create_project("Synthetic Optics")
    pipe.store.upsert_project(p.id, {"legal_name": "Synthetic Optics"}, consent=True)
    pipe.store.enqueue(p.id, "external")
    job = pipe.store.claim()
    execution = JobExecution(pipe.store, job)
    calls = []
    def transport(method, url, headers, body, timeout):
        calls.append(url)
        return response({"content": content}) if url.endswith("/fetch") else response(
            {"results": [page(content="" if no_content else content),
                         page(url="https://example.com/product#page3", content="" if no_content else content)]})
    http = execution.http(Http(transport), lambda url: ("page" if url.endswith("/fetch") else "search", "synthetic"))
    class Reader:
        def read(self, url):
            raise AssertionError("cached body must not request another origin")
    c = Collectors(http, SearchConfig("deepseek", "synthetic-key"))
    return PublicBootstrap(pipe, execution, c, Reader()), calls


def test_cached_body_is_windowed_without_losing_tail_or_requesting_again(tmp_path):
    text = "甲" * 30005 + "2025年意向订单500万元。"
    loop, calls = make_loop(tmp_path, text)
    results = loop.tool("pw_search", {"query": "Synthetic Optics", "category": "company"})
    assert len(results["sources"]) == 1 and results["sources"][0]["body_available"]
    assert len(loop.source("S1")["locators"]) == 2
    assert text not in json.dumps(results, ensure_ascii=False)
    window = loop.tool("pw_read", {"source_id": "S1"})
    assert len(window["excerpt"]) == 12000 and window["next_offset"] == 12000
    tail = loop.tool("pw_read", {"source_id": "S1", "offset": 29995})
    assert tail["excerpt"].endswith("2025年意向订单500万元。") and tail["next_offset"] is None
    loop.tool("pw_record", {"source_id": "S1", "kind": "business", "label": "订单",
                           "quote": "2025年意向订单500万元。", "text": "2025年意向订单500万元。"})
    assert len(calls) == 1 and tail["read_scope"] == "partial"
    from project_watch.studio.dsh_bootstrap import RecordValidationError
    with pytest.raises(RecordValidationError) as error:
        loop.tool("pw_record", {"source_id": "S1", "kind": "business", "label": "订单",
                               "quote": "2025年意向订单500万元。", "text": "2025年订单500万元。"})
    assert any(issue["code"] == "missing_qualifications" for issue in error.value.issues)
    assert loop.execution.usage()["search"]["requests"] == 1


def test_fetch_cached_and_journaled_separately(tmp_path):
    loop, calls = make_loop(tmp_path, "Synthetic Optics 开发光学模组。", no_content=True)
    loop.tool("pw_search", {"query": "Synthetic Optics", "category": "company"})
    loop.tool("pw_read", {"source_id": "S1"})
    loop.tool("pw_read", {"source_id": "S1"})
    assert len(calls) == 2 and calls[-1].endswith("/fetch")
    assert loop.execution.usage()["page"]["requests"] == 1


def test_settings_keep_old_keys_and_mask_new_key(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"search": {"providers": ["bocha"], "keys": {"bocha": "synthetic-old"}}})
    store.update({"search": {"providers": ["deepseek", "bocha"], "keys": {"deepseek": "synthetic-new"}}})
    config = store.load()
    assert config.search.credentials() == {"bocha": "synthetic-old", "deepseek": "synthetic-new"}
    store.update({"search": {"providers": ["deepseek", "bocha", "tavily", "brave"]}})
    assert len(store.load().search.enabled()) == 4


def test_deepseek_project_cache_survives_restart(tmp_path):
    from project_watch.studio.query_ledger import ProjectQueryCache

    loop, calls = make_loop(tmp_path, "甲" * 45000)
    signature = ("deepseek", "synthetic-account", "Synthetic", False, 10)
    cache = ProjectQueryCache(loop.store, loop.project_id, purpose="external")
    cache.get(signature, lambda: loop.collectors.web_search("Synthetic"))
    restored = ProjectQueryCache(loop.store, loop.project_id, purpose="competition")
    hits, cached = restored.get(signature, lambda: pytest.fail("must reuse completed search"))
    assert cached and len(hits[0]["extra"]["search_content"]) == 45000 and len(calls) == 1


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "http://[::1]/a", "https://service.internal/a",
                                "https://user:password@example.com/a"])
def test_fetch_rejects_private_or_credential_urls(url):
    c, seen = collector(lambda *_: pytest.fail("unsafe request"))
    with pytest.raises(HttpError, match="disallowed_url"):
        c.deepseek_fetch(url)
    assert not seen


def test_model_and_search_on_same_host_are_classified_separately(tmp_path):
    pipe = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipe.repository.create_project("Synthetic")
    pipe.store.upsert_project(project.id, {"legal_name": "Synthetic"}, consent=True)
    pipe.store.enqueue(project.id, "external")
    execution = JobExecution(pipe.store, pipe.store.claim())
    pipe._execution_state.current = execution
    pipe.http_factory = lambda _: Http(lambda *a: response({"usage": {"prompt_tokens": 2, "completion_tokens": 1}}))
    settings = StudioSettings(model=ModelConfig(base_url="https://api.deepseek.com/v1", model="synthetic", api_key="model-key"),
                              search=SearchConfig("deepseek", "search-key"))
    _, http, _, _ = pipe.tools(settings)
    for path in ("tools/search", "tools/fetch", "chat/completions"):
        http.post_json("https://api.deepseek.com/v1/" + path, {})
    with pipe.store._connect() as db:
        rows = db.execute("SELECT kind,input_tokens FROM studio_job_calls").fetchall()
    assert sorted((r["kind"], r["input_tokens"]) for r in rows) == [("model", 2), ("page", None), ("search", None)]


def test_bootstrap_child_is_claimable_only_after_parent_finishes(tmp_path, monkeypatch):
    from project_watch.studio import dsh_runtime

    loop, _ = make_loop(tmp_path, "Synthetic Optics")
    monkeypatch.setenv("PROJECT_WATCH_DSH_RUNTIME", "synthetic")
    monkeypatch.setenv("PROJECT_WATCH_DSH_MONITOR", "1")
    monkeypatch.setattr(dsh_runtime, "run_bootstrap", lambda *a: {})
    job = loop.execution.job
    loop.pipeline.run(job)
    assert loop.store.claim() is None
    loop.store.finish(job["id"], lease_token=job["lease_token"])
    child = loop.store.claim()
    assert child["kind"] == "external_followup" and child["input_version"] == job["id"]
