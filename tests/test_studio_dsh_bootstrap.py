"""Synthetic public research; no provider credentials, network or private data."""
import json
import sqlite3

import pytest

from project_watch.studio.app import create_studio_app
from project_watch.studio.execution import JobExecution
from project_watch.studio.http import HttpError


class Search:
    def web_search(self, query, count=6):
        return [{"key": "synthetic-one", "title": "Synthetic Optics 产品与团队",
                 "url": "https://example.com/company", "source": "Synthetic source",
                 "snippet": "Synthetic Optics 开发激光模组。李明为董事长。2025年意向订单为500万元。",
                 "published_at": "2026-01-01"}]


class Reader:
    def read(self, url):
        return {"status": "article_body", "text": Search().web_search("")[0]["snippet"]}


@pytest.fixture
def research(tmp_path):
    from project_watch.studio.dsh_bootstrap import PublicBootstrap
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project("Synthetic Optics")
    pipeline.store.upsert_project(project.id, {"legal_name": "Synthetic Optics"}, consent=True)
    pipeline.store.enqueue(project.id, "external")
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    from test_studio_dsh_monitor import synthetic_review
    result = PublicBootstrap(pipeline, execution, Search(), Reader())
    result.record_reviewer = synthetic_review
    return result


def call(research, name, **args):
    return research.tool(name, args)


def record(research, kind="product", **kwargs):
    return call(research, "pw_record", kind=kind, label="激光模组",
                source_id="S1", quote="Synthetic Optics 开发激光模组。",
                text="Synthetic Optics 开发激光模组。", **kwargs)


def test_source_linked_relation_is_not_a_question_and_replays_once(research):
    call(research, "pw_search", query="Synthetic Optics 产品", category="company")
    kwargs = dict(relation_type="product", relation_target="激光模组", axis="asset",
                  query="激光模组 进展", reason="公司开发该产品，跟踪产品进展")
    record(research, kind="relation", **kwargs)
    with pytest.raises(ValueError, match="relation requires"):
        record(research, kind="relation", **{**kwargs, "relation_target": "不存在的疾病"})
    for _ in range(2):
        call(research, "pw_finish", gaps={})
    plan = research.pipeline.monitoring_plan
    [watch] = plan.items(research.project_id)
    assert watch["scope_kind"] == "relation" and watch["relation_target"] == "激光模组"
    assert watch["status"] == "trial" and watch["cadence"] == "medium"
    assert watch["id"] in {w['id'] for w in plan.due(research.project_id)}
    assert watch["exit_rule"] == "" and not plan.explorations.managed(watch)
    assert plan.view(research.project_id)["changes"][0]["evidence"][0]["quote"]
    assert research.pipeline.questions.view(research.project_id) == []


def test_relation_aliases_require_same_quote_and_survive_projection(research):
    call(research, "pw_search", query="Synthetic Optics", category="company")
    quote = "Synthetic Optics 开发激光模组（SYN-LASER）。"
    research.reader.read = lambda _url: {"status": "article_body", "text": quote}
    call(research, "pw_read", source_id="S1")
    args = dict(kind="relation", label="激光模组", source_id="S1", quote=quote, text=quote,
        relation_type="product", relation_target="激光模组", reason="公司产品",
        axis="asset", query="激光模组 进展")
    with pytest.raises(ValueError, match="relation_aliases"):
        call(research, "pw_record", **args, relation_aliases=["不存在的别名"])
    call(research, "pw_record", **args, relation_aliases=["SYN-LASER"])
    call(research, "pw_finish", gaps={})
    assert research.pipeline.monitoring_plan.items(research.project_id)[0]["relation_aliases"] == ["SYN-LASER"]


def test_public_profile_and_news_are_persisted_with_evidence(research):
    call(research, "pw_search", query="Synthetic Optics 产品", category="company")
    call(research, "pw_read", source_id="S1")
    record(research)
    record(research, kind="company")
    result = call(research, "pw_finish", gaps={"people": "任职时间未确认",
        "competitor": "未找到可引用比较", "industry": "尚缺行业来源", "policy": "尚缺政策来源"})
    assert result["status"] == "partial"
    assert research.store.get_public_profile(research.project_id)["facts"][0]["kind"] == "product"
    items = research.store.list_items(project_id=research.project_id)
    assert len(items) == 1 and items[0]["title"] == "Synthetic Optics 产品与团队"
    assert items[0]["body_status"] == "article_body"
    assert research.store.category_counts(research.project_id) == {"company": 1}
    assert research.store.list_events(project_id=research.project_id)[0]["summary"] == items[0]["summary"]
    assert result["coverage"]["products"]["status"] == "covered"
    assert result["coverage"]["people"]["status"] == "gap"
    assert research.store.get_public_profile(research.project_id)["facts"][0]["refs"][0]["evidence_version_id"]


