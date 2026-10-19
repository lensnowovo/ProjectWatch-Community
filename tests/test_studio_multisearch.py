"""Independent search credentials, merging and partial coverage (offline only)."""
import json

import pytest

from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.settings import SearchConfig, SettingsStore
from project_watch.studio.sources import Collectors, SearchCache


def test_multi_settings_migrate_legacy_keep_mask_and_disable(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"search": {"provider": "bocha", "api_key": "synthetic-bocha-key"}})
    store.update({"search": {"providers": ["bocha", "tavily"], "keys": {"tavily": "synthetic-tavily-key"}}})
    view = store.public_view(store.load())
    assert view["search"]["providers"] == ["bocha", "tavily"]
    assert "synthetic-" not in json.dumps(view)
    store.update({"search": {"providers": ["tavily"], "keys": {"tavily": ""}}})
    assert store.load().search.ready_providers() == [("tavily", "synthetic-tavily-key")]
    store.update({"search": {"providers": []}})
    assert store.load().search.ready_providers() == []
    store.update({"search": {"providers": ["bocha", "tavily"]}})
    assert len(store.load().search.ready_providers()) == 2
    # Legacy clients continue to replace the active mode without leaking another key.
    store.update({"search": {"provider": "brave", "api_key": ""}})
    assert not store.load().search.ready_providers()


@pytest.mark.parametrize("patch", [
    {"providers": ["unknown"]}, {"providers": ["bocha", "bocha"]},
    {"providers": "bocha"}, {"keys": {"unknown": "key"}},
    {"keys": {"bocha": 123}}, {"keys": {"bocha": "bad key"}},
])
def test_multi_invalid_patch_does_not_write(tmp_path, patch):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.update({"search": patch})
    assert not store.path.exists()


def transport(bocha_status=200, tavily_status=200):
    calls = []

    def send(method, url, headers, body, timeout):
        provider = "bocha" if "bocha" in url else "tavily"
        calls.append(provider)
        assert method == "POST"
        assert headers["Authorization"] == f"Bearer synthetic-{provider}-key"
        assert json.loads(body)["query"] == "public synthetic company"
        status = bocha_status if provider == "bocha" else tavily_status
        if status != 200:
            return Response(status, {}, b'{"message":"echo synthetic-sensitive-key"}')
        if provider == "bocha":
            data = {"data": {"webPages": {"value": [
                {"name": "Shared", "url": "https://example.com/shared"},
                {"name": "Chinese", "url": "https://example.com/chinese"}]}}}
        else:
            data = {"results": [{"title": "Shared", "url": "https://example.com/shared#fragment"},
                                {"title": "Global", "url": "https://example.com/global"}]}
        return Response(200, {}, json.dumps(data).encode())

    return Http(send), calls


def config():
    return SearchConfig(mode="combined", providers=["bocha", "tavily"], keys={
        "bocha": "synthetic-bocha-key", "tavily": "synthetic-tavily-key"})


def test_both_services_merge_and_preserve_attribution():
    http, calls = transport()
    collectors = Collectors(http, config())
    hits = collectors.web_search("public synthetic company")
    assert calls == ["bocha", "tavily"]
    assert len(hits) == 3
    assert hits[0]["extra"]["search_providers"] == ["bocha", "tavily"]
    assert [r["ok"] for r in collectors.search_checks] == [True, True]


@pytest.mark.parametrize("bocha,tavily", [(401, 200), (200, 429)])
def test_partial_search_survives_with_visible_failure(bocha, tavily):
    http, calls = transport(bocha, tavily)
    collectors = Collectors(http, config())
    assert len(collectors.web_search("public synthetic company")) == 2
    assert len(calls) == 2
    assert sum(r["ok"] for r in collectors.search_checks) == 1
    assert "sensitive" not in json.dumps(collectors.search_checks)


def test_all_failures_raise_safe_error_for_existing_fallbacks():
    http, calls = transport(401, 429)
    collectors = Collectors(http, config())
    with pytest.raises(HttpError, match="search_key_invalid") as caught:
        collectors.web_search("public synthetic company")
    assert "sensitive" not in str(caught.value)
    assert len(calls) == 2


def test_missing_second_key_is_not_sent_and_not_claimed_connected():
    http, calls = transport()
    cfg = config()
    cfg.keys.pop("tavily")
    collectors = Collectors(http, cfg)
    assert collectors.has_search
    assert len(collectors.web_search("public synthetic company")) == 2
    assert calls == ["bocha"]
    assert collectors.search_checks[-1]["code"] == "search_key_missing"


def test_empty_success_is_not_an_all_provider_failure():
    def send(method, url, headers, body, timeout):
        if "bocha" in url:
            return Response(503, {}, b"private failure body")
        return Response(200, {}, b'{"results": []}')

    collectors = Collectors(Http(send), config())
    assert collectors.web_search("public synthetic company") == []
    assert [r["ok"] for r in collectors.search_checks] == [False, True]


def test_auto_search_uses_one_service_for_each_language():
    http, calls = transport()
    cfg = config()
    cfg.mode = "auto"
    collectors = Collectors(http, cfg)
    assert collectors.web_search("public synthetic company")
    assert calls == ["tavily"]
    calls.clear()
    collectors._provider_search = lambda p, k, q, **kwargs: calls.append(p) or [{
        "key": "synthetic", "extra": {}}]
    collectors.web_search("合成公司 汽车连接器")
    assert calls == ["bocha"]


