"""The same headline from another outlet is screened once; an irrelevant verdict is never reused. Synthetic."""
import json
import re

from tests.test_studio_triage_split import _setup


class Model:
    ready = True

    def __init__(self, relevant=True):
        self.seen, self.relevant = [], relevant

    def json(self, system, user, max_tokens=0):
        ids = re.findall(r"id=([0-9a-f]+)", user)
        titles = re.findall(r"标题：(.*)", user)
        self.seen += titles
        return {"results": [{"id": i, "source_title": t, "source_quote": t, "relevant": self.relevant,
                             "importance": "major", "summary": f"摘要：{t[:12]}", "impact": "影响"}
                            for i, t in zip(ids, titles, strict=True)]}


def add(store, project, titles, when='2026-11-20T08:00:00+00:00', start=0):
    store.add_items(project.id, "company", [{"key": f"r{start + n}", "title": t, "url": f"https://n.example/r{start + n}",
                                             "source": "示例", "snippet": "", "published_at": when}
                                            for n, t in enumerate(titles)])


def triage(store, project, pipeline, model):
    pipeline.triage(project.id, model, store.get_project(project.id), {"competitors": []}, lambda _: None)


def items(store, project):
    return store.list_items(project_id=project.id, include_dismissed=True)


HEADLINE = "光启视界发布新一代光场显示模组并开放样品申请"


def test_a_repost_takes_the_relevant_verdict_without_a_model_request(tmp_path):
    store, project, pipeline = _setup(tmp_path, 0)
    add(store, project, [HEADLINE])
    first = Model()
    triage(store, project, pipeline, first)
    add(store, project, [HEADLINE + " - 今日头条", HEADLINE + "-证券之星"], when='2026-11-21T08:00:00+00:00', start=1)
    later = Model()
    triage(store, project, pipeline, later)
    assert later.seen == []
    rows = items(store, project)
    assert len(rows) == 3 and {r["importance"] for r in rows} == {"major"} and store.untriaged(project.id) == []
    reposts = [r for r in rows if r["title"] != HEADLINE]
    extras = [r["extra"] if isinstance(r["extra"], dict) else json.loads(r["extra"] or "{}") for r in reposts]
    assert all(e["triage_binding"]["status"] == "repost" for e in extras)


def test_reposts_in_one_batch_are_screened_once(tmp_path):
    store, project, pipeline = _setup(tmp_path, 0)
    add(store, project, [HEADLINE, HEADLINE + " - 今日头条", "光启视界与某车企签署合作框架协议，共建座舱方案"])
    model = Model()
    triage(store, project, pipeline, model)
    assert len(model.seen) == 2 and sum(t.startswith(HEADLINE) for t in model.seen) == 1  # one copy of the story
    assert len(items(store, project)) == 3 and store.untriaged(project.id) == []


def test_an_irrelevant_verdict_is_asked_again(tmp_path):
    store, project, pipeline = _setup(tmp_path, 0)
    add(store, project, [HEADLINE])
    triage(store, project, pipeline, Model(relevant=False))
    add(store, project, [HEADLINE + " - 今日头条"], when='2026-11-21T08:00:00+00:00', start=1)
    later = Model()
    triage(store, project, pipeline, later)
    assert later.seen == [HEADLINE + " - 今日头条"]


def test_a_different_headline_or_a_later_week_is_asked_again(tmp_path):
    store, project, pipeline = _setup(tmp_path, 0)
    add(store, project, [HEADLINE])
    triage(store, project, pipeline, Model())
    add(store, project, [HEADLINE.replace("新一代", "第二代")], when='2026-11-21T08:00:00+00:00', start=1)
    add(store, project, [HEADLINE + " - 今日头条"], when='2026-11-28T08:00:00+00:00', start=2)
    later = Model()
    triage(store, project, pipeline, later)
    assert len(later.seen) == 2