def test_finish_without_research_and_foreign_reference_rejected(research):
    with pytest.raises(ValueError):
        call(research, "pw_finish", gaps={})
    with pytest.raises(ValueError):
        record(research)


@pytest.mark.parametrize("include_profile", [False, True])
def test_customer_application_is_an_attributed_profile_fact_not_automatic_news_projection(research, include_profile):
    from project_watch.studio.dsh_bootstrap import PROMPT
    from project_watch.studio.public_profile import CUSTOMER_PROFILE_GUIDANCE, SYSTEM
    assert CUSTOMER_PROFILE_GUIDANCE in PROMPT and CUSTOMER_PROFILE_GUIDANCE in SYSTEM
    quote = "Synthetic Optics 拟供科研机构试用，尚未形成采购。"
    research.collectors.web_search = lambda *a, **k: [{**Search().web_search("")[0], "snippet": quote}]
    call(research, "pw_search", query="Synthetic Optics 应用", category="company")
    args = dict(label="应用场景", source_id="S1", quote=quote, text=quote)
    call(research, "pw_record", kind="company", **args)
    if include_profile:
        call(research, "pw_record", kind="business", **args)
    call(research, "pw_finish", gaps={})
    assert research.store.category_counts(research.project_id) == {"company": 1}
    if not include_profile:
        assert not (research.store.get_public_profile(research.project_id) or {}).get("facts")
        return
    [fact] = research.store.get_public_profile(research.project_id)["facts"]
    assert fact["kind"] == "business" and fact["label"] == "应用场景"
    assert fact["text"] == quote and fact["refs"][0]["quote"] == quote
    assert fact["refs"][0]["evidence_version_id"]


def test_same_source_multiple_categories_and_later_runs_keep_one_event(research):
    from project_watch.studio.dsh_bootstrap import PublicBootstrap
    call(research, "pw_search", query="synthetic", category="company")
    record(research, kind="company")
    record(research, kind="industry")
    call(research, "pw_finish", gaps={})
    p, pid = research.pipeline, research.project_id
    [event] = p.store.list_events(project_id=pid)
    [item] = p.store.list_items(project_id=pid)
    assert len(item["extra"]["dsh_claims"]) == 2
    p.store.finish(research.execution.job["id"], lease_token=research.execution.job["lease_token"])
    p.store.enqueue(pid, "external", input_version="second")
    search = Search()
    search.web_search = lambda *a, **k: [{**Search().web_search("")[0],
        "url": "https://example.com/company#new-section", "published_at": "2020-01-01"}]
    newer = PublicBootstrap(p, JobExecution(p.store, p.store.claim()), search, Reader())
    newer.record_reviewer = research.record_reviewer
    call(newer, "pw_search", query="synthetic", category="policy")
    record(newer, kind="policy")
    call(newer, "pw_finish", gaps={})
    assert [e["id"] for e in p.store.list_events(project_id=pid)] == [event["id"]]
    assert [i["id"] for i in p.store.list_items(project_id=pid)] == [item["id"]]
    assert {c["kind"] for c in p.store.list_items(project_id=pid)[0]["extra"]["dsh_claims"]} == {"company", "industry", "policy"}


def test_old_category_key_and_dismissal_survive_new_projection(research):
    from project_watch.studio.dsh_bootstrap import digest
    call(research, "pw_search", query="synthetic", category="company")
    old_key = "dsh:" + digest(["https://example.com/company", "industry"])
    research.store.add_items(research.project_id, "industry", [{
        "key": old_key, "title": "旧分类记录", "url": "https://example.com/company#old",
        "source": "synthetic", "snippet": "", "published_at": "2020-01-01"}])
    item = research.store.item_by_key(research.project_id, old_key)
    eid = research.store.create_event(research.project_id,
        {"title": "旧分类记录", "category": "industry", "importance": "routine"}, [item["id"]])
    with research.store._connect() as db, db:
        db.execute("UPDATE studio_events SET status='dismissed' WHERE id=?", (eid,))
    record(research, kind="company")
    call(research, "pw_finish", gaps={})
    with research.store._connect() as db:
        assert db.execute("SELECT count(*) FROM studio_items WHERE project_id=?", (research.project_id,)).fetchone()[0] == 1
        row = db.execute("SELECT id,status FROM studio_events WHERE project_id=?", (research.project_id,)).fetchone()
    assert tuple(row) == (eid, "dismissed")
    assert research.store.item_by_key(research.project_id, old_key)["id"] == item["id"]