def test_auto_search_falls_back_after_failure():
    http, calls = transport(200, 401)
    cfg = config()
    cfg.mode = "auto"
    collectors = Collectors(http, cfg)
    assert collectors.web_search("public synthetic company")
    assert calls == ["tavily", "bocha"]
    assert not collectors.search_checks[0]["ok"]


def test_original_auto_search_falls_back_after_nonempty_unusable_hits():
    title = '合成公司完成新一轮融资并推进细胞治疗研发'
    calls = []
    cfg = config()
    cfg.mode = 'auto'
    collectors = Collectors(Http(lambda *_: None), cfg)
    def search(provider, credential, query, **kwargs):
        calls.append(provider)
        assert query == '"' + title + '"'
        host = 'blocked.test' if provider == 'bocha' else 'publisher.test'
        return [{'key': host, 'url': f'https://{host}/report', 'title': title, 'extra': {}}]
    collectors._provider_search = search
    results = collectors.original_search({'url': 'https://blocked.test/lead', 'title': title})
    assert calls == ['bocha', 'tavily']
    assert len(results) == 2  # Unsuccessful leads are retained, not quietly erased.
    assert [r['acceptable_count'] for r in collectors.search_checks] == [0, 1]


@pytest.mark.parametrize('code', ['request_outcome_unknown', 'request_budget', 'provider_backoff'])
def test_original_search_holds_do_not_trigger_a_second_provider(code):
    calls = []
    cfg = config()
    cfg.mode = 'auto'
    collectors = Collectors(Http(lambda *_: None), cfg)
    def search(*args, **kwargs):
        calls.append(1)
        raise HttpError(code)
    collectors._provider_search = search
    with pytest.raises(HttpError, match=code):
        collectors.original_search({'url': 'https://blocked.test/lead', 'title': '合成公司完成新一轮融资'})
    assert calls == [1]


@pytest.mark.parametrize("code", ["request_outcome_unknown", "request_budget", "provider_backoff"])
def test_uncertain_or_held_search_does_not_trigger_another_provider(code):
    calls = []

    def send(*_args):
        calls.append(1)
        raise HttpError(code)

    cfg = config()
    cfg.mode = "auto"
    with pytest.raises(HttpError, match=code):
        Collectors(Http(send), cfg).web_search("public synthetic company")
    assert calls == [1]


def test_shared_cache_reuses_calls_but_does_not_share_mutations_or_credentials():
    http, calls = transport()
    cache = SearchCache()
    collectors = Collectors(http, config(), search_cache=cache)
    first = collectors.web_search("public synthetic company")
    first[0]["title"] = "Modified by caller"
    second = Collectors(http, config(), search_cache=cache).web_search("public synthetic company")
    assert calls == ["bocha", "tavily"]
    assert second[0]["title"] == "Shared"
    assert second[0]["extra"]["search_providers"] == ["bocha", "tavily"]
    signature = ("key-fingerprint",)
    cache.get(signature, lambda: [])
    _, cached = cache.get(("different-key-fingerprint",), lambda: [])
    assert not cached


def test_cache_expires_evicts_and_coalesces_simultaneous_queries():
    from concurrent.futures import ThreadPoolExecutor

    cache = SearchCache(capacity=1)
    calls = []

    def fetch():
        calls.append(1)
        return []

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: cache.get(("same",), fetch), range(4)))
    assert len(calls) == 1
    cache.get(("other",), fetch)
    assert len(cache.entries) == 1
    assert not cache.get(("same",), fetch)[1]
    cache.ttl = 0
    assert not cache.get(("same",), fetch)[1]


def test_mode_only_update_keeps_both_keys(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"search": {"providers": ["bocha", "tavily"], "keys": config().keys}})
    store.update({"search": {"mode": "auto"}})
    assert len(store.load().search.ready_providers()) == 2
    assert store.public_view(store.load())["search"]["mode"] == "auto"
    with pytest.raises(ValueError):
        store.update({"search": {"mode": "unknown"}})


def test_settings_api_tests_each_provider_and_reports_missing_keys(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    http, calls = transport(401, 200)
    collectors = Collectors(http, config())
    monkeypatch.setattr(collectors, "bing_news", lambda query: [])
    # Reuse the production test route with a fake local pipeline (no model/network).
    class NoModel:
        ready = False

    monkeypatch.setattr(app.state.studio_pipeline, "tools", lambda settings: (settings, http, NoModel(), collectors))
    with TestClient(app) as client:
        view = client.put("/api/studio/settings", json={"search": {
            "providers": ["bocha", "tavily"], "keys": config().keys}}).json()
        assert view["search"]["ready"]
        assert "synthetic-" not in json.dumps(view)
        # Transport asserts its query to verify the exact synthetic/public request.
        monkeypatch.setattr(Collectors, "web_search", lambda self, q, **kw:
                            self._provider_search(self.search_config.provider, self.search_config.api_key,
                                                  "public synthetic company", news=False, count=3))
        result = client.post("/api/studio/settings/test").json()
        assert result["search_bocha"]["ok"] is False
        assert result["search_tavily"]["ok"] is True
        assert calls == ["bocha", "tavily"]
        settings_store = app.state.studio_pipeline.settings_store
        settings = settings_store.load()
        settings.search.keys.pop("tavily")
        settings_store.save(settings)
        result = client.post("/api/studio/settings/test").json()
        assert result["search_tavily"] == {"ok": False, "message": "尚未填写 API Key"}
