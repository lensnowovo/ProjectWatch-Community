"""Synthetic paper metadata/abstracts; no live OpenAlex or model requests."""

import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from project_watch.studio import pipeline as pipeline_module
from project_watch.studio import race
from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import Http, Response
from project_watch.studio.settings import SearchConfig
from project_watch.studio.sources import Collectors, paper_affiliated


def index(text):
    result = {}
    for position, word in enumerate(text.split()):
        result.setdefault(word, []).append(position)
    return result


def collect_paper(abstract, *, affiliation=False):
    calls = []
    work = {"id": "https://openalex.org/W123", "doi": "https://doi.org/10.123/synthetic",
            "display_name": "CAR-T platform study", "publication_date": "2026-09-01",
            "abstract_inverted_index": abstract,
            "authorships": [{"author": {"display_name": "A. Synthetic"},
                            "institutions": [{"display_name": "Synthetic University"}],
                            "raw_affiliation_strings": ["Synthetic University, Department of Biology"]}]}

    def transport(_method, url, *_args):
        calls.append(url)
        return Response(200, {}, json.dumps({"results": [work]}).encode())

    items = Collectors(Http(transport), SearchConfig()).openalex("CAR-T cells", affiliation=affiliation)
    return items[0], parse_qs(urlsplit(calls[0]).query)


def test_openalex_restores_abstract_order_and_repeated_words():
    abstract = "CAR-T cells target tumors and cells persist after treatment."
    paper, query = collect_paper(index(abstract))
    assert "abstract_inverted_index" in query["select"][0].split(",")
    assert paper["extra"]["paper_abstract"] == {"status": "available", "text": abstract, "truncated": False}
    assert paper["snippet"].startswith(abstract)
    assert paper["extra"]["paper_search"] == {"query": "CAR-T cells", "mode": "topic"}
    assert paper["extra"]["affiliations"] == ["Synthetic University, Department of Biology"]
    assert paper["extra"]["authors"] == ["A. Synthetic"]


@pytest.mark.parametrize("abstract,status", [
    (None, "missing"), ({}, "missing"), ([], "invalid"),
    ({"CAR-T": [0], "cells": [2]}, "invalid"),
    ({"CAR-T": [0], "cells": [0]}, "invalid"),
    ({"CAR-T": [-1]}, "invalid"), ({"CAR-T": [True]}, "invalid"),
    ({"CAR-T": ["0"]}, "invalid"), ({"CAR-T": [10**12]}, "invalid"),
    ({"CAR-T": "0"}, "invalid"), ({"CAR-T": [0, 0]}, "invalid"),
])
def test_missing_or_invalid_abstract_never_fabricates_text(abstract, status):
    paper, _query = collect_paper(abstract)
    assert paper["extra"]["paper_abstract"] == {"status": status, "text": "", "truncated": False}
    assert paper["snippet"] == "作者：A. Synthetic"


def test_long_abstract_is_bounded_and_reports_truncation():
    original = " ".join(f"token{n:04}" for n in range(2000))
    paper, _query = collect_paper(index(original))
    abstract = paper["extra"]["paper_abstract"]
    assert abstract["status"] == "available" and abstract["truncated"] is True
    assert 1000 < len(abstract["text"]) <= 8000
    assert original.startswith(abstract["text"])
    assert len(paper["snippet"]) <= 1200


def test_affiliation_search_remains_a_query_not_an_authorship_claim():
    paper, query = collect_paper(index("CAR-T cells in an unrelated laboratory."), affiliation=True)
    assert query["filter"] == ["raw_affiliation_strings.search:CAR-T cells"]
    assert paper["extra"]["paper_search"]["mode"] == "affiliation"
    assert "CAR-T cells" not in paper["extra"]["affiliations"]