def test_unsupported_numbers_and_lost_intention_rejected(research):
    call(research, "pw_search", query="Synthetic Optics 订单", category="company")
    for text in ("2025年订单为500万元。", "2025年意向订单为900万元。"):
        with pytest.raises(ValueError):
            call(research, "pw_record", kind="business", label="订单", source_id="S1",
                 quote="2025年意向订单为500万元。", text=text)


def test_record_feedback_reports_all_fields_without_silently_fixing(research):
    research.collectors.web_search = lambda *a, **k: [{**Search().web_search("")[0],
        "snippet": "6月11日，Synthetic Optics（以下简称“公司”）拟申请上市。"}]
    call(research, "pw_search", query="synthetic", category="company")
    with pytest.raises(ValueError) as error:
        call(research, "pw_record", kind="milestone", source_id="S1", label="2026年上市",
            quote='6月11日，Synthetic Optics（以下简称"公司"）拟申请上市。',
            text="2026年6月11日，Synthetic Optics申请上市。")
    issues = error.value.issues
    assert {i["field"] for i in issues} == {"quote", "text", "label"}
    quote = next(i for i in issues if i["field"] == "quote")
    assert quote["suggested_quote"] == "6月11日，Synthetic Optics（以下简称“公司”）拟申请上市。"
    assert any("2026年" in str(i) for i in issues if i["code"] == "unsupported_numbers")
    assert any(i["code"] == "missing_qualifications" for i in issues)
    assert not research.state["records"]
    # Use original quote; do not borrow provider publication year as event year.
    call(research, "pw_record", kind="milestone", source_id="S1", label="上市申请",
         quote=quote["suggested_quote"], text="6月11日，Synthetic Optics拟申请上市。")
    assert research.state["records"][0]["refs"][0]["quote"] == quote["suggested_quote"]


def test_record_feedback_does_not_invent_quote_match(research):
    call(research, "pw_search", query="synthetic", category="company")
    with pytest.raises(ValueError) as error:
        call(research, "pw_record", kind="business", label="订单", source_id="S1",
             quote="公司已经获得了900万元订单。", text="公司已经获得了900万元订单。")
    assert not any("suggested_quote" in i for i in error.value.issues)
    assert not research.state["records"]


def test_unknown_latches_and_cannot_be_bypassed_with_another_query(research):
    def unknown(*args, **kwargs):
        raise HttpError("request_outcome_unknown")
    research.collectors.web_search = unknown
    with pytest.raises(HttpError):
        call(research, "pw_search", query="Synthetic Optics", category="company")
    research.collectors = Search()
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        call(research, "pw_search", query="Synthetic Optics changed", category="industry")


def test_no_arbitrary_read_shell_or_unknown_tool(research):
    for name, args in (("pw_read", {"source_id": "http://127.0.0.1/secrets"}),
                       ("shell", {"command": "echo nope"})):
        with pytest.raises(ValueError):
            research.tool(name, args)


def test_private_search_targets_do_not_become_public_evidence(research):
    research.collectors.web_search = lambda *a, **k: [
        {**Search().web_search("")[0], "url": url}
        for url in ("http://127.0.0.1/secret", "http://[::1]/secret",
                    "http://service.local/secret", "http://localhost./secret")]
    assert not call(research, "pw_search", query="Synthetic Optics", category="company")["sources"]
    assert not research.state["sources"]


def test_repeat_finish_is_idempotent_and_stops_further_tools(research):
    call(research, "pw_search", query="Synthetic Optics", category="company")
    record(research)
    first = call(research, "pw_finish", gaps={})
    assert call(research, "pw_finish", gaps={}) == first
    with pytest.raises(ValueError):
        call(research, "pw_search", query="more", category="company")
    assert len(research.store.get_public_profile(research.project_id)["facts"]) == 1


