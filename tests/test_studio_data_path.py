"""Data-path improvements against isolated synthetic stores, no provider calls."""

import json

from project_watch.studio import landscape
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError
from project_watch.studio.store import StudioStore


def setup(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project("合成细胞")
    pipeline.store.upsert_project(project.id, {}, consent=True)
    pipeline.store.add_items(project.id, "company", [
        {"key": "useful", "title": "合成细胞发布产品", "url": "https://public.example/useful",
         "source": "合成来源", "snippet": "公司产品进展", "published_at": None},
        {"key": "noise", "title": "同名旅游景点发布广告", "url": "https://public.example/noise",
         "source": "合成来源", "snippet": "旅游广告", "published_at": None},
    ])
    return pipeline, project.id


class Prefilter:
    ready = True

    def __init__(self):
        self.calls = []

    def json(self, system, user, **kwargs):
        self.calls.append((system, user))
        return {"results": [
            {"id": row["id"], "read": "旅游" not in row["title"]}
            for row in json.loads(user)["items"]
        ]}


def test_prefilter_spares_body_reads_without_hiding_or_triaging_items(tmp_path):
    pipeline, pid = setup(tmp_path)
    llm = Prefilter()
    eligible = pipeline.prefilter_bodies(pid, llm, pipeline.store.get_project(pid), {}, lambda _: None)
    items = pipeline.store.untriaged(pid)
    assert len(items) == 2 and all(i["triaged"] == 0 for i in items)
    assert {i["key"] for i in items if i["id"] in eligible} == {"useful"}
    called = []

    class Reader:
        def read(self, url):
            called.append(url)
            return {"status": "article_body", "text": "合成细胞发布产品的公开原文。"}

    pipeline.article_reader = Reader()
    assert pipeline.fetch_bodies(pid, lambda _: None, eligible_ids=eligible) == 1
    assert called == ["https://public.example/useful"]
    [noise] = [i for i in pipeline.store.untriaged(pid) if i["key"] == "noise"]
    assert noise["body_status"] == "" and noise["relevant"] == 1
    assert json.loads(noise["extra"])["body_prefilter"]["read"] is False


def test_prefilter_receipt_replays_same_job_without_a_second_model_call(tmp_path):
    pipeline, pid = setup(tmp_path)
    pipeline.store.enqueue(pid, "external")
    job = pipeline.store.claim()
    pipeline._execution_state.current = JobExecution(pipeline.store, job)
    llm = Prefilter()
    args = (pid, llm, pipeline.store.get_project(pid), {}, lambda _: None)
    first = pipeline.prefilter_bodies(*args)
    pipeline.store = StudioStore(pipeline.store.db_path)
    pipeline._execution_state.current = JobExecution(pipeline.store, job)
    assert pipeline.prefilter_bodies(*args) == first
    assert len(llm.calls) == 1


def test_missing_prefilter_decision_is_read_not_silently_excluded(tmp_path):
    pipeline, pid = setup(tmp_path)

    class Incomplete(Prefilter):
        def json(self, *_args, **_kwargs):
            return {"results": []}

    eligible = pipeline.prefilter_bodies(pid, Incomplete(), pipeline.store.get_project(pid), {}, lambda _: None)
    assert len(eligible) == 2


def test_competition_prompt_only_requests_source_profiles_not_discarded_verdicts():
    prompt = landscape._COMPETITION_SYSTEM
    assert '"threat"' not in prompt and '"claims"' not in prompt
    assert '"summary"' not in prompt and "竞争中的位置" not in prompt
    assert '"companies"' in prompt and '"cells"' in prompt


def test_google_news_recovers_matching_publisher_body_with_donor_provenance(tmp_path):
    pipeline, pid = setup(tmp_path)
    title = "合成细胞完成3亿元B轮融资"
    pipeline.store.add_items(pid, "company", [{"key": "google", "title": title,
        "url": "https://news.google.com/rss/articles/opaque", "source": "合成发布者",
        "snippet": "", "published_at": "2026-10-10T00:00:00Z"}])
    google = next(i for i in pipeline.store.untriaged(pid) if i["key"] == "google")
    queries, reads = [], []

    class Search:
        has_search = True

        def web_search(self, query, **kwargs):
            queries.append(query)
            return [{"title": title, "url": "https://publisher.example/financing", "source": "合成发布者",
                     "snippet": "检索摘要不应冒充正文", "published_at": "2026-10-10T00:00:00Z"}]

    class Reader:
        def read(self, url):
            reads.append(url)
            return {"status": "article_body", "text": title + "，原文披露了融资用途。",
                    "resolved_url": url}

    pipeline.article_reader = Reader()
    assert pipeline.fetch_bodies(pid, lambda _: None, eligible_ids={google["id"]}, collectors=Search()) == 1
    assert reads == ["https://publisher.example/financing"] and queries == [f'"{title}"']
    original = next(i for i in pipeline.store.untriaged(pid) if i["key"] == "google")
    assert original["body_status"] == "twin_body" and "原文披露" in original["body"]
    assert original["url"].startswith("https://news.google.com/")
    assert json.loads(original["extra"])["twin_source"]["url"] == reads[0]


def test_google_title_lookup_rejects_conflicting_amount_and_old_story(tmp_path):
    pipeline, pid = setup(tmp_path)
    title = "合成细胞完成3亿元B轮融资"
    pipeline.store.add_items(pid, "company", [{"key": "google", "title": title,
        "url": "https://news.google.com/rss/articles/opaque", "source": "合成发布者", "snippet": "",
        "published_at": "2026-10-10T00:00:00Z"}])
    google = next(i for i in pipeline.store.untriaged(pid) if i["key"] == "google")

    class Search:
        has_search = True

        def web_search(self, *_args, **_kwargs):
            return [{"title": title.replace("3亿", "5亿"), "url": "https://publisher.example/conflict",
                     "published_at": "2026-10-10T00:00:00Z"},
                    {"title": title, "url": "https://publisher.example/old", "published_at": "2025-01-01T00:00:00Z"}]

    class Reader:
        def read(self, url):
            raise AssertionError("unrelated reports must not be fetched")

    pipeline.article_reader = Reader()
    assert pipeline.fetch_bodies(pid, lambda _: None, eligible_ids={google["id"]}, collectors=Search()) == 0


def test_unknown_title_search_is_not_converted_to_empty_success(tmp_path):
    import pytest

    pipeline, pid = setup(tmp_path)
    pipeline.store.add_items(pid, "company", [{"key": "google", "title": "合成细胞发布最新进展",
        "url": "https://news.google.com/rss/articles/opaque", "source": "合成发布者", "snippet": "",
        "published_at": None}])
    google = next(i for i in pipeline.store.untriaged(pid) if i["key"] == "google")

    class Search:
        has_search = True

        def web_search(self, *_args, **_kwargs):
            raise HttpError("request_outcome_unknown")

    with pytest.raises(HttpError, match="request_outcome_unknown"):
        pipeline.fetch_bodies(pid, lambda _: None, eligible_ids={google["id"]}, collectors=Search())


def test_fulltext_filing_hits_bypass_the_title_prefilter(tmp_path):
    """R45: cninfo full-text hits name the company in the body, never in the title;
    the title prefilter declined 18 of 20 and no announcement was read."""
    pipeline, pid = setup(tmp_path)
    pipeline.store.add_items(pid, "company", [
        {"key": "filing", "title": "某上市公司：重大资产购买报告书", "url": "https://static.cninfo.com.cn/finalpage/a.PDF",
         "source": "巨潮资讯", "snippet": "", "published_at": None, "extra": {"matched": "fulltext"}},
        {"key": "title-filing", "title": "某上市公司：年度报告", "url": "https://static.cninfo.com.cn/finalpage/b.PDF",
         "source": "巨潮资讯", "snippet": "", "published_at": None, "extra": {"matched": "title"}},
    ])

    class DeclineAll(Prefilter):
        def json(self, system, user, **kwargs):
            self.calls.append((system, user))
            return {"results": [{"id": row["id"], "read": False} for row in json.loads(user)["items"]]}

    llm = DeclineAll()
    eligible = pipeline.prefilter_bodies(pid, llm, pipeline.store.get_project(pid), {}, lambda _: None)
    by_key = {i["key"]: i["id"] for i in pipeline.store.untriaged(pid)}
    assert by_key["filing"] in eligible and by_key["title-filing"] not in eligible
    assert all("重大资产购买" not in user for _, user in llm.calls)
