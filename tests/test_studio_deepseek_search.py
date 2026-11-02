"""Synthetic DeepSeek fixtures only; no live services or credentials."""
import json

import pytest

from project_watch.studio.app import create_studio_app
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


def test_deepseek_preferred_content_retained_and_page_date_labelled():
    c, seen = collector(lambda *a: response({"results": [page()]}))
    hit = c.web_search("Synthetic Optics", count=10)[0]
    assert len(seen) == 1 and seen[0][0].endswith("/tools/search")
    assert seen[0][1] == {"query": "Synthetic Optics", "count": 10, "need_content": True}
    assert hit["extra"]["search_content"] == "2025年意向订单500万元。"
    # No date in the URL or the page text: the provider's page date is used and labelled as such.
    assert hit["published_at"] == "2026-01-01T00:00:00+00:00" and hit["extra"]["date_source"] == "search"
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








def test_settings_keep_old_keys_and_mask_new_key(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"search": {"providers": ["bocha"], "keys": {"bocha": "synthetic-old"}}})
    store.update({"search": {"providers": ["deepseek", "bocha"], "keys": {"deepseek": "synthetic-new"}}})
    config = store.load()
    assert config.search.credentials() == {"bocha": "synthetic-old", "deepseek": "synthetic-new"}
    store.update({"search": {"providers": ["deepseek", "bocha", "tavily", "brave"]}})
    assert len(store.load().search.enabled()) == 4




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
    pipe.store.enqueue(project.id, "collect")
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