def test_pause_and_stale_lease_reject_writes(research):
    research.store.set_status(research.project_id, "paused")
    with pytest.raises(HttpError, match="project_paused"):
        call(research, "pw_search", query="Synthetic Optics", category="company")
    research.store.set_status(research.project_id, "active")
    with sqlite3.connect(research.store.db_path) as db:
        db.execute("UPDATE studio_jobs SET lease_until='2000-01-01' WHERE id=?",
                   (research.execution.job["id"],))
    with pytest.raises(RuntimeError, match="lease"):
        call(research, "pw_search", query="Synthetic Optics", category="company")


def test_existing_data_not_replaced(research):
    legacy = {"kind": "business", "label": "Existing", "text": "Existing public fact", "refs": []}
    research.store.put_public_profile(research.project_id, {"facts": [legacy]})
    call(research, "pw_search", query="Synthetic Optics", category="company")
    record(research)
    call(research, "pw_finish", gaps={})
    profile = research.store.get_public_profile(research.project_id)
    # Unversioned history survives verbatim, without claiming it is current evidence.
    assert profile['historical_facts'] == [legacy]
    assert len(profile['facts']) == 1 and profile['facts'][0]['refs'][0]['evidence_version_id']
    assert profile['dependency']['status'] == 'current'
    assert json.loads(research.store.get_state("dsh-bootstrap:" + research.project_id))["status"] == "partial"


def test_monitoring_query_and_partial_ui_projection(research):
    from project_watch.studio import progress, runtime_health
    call(research, "pw_search", query="Synthetic Optics 产品", category="company")
    record(research, kind="watch", reason="跟踪产品交付进展", axis="asset", query="Synthetic Optics 激光模组",
           exit_rule="取得激光模组交付的公开记录")
    call(research, "pw_finish", gaps={})
    points = research.pipeline.monitoring_plan.items(research.project_id)
    assert points[0]["queries"] == ["Synthetic Optics 激光模组"]
    assert points[0]["exit_rule"] == "取得激光模组交付的公开记录"
    job = research.execution.job
    research.store.finish(job["id"], lease_token=job["lease_token"])
    with research.store._connect() as db:
        row = dict(db.execute("SELECT * FROM studio_jobs WHERE id=?", (job["id"],)).fetchone())
        assert runtime_health.projection(db, row)["state"] == "partial"
    assert progress.read(research.store, research.project_id)["state"] == "partial"


def test_watch_cannot_be_created_without_exit_rule(research):
    call(research, "pw_search", query="Synthetic Optics 产品", category="company")
    with pytest.raises(ValueError, match="exit_rule"):
        record(research, kind="watch", reason="产品", axis="asset", query="激光模组")
    assert not research.state["records"]


def test_legacy_checkpoint_missing_exit_is_partial_not_unbounded_topic(research):
    call(research, "pw_search", query="Synthetic Optics 产品", category="company")
    record(research, kind="watch", reason="产品", axis="asset", query="激光模组", exit_rule="公开交付记录")
    research.state["records"][0].pop("exit_rule")
    result = call(research, "pw_finish", gaps={})
    assert result["status"] == "partial"
    assert result["monitoring_scope"]["missing_exit_rules"] == ["激光模组"]
    assert not research.pipeline.monitoring_plan.items(research.project_id)


def test_incremental_peers_preserve_manual_fields_and_merge_evidence(research):
    peers = [{"name": f"Existing Peer {i}", "reason": "人工确认", "manual": True,
              "evidence": [{"url": f"https://example.com/{i}"}]} for i in range(9)]
    research.store.put_plan(research.project_id, {"competitors": peers, "custom": "keep"})
    fields = {"collection_phase": "complete"}
    research.store.merge_public_plan(research.project_id, fields, [])
    assert research.store.get_plan(research.project_id)["competitors"] == peers
    increment = [{"name": "existing peer 0", "reason": "新说明不能覆盖人工",
                  "evidence": [{"url": "https://example.com/new"}]},
                 {"name": "New Peer", "evidence": []}]
    for _ in range(2):
        research.store.merge_public_plan(research.project_id, fields, increment)
    plan = research.store.get_plan(research.project_id)
    assert len(plan["competitors"]) == 10 and plan["custom"] == "keep"
    first = plan["competitors"][0]
    assert first["manual"] and first["reason"] == "人工确认" and first["name"] == "Existing Peer 0"
    assert len(first["evidence"]) == 2


