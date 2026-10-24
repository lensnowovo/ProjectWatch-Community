"""A truncated triage answer splits the batch instead of failing the external update. Synthetic."""

from __future__ import annotations

import json
import re

import pytest

from project_watch.studio.llm import ModelError


class ShortModel:
    """Answers at most 4 items; longer batches come back cut off, like a model hitting max_tokens."""

    ready = True

    def __init__(self, limit: int = 4, stubborn: str = "") -> None:
        self.limit, self.stubborn, self.calls = limit, stubborn, []

    def json(self, system, user, max_tokens=0):
        ids = re.findall(r"id=([0-9a-f]+)", user)
        self.calls.append(len(ids))
        if len(ids) > self.limit or (self.stubborn and self.stubborn in user):
            raise ModelError("模型输出未完成", code="model_output_truncated")
        return {"results": [{"id": i, "source_title": t, "source_quote": t,
                             "relevant": True, "importance": "notable", "summary": "s", "impact": "i"}
                            for i, t in zip(ids, re.findall(r"标题：(.*)", user), strict=True)]}


def _setup(tmp_path, n):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {"legal_name": "光启视界科技有限公司"}, consent=True)
    store.add_items(project.id, "company", [{"key": f"k{i}", "title": f"光启视界新闻 {i}", "url": f"https://n.example/{i}",
                                             "source": "示例", "snippet": "", "published_at": None} for i in range(n)])
    return store, project, Pipeline(repo, store, SettingsStore(tmp_path / "s.json"))


def test_truncated_batches_are_split_until_they_fit(tmp_path):
    store, project, pipeline = _setup(tmp_path, 25)
    model = ShortModel(limit=4)
    pipeline.triage(project.id, model, store.get_project(project.id), {"competitors": []}, lambda _: None)
    items = store.list_items(project_id=project.id, include_dismissed=True)
    assert len(items) == 25 and all(i["importance"] == "notable" for i in items)
    assert max(model.calls) == 20 and store.untriaged(project.id) == []


def test_an_item_that_never_fits_is_kept_as_routine(tmp_path):
    store, project, pipeline = _setup(tmp_path, 3)
    model = ShortModel(limit=10, stubborn="光启视界新闻 1")
    pipeline.triage(project.id, model, store.get_project(project.id), {"competitors": []}, lambda _: None)
    by_title = {i["title"]: i["importance"] for i in store.list_items(project_id=project.id, include_dismissed=True)}
    assert by_title == {"光启视界新闻 0": "notable", "光启视界新闻 1": "routine", "光启视界新闻 2": "notable"}


def test_other_model_errors_still_surface(tmp_path):
    import pytest

    store, project, pipeline = _setup(tmp_path, 2)

    class Broken:
        ready = True

        def json(self, *a, **k):
            raise ModelError("模型 Key 无效", code="model_access_denied")

    with pytest.raises(ModelError):
        pipeline.triage(project.id, Broken(), store.get_project(project.id), {"competitors": []}, lambda _: None)


class MalformedModel(ShortModel):
    """Complete but invalid JSON, including a failed model repair, on large batches."""

    def json(self, system, user, max_tokens=0):
        ids = re.findall(r"id=([0-9a-f]+)", user)
        self.calls.append(len(ids))
        if len(ids) > self.limit or (self.stubborn and self.stubborn in user):
            raise ModelError("模型返回的内容不是有效 JSON", code="model_output_invalid_json")
        return {"results": [{"id": i, "source_title": t, "source_quote": t, "relevant": True, "importance": "notable",
                             "summary": "s", "impact": "i"}
                            for i, t in zip(ids, re.findall(r"标题：(.*)", user), strict=True)]}


def test_malformed_complete_batches_are_split(tmp_path):
    store, project, pipeline = _setup(tmp_path, 25)
    model = MalformedModel(limit=4)
    pipeline.triage(project.id, model, store.get_project(project.id), {"competitors": []}, lambda _: None)
    items = store.list_items(project_id=project.id, include_dismissed=True)
    assert len(items) == 25 and all(i["importance"] == "notable" for i in items)
    assert max(model.calls) == 20 and len(model.calls) <= 38
    assert store.untriaged(project.id) == []


def test_malformed_singleton_is_not_silently_assessed(tmp_path):
    store, project, pipeline = _setup(tmp_path, 1)
    model = MalformedModel(limit=0)
    with pytest.raises(ModelError) as caught:
        pipeline.triage(project.id, model, store.get_project(project.id), {"competitors": []}, lambda _: None)
    assert caught.value.code == "model_output_invalid_json"
    assert model.calls == [1] and len(store.untriaged(project.id)) == 1


@pytest.mark.parametrize("code", ["model_access_denied", "request_outcome_unknown", "provider_backoff", "request_budget"])
def test_non_content_errors_are_not_bisected(tmp_path, code):
    store, project, pipeline = _setup(tmp_path, 5)

    class Interrupted:
        ready = True
        calls = 0

        def json(self, *args, **kwargs):
            self.calls += 1
            raise ModelError("暂停", code=code)

    model = Interrupted()
    with pytest.raises(ModelError) as caught:
        pipeline.triage(project.id, model, store.get_project(project.id), {"competitors": []}, lambda _: None)
    assert caught.value.code == code and model.calls == 1
    assert len(store.untriaged(project.id)) == 5


@pytest.mark.parametrize("bad", ["text", {"unexpected": "object"}, ["not an object"]])
def test_wrong_results_shape_is_an_explicit_content_error(tmp_path, bad):
    store, project, pipeline = _setup(tmp_path, 1)

    class WrongShape:
        ready = True

        def json(self, *args, **kwargs):
            return {"results": bad}

    with pytest.raises(ModelError) as caught:
        pipeline.triage(project.id, WrongShape(), store.get_project(project.id), {"competitors": []}, lambda _: None)
    assert caught.value.code == "model_output_invalid_shape"
    assert len(store.untriaged(project.id)) == 1


def test_http_200_stop_and_failed_repair_can_recover_by_bisection(tmp_path):
    from project_watch.studio.http import Http, Response
    from project_watch.studio.llm import LLM
    from project_watch.studio.settings import ModelConfig

    store, project, pipeline = _setup(tmp_path, 3)
    calls = []

    def provider(method, url, headers, body, timeout):
        payload = json.loads(body)
        user = payload["messages"][-1]["content"]
        ids = re.findall(r"id=([0-9a-f]+)", user)
        calls.append(len(ids))
        if len(ids) > 1 or not ids:  # Invalid batch and equally invalid repair, both complete.
            text = '{"results":[{"id":"example","",""}]}'
        else:
            title = re.findall(r"标题：(.*)", user)[0]
            text = json.dumps({"results": [{"id": ids[0], "source_title": title, "source_quote": title, "relevant": True,
                                            "importance": "notable", "summary": "s"}]})
        return Response(200, {}, json.dumps({"choices": [{"message": {"content": text},
                                                         "finish_reason": "stop"}]}).encode())

    model = LLM(ModelConfig(base_url="https://synthetic.example/v1", model="test", api_key="test"), Http(provider))
    pipeline.triage(project.id, model, store.get_project(project.id), {"competitors": []}, lambda _: None)
    assert calls == [3, 0, 1, 2, 0, 1, 1]
    assert store.untriaged(project.id) == []
    assert all(i["importance"] == "notable" for i in store.list_items(project_id=project.id))