def test_paper_triage_reads_labeled_abstract_not_only_authors(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project("合成细胞")
    pipeline.store.upsert_project(project.id, {"description": "CAR-T细胞治疗"}, consent=True)
    useful, _ = collect_paper(index("CAR-T cells target tumors with durable persistence."))
    noise, _ = collect_paper(index("This study measures distant galaxies and stellar dust."))
    noise.update(title="CAR-T survey", url="https://doi.org/10.123/noise", key="noise")
    pipeline.store.add_items(project.id, "paper", [useful, noise])
    pipeline.prepare_paper_abstracts(project.id)

    class Model:
        ready = True

        def json(self, system, user, **_kwargs):
            assert "论文摘要" in user and "distant galaxies" in user
            assert "署名单位" in user and "全文" in system and "行业共识" in system
            rows = pipeline.store.untriaged(project.id)
            return {"results": [{"id": row["id"], "relevant": row["key"] != "noise",
                                 "category": "company", "summary": "合成摘要", "importance": "routine"}
                                for row in rows]}

    pipeline.triage(project.id, Model(), pipeline.store.get_project(project.id), {}, lambda _: None)
    rows = pipeline.store.list_items(project_id=project.id)
    assert len(rows) == 1 and rows[0]["category"] == "paper"
    assert rows[0]["body_status"] == "paper_abstract"
    pointer = pipeline.store.item_evidence(project.id, rows[0]["id"])
    evidence = pipeline.evidence.get(project.id, pointer["evidence_version_id"], include_internal=False)
    assert evidence["read_scope"] == "summary" and evidence["source_type"] == "public_page"
    assert evidence["extraction_version"] == "openalex-abstract-v1"
    assert pipeline.prepare_paper_abstracts(project.id) == 0
    assert pipeline.store.body_coverage(project.id)["total"] == 0
    assert "paper" in pipeline_module._TRIAGE_SYSTEM


def test_abstract_missing_is_explicit_in_model_input(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project("合成细胞")
    pipeline.store.upsert_project(project.id, {}, consent=True)
    paper, _ = collect_paper(None)
    pipeline.store.add_items(project.id, "paper", [paper])
    pipeline.prepare_paper_abstracts(project.id)

    class Model:
        ready = True

        def json(self, _system, user, **_kwargs):
            assert "未取得论文摘要" in user and "Synthetic University" in user
            return {"results": []}

    pipeline._triage_results(Model(), "合成细胞", pipeline.store.untriaged(project.id))
    assert pipeline.store.untriaged(project.id)[0]["body_status"] == ""


def test_company_rd_counts_source_affiliations_not_topic_search_or_title():
    topic, _ = collect_paper(index("CAR-T cells persist."))
    topic.update(category="paper", title="Synthetic Cell publishes research")
    own = json.loads(json.dumps(topic))
    own["extra"]["affiliations"] = ["Synthetic Cell, Department of Research, City"]
    own["extra"]["institutions"] = ["Synthetic Cell"]
    lookalike = json.loads(json.dumps(topic))
    lookalike["extra"]["affiliations"] = ["Synthetic Cellular Institute"]
    result = race.build(project={"name": "Synthetic Cell", "short_name": "Synthetic Cell"}, plan={},
                        peers=[], company_events=[], competitor_events=[], threads=[], event_states=[],
                        commercial_records=[], rd_items=[topic, own, lookalike], today=date(2026, 10, 10))
    assert result["rows"][0]["rd"]["papers"] == 1


@pytest.mark.parametrize("record,matched", [
    ("Synthetic Cell, Department of Research, City", True),
    ("Department of Research; Synthetic Cell (City)", True),
    ("Synthetic Cellular Institute", False), ("Synthetic Cell Institute", False),
    ("AnotherSynthetic Cell, City", False),
    ("合成细胞大学,生物系", False), ("合成细胞,研究部", True),
])
def test_raw_affiliations_require_a_separate_identity_not_substring(record, matched):
    assert paper_affiliated({"extra": {"affiliations": [record]}}, ["Synthetic Cell", "合成细胞"]) is matched


def test_collection_to_event_keeps_topic_paper_and_original_abstract(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project("合成细胞")
    pipeline.store.upsert_project(project.id, {"description": "CAR-T细胞治疗"}, consent=True)
    paper, _query = collect_paper(index("CAR-T cells target tumors with durable persistence."))

    class Sources:
        has_search = has_registry = False

        def google_news(self, *_args, **_kwargs):
            return []

        bing_news = cninfo = google_news

        def openalex(self, *_args, **_kwargs):
            return [paper]

    class Model:
        ready = True

        def json(self, _system, user, **_kwargs):
            assert "durable persistence" in user and "CAR-T cells" in user
            rows = pipeline.store.untriaged(project.id)
            return {"results": [{"id": r["id"], "relevant": True, "category": "paper",
                                 "importance": "notable", "summary": "CAR-T持续性研究线索"} for r in rows]}

    plan = {"company_queries": ["合成细胞"], "paper_queries": ["CAR-T cells"], "affiliation_names": []}
    report = pipeline.collect(project.id, plan, Sources(), lambda _: None, pipeline.store.get_project(project.id))
    assert report["paper"]["checked"] == 1 and report["paper"]["added"] == 1
    pipeline.triage(project.id, Model(), pipeline.store.get_project(project.id), plan, lambda _: None)
    pipeline.cluster(project.id, Model(), lambda _: None)
    [event] = pipeline.store.list_events(project_id=project.id)
    assert event["category"] == "paper" and event["sources"][0]["body_status"] == "paper_abstract"
    [stored] = pipeline.store.list_items(project_id=project.id, category="paper")
    assert not paper_affiliated(stored, ["合成细胞"])
    assert stored["extra"]["paper_abstract"]["text"] == paper["extra"]["paper_abstract"]["text"]


def test_oversized_word_index_cannot_allocate_from_untrusted_offsets():
    paper, _query = collect_paper({"x": list(range(5000))})
    assert paper["extra"]["paper_abstract"]["status"] == "invalid"
    assert paper["extra"]["paper_abstract"]["text"] == ""


@pytest.mark.parametrize('expired', [False, True])
def test_leased_abstract_ingestion_uses_real_dispatch_fence(tmp_path, expired):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Cell').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    paper, _ = collect_paper(index('Synthetic CAR-T cells persist after treatment.'))
    pipeline.store.add_items(pid, 'paper', [paper])
    pipeline.store.enqueue(pid, 'external')
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job)
    pipeline._execution_state.current = execution
    if expired:
        with pipeline.store._connect() as db, db:
            db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?", (job['id'],))
        with pytest.raises(RuntimeError, match='lease expired'):
            pipeline.prepare_paper_abstracts(pid)
        assert pipeline.store.untriaged(pid)[0]['body_status'] == ''
        with pipeline.store._connect() as db:
            assert db.execute('SELECT count(*) FROM studio_evidence_versions').fetchone()[0] == 0
    else:
        assert pipeline.prepare_paper_abstracts(pid) == 1
        row = pipeline.store.untriaged(pid)[0]
        assert row['body_status'] == 'paper_abstract'
        pointer = pipeline.store.item_evidence(pid, row['id'])
        record = pipeline.evidence.get(pid, pointer['evidence_version_id'])
        assert record['read_scope'] == 'summary' and record['extraction_version'] == 'openalex-abstract-v1'
        assert pipeline.prepare_paper_abstracts(pid) == 0