def test_concurrent_peer_increments_do_not_overwrite_each_other(research):
    from concurrent.futures import ThreadPoolExecutor

    research.store.put_plan(research.project_id, {"competitors": [{"name": "Keep Peer"}]})
    def merge(index):
        research.store.merge_public_plan(research.project_id, {}, [{"name": f"New Peer {index}"}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(merge, range(6)))
    assert len(research.store.get_plan(research.project_id)["competitors"]) == 7


def test_pipeline_opt_in_routes_to_dsh_no_legacy_fallback(research, monkeypatch):
    from project_watch.studio import dsh_runtime
    monkeypatch.setenv("PROJECT_WATCH_DSH_RUNTIME", "synthetic-runtime")
    called = []
    def runner(pipeline, execution, root):
        called.append(root.name)
        raise HttpError("request_outcome_unknown")
    monkeypatch.setattr(dsh_runtime, "run_bootstrap", runner)
    research.pipeline.collect = lambda *a, **k: pytest.fail("must not fall back to legacy collector")
    with pytest.raises(HttpError, match="request_outcome_unknown"):
        research.pipeline.run(research.execution.job)
    assert called == ["synthetic-runtime"]


def test_long_public_read_keeps_locatable_evidence(research):
    call(research, "pw_search", query="Synthetic Optics", category="company")
    quote = "Synthetic Optics 开发激光模组。"
    research.reader.read = lambda _url: {"status": "read", "text": "a" * 19990 + quote + "b" * 3000}
    call(research, "pw_read", source_id="S1")
    record(research)
    ref = research.state["records"][0]["refs"][0]
    assert ref["part_index"] == 1 and ref["start"] == 1490


@pytest.mark.parametrize("status,scope", [("article_body", "full"), ("partial_body", "partial")])
def test_real_reader_statuses_preserve_body_and_coverage(research, status, scope):
    call(research, "pw_search", query="Synthetic Optics", category="company")
    body = "正文独有：Synthetic Optics 开发激光模组。"
    research.reader.read = lambda _url: {"status": status, "text": body}
    source = call(research, "pw_read", source_id="S1")
    assert source["excerpt"] == body and source["read_scope"] == scope
    record(research)
    assert research.state["records"][0]["refs"][0]["read_scope"] == scope
    assert bool(research.state["errors"]) == (scope == "partial")


def test_dsh_phases_do_not_pretend_research_planning_is_a_judgment():
    from project_watch.studio.runtime_health import stage_phase
    assert stage_phase("DSH 阅读来源") == "read"
    assert stage_phase("DSH 检索公开信息") == "collect"
    assert stage_phase("DSH 研究中：选择下一步") is None


def test_peer_titles_do_not_become_duplicate_company_names(research):
    call(research, "pw_search", query="Synthetic Optics", category="competitor")
    for label in ("Example Peer：激光路线竞对", "Example Peer：同一公司另一个说明"):
        call(research, "pw_record", kind="peer", label=label, source_id="S1",
             quote="Synthetic Optics 开发激光模组。", text="Synthetic Optics 开发激光模组。",
             reason="合成测试中的产品可比关系")
    result = call(research, "pw_finish", gaps={})
    peers = research.store.get_plan(research.project_id)["competitors"]
    assert [p["name"] for p in peers] == ["Example Peer"]
    assert result["monitoring_scope"]["reason"]


def test_same_news_source_retains_both_findings(research):
    call(research, "pw_search", query="Synthetic Optics", category="company")
    record(research, kind="company")
    call(research, "pw_record", kind="company", label="意向订单", source_id="S1",
         text="2025年意向订单为500万元。", quote="2025年意向订单为500万元。")
    call(research, "pw_finish", gaps={})
    items = research.store.list_items(project_id=research.project_id)
    assert len(items) == 1
    assert "激光模组" in items[0]["summary"] and "意向订单" in items[0]["summary"]
    assert len(items[0]["extra"]["dsh_claims"]) == 2
    assert items[0]["body_status"] == "search_summary"


def capacity_records(research, count):
    call(research, "pw_search", query="Synthetic Optics", category="company")
    names = [f"合成产品{chr(0x7532 + i)}" for i in range(count)]
    quote = "Synthetic Optics 的产品包括" + "、".join(names) + "。"
    research.reader.read = lambda _url: {"status": "article_body", "text": quote}
    call(research, "pw_read", source_id="S1")
    for name in names:
        call(research, "pw_record", kind="relation", label=name, text=quote, source_id="S1", quote=quote,
             relation_type="product", relation_target=name, axis="asset", query=name, reason="公司产品")
    return names


def test_scope_capacity_preserves_records_and_partial_finish(research):
    from project_watch.studio.monitoring_plan import MAX_ITEMS
    names = capacity_records(research, MAX_ITEMS + 3)
    quote = research.state["records"][0]["text"]
    for kind in ("product", "company"):
        call(research, "pw_record", kind=kind, label=names[0], source_id="S1", quote=quote, text=quote)
    before = list(research.state["records"])
    result = call(research, "pw_finish", gaps={})
    assert result["status"] == "partial"
    assert result["monitoring_scope"]["admitted_count"] == MAX_ITEMS
    assert result["monitoring_scope"]["deferred_count"] == 3
    assert all(d["record_key"] in {r["key"] for r in before}
               and d["reason"] == "capacity_limit" for d in result["monitoring_scope"]["deferred"])
    assert research.state["records"] == before
    assert len(research.pipeline.monitoring_plan.items(research.project_id)) == MAX_ITEMS
    assert research.store.get_public_profile(research.project_id)["facts"]
    assert research.store.list_events(project_id=research.project_id)
    assert call(research, "pw_finish", gaps={}) == result
    assert research.execution.read_checkpoint("dsh_coverage")["partial"] is True


def test_scope_at_capacity_preserves_human_and_retired_items(research):
    from project_watch.studio.monitoring_plan import MAX_ITEMS
    names = capacity_records(research, 2)
    plan = research.pipeline.monitoring_plan
    common = dict(axis="asset", cadence="medium", tier="background", reason="人工设置")
    for i in range(MAX_ITEMS):
        plan.add(research.project_id, {**common, "text": f"人工方向{i}"}, actor="human", origin="human")
    before = plan.items(research.project_id)
    result = call(research, "pw_finish", gaps={})
    assert result["monitoring_scope"]["admitted_count"] == 0
    assert result["monitoring_scope"]["deferred_count"] == 2
    assert plan.items(research.project_id) == before
    # A retired relation with the same target stays closed on a fresh projection.
    plan.update(before[0]["id"], {"status": "retired"}, actor="human")
    relation = plan.add(research.project_id, {**common, "text": names[0], "scope_kind": "relation",
        "relation_type": "product", "relation_target": names[0], "queries": [names[0]],
        "sources": "https://example.com/company"}, actor="human", origin="human")
    plan.update(relation["id"], {"status": "retired"}, actor="human")
    projected = research.project_monitoring_scope(research.state["records"])
    assert projected["admitted_count"] == 1
    assert next(w for w in plan.items(research.project_id) if w["id"] == relation["id"])["status"] == "retired"


def test_scope_section_capacity_and_duplicate_candidates(research):
    from project_watch.studio.monitoring_plan import MAX_PER_SECTION
    capacity_records(research, 1)
    row = research.state["records"][0]
    scopes = [{**row, "kind": "watch", "key": str(i), "label": f"合成研究问题{i}",
               "exit_rule": "取得公开结果"} for i in range(MAX_PER_SECTION + 2)]
    for scope in scopes:
        scope.pop("relation_target", None)
        scope.pop("relation_type", None)
    scopes += [scopes[-1]]
    result = research.project_monitoring_scope(scopes)
    assert result["admitted_count"] == MAX_PER_SECTION
    assert result["deferred_count"] == 2
    assert len(research.pipeline.monitoring_plan.items(research.project_id)) == MAX_PER_SECTION


def test_scope_projection_does_not_swallow_unrelated_validation(research, monkeypatch):
    capacity_records(research, 1)
    def invalid(*args, **kwargs):
        raise ValueError("unrelated validation")
    monkeypatch.setattr(type(research.pipeline.monitoring_plan), "add", invalid)
    with pytest.raises(ValueError, match="unrelated validation"):
        call(research, "pw_finish", gaps={})
    assert research.state["finished"] is None


def test_scope_capacity_rechecked_after_another_writer_allocates(research, monkeypatch):
    from project_watch.studio.monitoring_plan import MAX_ITEMS
    capacity_records(research, 1)
    plan = research.pipeline.monitoring_plan
    original = type(plan).add
    def raced(self, pid, fields, **kwargs):
        for i in range(MAX_ITEMS):
            original(self, pid, {"text": f"并发人工方向{i}", "axis": "asset"}, actor="human", origin="human")
        return original(self, pid, fields, **kwargs)
    monkeypatch.setattr(type(plan), "add", raced)
    result = call(research, "pw_finish", gaps={})
    assert result["status"] == "partial"
    assert result["monitoring_scope"]["deferred_count"] == 1
    assert len(plan.items(research.project_id)) == MAX_ITEMS
