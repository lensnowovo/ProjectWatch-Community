"""Founder-led companies: cited dossier people, author papers and investigator trials reach collection. Synthetic only."""

from types import SimpleNamespace

from project_watch.studio.app import create_studio_app


def _pipeline(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = pipeline.repository.create_project("合成干细胞").id
    pipeline.store.upsert_project(pid, {"short_name": "合成干细胞", "legal_name": "北京合成干细胞医药有限公司",
                                        "industry": "细胞治疗"}, consent=True)
    pipeline.store.put_public_profile(pid, {"facts": [
        {"kind": "person", "label": "张合成", "text": "科学创始人，创立化学重编程技术", "refs": [{"url": "https://n.example/1"}]},
        {"kind": "business", "label": "主营业务", "text": "化学诱导多能干细胞平台", "refs": [{"url": "https://n.example/2"}]},
    ]})
    return pipeline, pid


def test_plan_uses_cited_dossier_people_but_not_model_only_names(tmp_path):
    pipeline, pid = _pipeline(tmp_path)
    seen = {}

    class Model:
        ready = True

        def json(self, system, user, max_tokens=0):
            seen["user"] = user
            return {"company_queries": ["合成干细胞"], "people": ["模型臆测人名"], "industry_queries": ["化学重编程 CiPSC"],
                    "paper_authors": ["Hecheng Zhang"], "trial_terms": ["chemically induced pluripotent stem cell islet"],
                    "is_biomed": True}

    pipeline.objects.ensure_subject(pid)  # created projects have a confirmed subject object
    plan = pipeline.build_plan(Model(), pipeline.store.get_project(pid))
    assert "公开档案" in seen["user"] and "张合成" in seen["user"]
    assert "张合成" in plan["people"] and "模型臆测人名" not in plan["people"]
    assert plan["paper_authors"] == ["Hecheng Zhang"]
    assert plan["trial_terms"] == ["chemically induced pluripotent stem cell islet"]


def test_collection_searches_people_with_short_name_and_topic_authors_and_trial_terms(tmp_path):
    pipeline, pid = _pipeline(tmp_path)
    calls = []

    class Collectors:
        has_search = False
        has_registry = False

        def bing_news(self, query):
            calls.append(("news", query))
            return []

        def openalex(self, query, **kwargs):
            calls.append(("paper", query, "author" if kwargs.get("author") else "affiliation" if kwargs.get("affiliation") else "topic"))
            return []

        def clinical_trials(self, *, sponsor=None, term=None, page_size=20):
            calls.append(("trial", sponsor or term, "sponsor" if sponsor else "term"))
            return []

        def cninfo(self, query, **kwargs):
            return []

    plan = {"company_queries": ["北京合成干细胞医药有限公司"], "people": ["张合成"], "competitors": [],
            "industry_queries": ["化学重编程 CiPSC"], "policy_queries": [], "paper_queries": [], "affiliation_names": [],
            "paper_authors": ["Hecheng Zhang"], "trial_sponsors": ["Synthetic Sponsor"],
            "trial_terms": ["chemically induced pluripotent stem cell islet"], "patent_assignees": [], "is_biomed": True}
    pipeline.collect(pid, plan, Collectors(), lambda _s: None, pipeline.store.get_project(pid))
    assert ("news", "张合成 合成干细胞") in calls and ("news", "张合成 化学重编程 CiPSC") in calls
    assert ("news", "张合成 北京合成干细胞医药有限公司") not in calls  # founders are rarely reported with the legal name
    assert ("paper", "Hecheng Zhang", "author") in calls
    assert ("trial", "chemically induced pluripotent stem cell islet", "term") in calls
    assert ("trial", "Hecheng Zhang", "term") in calls  # investigator-initiated studies
    assert ("trial", "Synthetic Sponsor", "sponsor") in calls


def test_a_new_public_dossier_rebuilds_the_plan(tmp_path, monkeypatch):
    pipeline, pid = _pipeline(tmp_path)
    built = []
    monkeypatch.setattr(pipeline, "build_plan", lambda *_a: built.append(1) or {
        "company_queries": ["合成干细胞"], "people": [], "competitors": [], "industry_queries": [], "policy_queries": [],
        "paper_queries": [], "affiliation_names": [], "trial_sponsors": [], "patent_assignees": [], "is_biomed": False})
    settings = pipeline.settings_store.load()

    class Collectors:
        has_search = False
        has_registry = False

        def bing_news(self, query):
            return []

        def openalex(self, query, **kwargs):
            return []

        def cninfo(self, query, **kwargs):
            return []

    monkeypatch.setattr(pipeline, "tools", lambda: (settings, None, SimpleNamespace(ready=False), Collectors()))
    monkeypatch.setattr(pipeline, "_check_sites", lambda *_a: None)
    monkeypatch.setattr(pipeline, "_collect_monitor", lambda *_a: {})
    monkeypatch.setattr(pipeline, "run_public_profile", lambda *_a: None)

    def round_():
        pipeline.store.enqueue(pid, "external")
        while (job := pipeline.store.claim()):
            pipeline.run(job)
            pipeline.store.finish(job["id"], lease_token=job["lease_token"])

    round_()
    first = len(built)
    round_()
    assert len(built) == first  # unchanged inputs reuse the plan
    pipeline.store.put_public_profile(pid, {"facts": [{"kind": "person", "label": "李新人", "text": "新任首席科学家",
                                                       "refs": [{"url": "https://n.example/3"}]}]})
    round_()
    assert len(built) == first + 1
