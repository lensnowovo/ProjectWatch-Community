"""Synthetic batch cross-wiring regressions; no network or historical private rows."""
from test_studio_triage_split import _setup


class Reply:
    ready = True
    calls = 0

    def __init__(self, results):
        self.results = results

    def json(self, *args, **kwargs):
        self.calls += 1
        return {"results": self.results}


def test_other_articles_summary_is_not_attached_by_id_alone(tmp_path):
    store, project, pipeline = _setup(tmp_path, 2)
    batch = store.untriaged(project.id)
    a, b = batch
    model = Reply([{"id": a["id"], "source_title": b["title"], "source_quote": b["title"],
                    "summary": "错绑到另一个条目的摘要", "importance": "major", "watch": ["W1"]}])
    pipeline.triage(project.id, model, store.get_project(project.id), {}, lambda _: None)
    rows = store.list_items(project_id=project.id)
    assert all(r["summary"] == r["title"] and r["importance"] == "routine" for r in rows)
    assert next(r for r in rows if r["id"] == a["id"])["extra"]["triage_binding"]["status"] == "title_mismatch"
    assert model.calls == 1  # never spend a repair request on a binding error


def test_matching_title_still_requires_quote_from_that_item(tmp_path):
    store, project, pipeline = _setup(tmp_path, 1)
    batch = store.untriaged(project.id)
    a = batch[0]
    model = Reply([{"id": a["id"], "source_title": a["title"], "source_quote": "another article's exact passage",
                    "summary": "不可接受的摘要"}])
    result = pipeline._triage_results(model, "", batch)[0]
    assert result["summary"] == a["title"]
    assert result["triage_binding"]["status"] == "quote_mismatch"


def test_valid_binding_keeps_result_but_duplicate_ids_fall_back(tmp_path):
    store, project, pipeline = _setup(tmp_path, 1)
    batch = store.untriaged(project.id)
    a = batch[0]
    row = {"id": a["id"], "source_title": a["title"], "source_quote": a["title"], "summary": "正确摘要"}
    assert pipeline._triage_results(Reply([row]), "", batch)[0]["summary"] == "正确摘要"
    duplicated = pipeline._triage_results(Reply([row, row]), "", batch)
    assert len(duplicated) == 1 and duplicated[0]["summary"] == a["title"]
    assert duplicated[0]["triage_binding"]["status"] == "duplicate_id"


def test_legacy_unbound_receipt_is_visible_without_reusing_generated_claim(tmp_path):
    store, project, pipeline = _setup(tmp_path, 1)
    batch = store.untriaged(project.id)
    a = batch[0]
    result = pipeline._triage_results(Reply([{"id": a["id"], "summary": "旧回执的未绑定摘要"}]), "", batch)[0]
    assert result["summary"] == a["title"] and result["impact"] == ""
    assert result["triage_binding"]["status"] == "missing_binding"
