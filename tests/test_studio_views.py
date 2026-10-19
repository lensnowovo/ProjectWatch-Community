"""Financial view, competition/industry landscape and impact context. Synthetic data, no network."""

from __future__ import annotations

import json
import re

from project_watch.studio import finance, landscape
from project_watch.studio.normalize import normalize_metric

_COUNTER = iter(range(1, 10_000))


def _m(name, period, value, basis="actual", unit="万元", category="financial", created="2026-01-01"):
    return normalize_metric({"id": f"m{next(_COUNTER)}", "source_id": "s", "source_name": "bp.pdf", "locator": "p1", "name": name,
                             "period": period, "value": value, "unit": unit, "basis": basis, "category": category,
                             "created_at": created})


def test_statement_sections_and_ratios_are_rule_based():
    metrics = [_m("营业收入", "2024", 3200), _m("营业收入", "2025", 5800), _m("营业收入", "2026E", 12000, "forecast"),
               _m("营业成本", "2025", 3500), _m("净利润", "2025", -900), _m("销售费用", "2025", 800),
               _m("研发费用", "2025", 1500), _m("货币资金", "2025", 4200), _m("经营活动产生的现金流量净额", "2025", -1300),
               _m("签约客户", "2025", 6, unit="家", category="operating")]
    sections = {m["std_name"]: finance.section_of(m) for m in metrics}
    assert sections["营业收入"] == "income" and sections["货币资金"] == "balance"
    assert sections["经营活动现金流净额"] == "cashflow" and sections["签约客户"] == "operating"
    rows = {r["name"]: r["cells"] for r in finance.ratios(metrics)}
    assert rows["收入同比增长"]["2025"]["value"] == 81.2 and rows["收入同比增长"]["2025"]["basis"] == "actual"
    assert rows["收入同比增长"]["2026"]["basis"] == "forecast"
    assert rows["毛利率"]["2025"]["value"] == 39.7 and "营业成本" in rows["毛利率"]["2025"]["formula"]
    assert rows["净利率"]["2025"]["value"] == -15.5
    assert "期间费用率" not in rows
    assert rows["已披露费用占收入（不完整）"]["2025"]["value"] == 39.7


def test_key_numbers_and_runway():
    metrics = [_m("营业收入", "2024", 3200), _m("营业收入", "2025", 5800), _m("净利润", "2025", -900),
               _m("货币资金", "2025", 4200), _m("经营活动现金流净额", "2025", -1300)]
    k = finance.key_numbers(metrics)
    assert k["revenue"]["value"] == 5800 and k["revenue_growth"] == 81.2
    assert k["runway"]["months"] == 38.8 and k["runway"]["status"] == "ok" and len(k["runway"]["refs"]) == 2
    # Half-year burn is scaled by six months; a later actual replaces an older one for the same period.
    half = [_m("货币资金", "2025-06", 1200), _m("经营活动现金流净额", "2025H1", -1200)]
    assert finance.runway(half)["months"] == 6.0 and finance.runway(half)["status"] == "short"
    assert finance.runway([_m("货币资金", "2025", 900), _m("经营活动现金流净额", "2025", 300)])["status"] == "positive"
    assert finance.runway([_m("货币资金", "2025", 900)])["status"] == "unknown"
    assert finance.runway([_m("营业收入", "2025", 1)]) is None
    newer = [_m("营业收入", "2025", 5000, created="2026-01-01"), _m("营业收入", "2025", 5800, created="2026-03-01")]
    assert finance.key_numbers(newer)["revenue"]["value"] == 5800


def test_runway_uses_measured_cash_burn_before_net_loss():
    metrics = [_m("银行结余及现金", "2025-12-31", 50300), _m("货币资金", "2026-06-30", 39200),
               _m("经营活动所用现金净额", "2026H1", -8400), _m("净利润", "2026H1", -5040)]
    r = finance.runway(metrics)
    # Cash fell 11,100 in six months (1,850/month) — more than operating cash flow (1,400) or the loss (840).
    assert r["method"] == "cash_change" and r["burn"] == 1850.0 and r["months"] == 21.2
    assert [a["method"] for a in r["alternatives"]] == ["cash_change", "operating_cash_flow", "net_profit"]
    assert r["alternatives"][1]["months"] == 28.0 and r["alternatives"][2]["months"] == 46.7
    # Larger operating burn wins over a smaller balance decline (e.g. a loan drawn in between).
    r = finance.runway([_m("货币资金", "2025-12-31", 1000), _m("货币资金", "2026-06-30", 940),
                        _m("经营活动现金流净额", "2026H1", -600)])
    assert r["method"] == "operating_cash_flow" and r["months"] == 9.4 and r["status"] == "short"
    # A rising balance alone cannot measure consumption.
    r = finance.runway([_m("货币资金", "2025-12-31", 900), _m("货币资金", "2026-06-30", 1200)])
    assert r["months"] is None and r["status"] == "unknown"


def test_full_dates_share_month_end_columns_and_outflow_labels_keep_their_sign():
    assert _m("货币资金", "2025-12-31", 1)["period_key"] == _m("货币资金", "2025年12月", 1)["period_key"] == "2025M12"
    assert _m("货币资金", "2026年6月30日", 1)["period_key"] == "2026M06"
    outflow = _m("经营活动所用现金净额", "2025", 7684.6)
    assert outflow["std_name"] == "经营活动现金流净额" and outflow["value_norm"] == -7684.6
    assert _m("经营活动所得现金净额", "2025", 300)["value_norm"] == 300


def test_value_cited_on_most_pages_wins_within_one_report():
    rows = [_m("银行结余及现金", "2025-12-31", 50310), _m("银行结余及现金", "2025-12-31", 50306.6),
            _m("银行结余及现金", "2025-12-31", 9114.7), _m("银行结余及现金", "2025-12-31", 18704.4)]
    assert finance.key_numbers(rows)["cash"]["value"] in (50310, 50306.6)


def test_leverage_ratio_needs_both_balances_at_one_date():
    rows = {r["name"]: r["cells"] for r in finance.ratios(
        [_m("资产总值", "2025-12-31", 98689.5), _m("负债总额", "2025-12-31", 43063.5), _m("资产总值", "2026-06-30", 83318)])}
    assert rows["资产负债率"]["2025M12"]["value"] == 43.6 and "2026M06" not in rows["资产负债率"]


def test_forecast_only_period_is_not_a_key_number():
    k = finance.key_numbers([_m("营业收入", "2026", 9000, "forecast")])
    assert k["revenue"] is None and k["revenue_growth"] is None


def test_finance_does_not_mix_currency_scopes_or_profit_with_cash():
    metrics = [_m("营业收入", "2025", 100), _m("营业收入", "2025", 200, unit="万美元", created="2026-03-01"),
               _m("货币资金", "2024", 100), _m("货币资金", "2025-06", 600),
               _m("净利润", "2025H1", 20)]
    keys = finance.key_numbers(metrics)
    assert keys["revenue"]["value"] == 100
    assert keys["cash"]["value"] == 600 and keys["cash"]["period"] == "2025M06"
    assert keys["runway"]["status"] == "unknown"  # profit is not operating cash flow
    assert finance.runway([_m("货币资金", "2024", 100), _m("经营现金流", "2025", -1200)])["status"] == "unknown"
    assert finance.runway([_m("货币资金", "2025", -1), _m("净利润", "2025", -120)])["months"] is None
    subsidiary = {**_m("营业收入", "2025", 999), "scope": "某子公司"}
    assert finance.key_numbers([subsidiary])["revenue"] is None


def test_expense_ratio_requires_all_four_inputs_and_unknown_is_not_forecast():
    metrics = [_m("营业收入", "2025", 100), _m("销售费用", "2025", 10), _m("管理费用", "2025", 15),
               _m("研发投入", "2025", 20), _m("财务费用", "2025", 5)]
    rows = {r["name"]: r["cells"] for r in finance.ratios(metrics)}
    assert rows["期间费用率"]["2025"]["value"] == 50 and len(rows["期间费用率"]["2025"]["refs"]) == 5
    unknown = finance.ratios([_m("营业收入", "2025", 100, "unknown"), _m("净利润", "2025", 10)])
    assert unknown[0]["cells"]["2025"]["basis"] == "unknown"


def test_landscape_drops_valid_ids_with_fabricated_quotes_and_numeric_claims():
    evidence = landscape.Evidence()
    evidence.add(title="合成竞品甲融资5亿元", url="https://example.com/news", source="示例", date="2026-10-01",
                 text="装车量同比增长78%。", kind="industry")
    assert evidence.resolve(["E1"]) == []
    assert evidence.resolve([{"ref": "E1", "quote": "公司收入999亿元"}]) == []
    refs = evidence.resolve([{"ref": "E1", "quote": "装车量同比增长78%"}])
    assert landscape._supported("装车量同比增长78%", refs)
    assert not landscape._supported("装车量同比增长999%", refs)
    evidence.add(title="不安全来源", url="javascript:alert(1)", source="", date="", text="", kind="industry")
    assert len(evidence.items) == 1


def test_competitor_reports_have_topics_not_unverified_event_subjects():
    events = [{"id": "1", "title": "合成竞品甲完成5亿元C轮融资", "summary": "", "importance": "major", "event_at": "2026-10-01"},
              {"id": "2", "title": "合成竞品乙获某车企定点", "summary": "", "importance": "major", "event_at": "2026-09-01"},
              {"id": "3", "title": "合成竞品甲新方案价格下探20%", "summary": "", "importance": "notable", "event_at": "2026-08-01"},
              {"id": "4", "title": "行业论坛召开", "summary": "", "importance": "routine", "event_at": None}]
    moves = landscape.competitor_moves(events, ["合成竞品甲", "合成竞品乙"])
    assert all(not m["company"] and m["type"] == "其他" for m in moves)
    assert [m["topic"] for m in moves] == ["融资", "客户与订单", "产品与价格", "其他"]


class Model:
    """Answers competition/industry prompts, citing one valid and one invented evidence id."""

    ready = True

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []

    def json(self, system, user, max_tokens=0):
        self.prompts.append((system, user))
        ids = re.findall(r"\[(E\d+)\]", user)
        rival_ref = [{"ref": ids[0], "quote": "合成竞品甲完成5亿元C轮融资"}]
        growth_ref = [{"ref": ids[-1], "quote": "2026年前三季度装车量同比增长78%"}]
        if "公司资料，不生成竞争判断" in system:
            return {"summary": "竞品甲完成融资。", "summary_refs": rival_ref, "companies": [
                {"name": "光启视界", "is_self": True, "cells": {"技术路线": {"text": "905nm", "refs": [{"ref": "INT", "quote": "合成方案使用905nm技术"}]},
                                                              "主要客户": {"text": "两家整车厂", "refs": ["E999"]}}},
                {"name": "合成竞品甲", "cells": {"融资与股东": {"text": "C 轮 5 亿元", "refs": rival_ref},
                 "规模信号": {"text": "收入999亿元", "refs": rival_ref}},
                 "threat": "high", "threat_reason": "融资扩张带来竞争压力", "refs": rival_ref},
                {"name": "编造公司", "is_self": True, "cells": {"技术路线": {"text": "1550nm", "refs": rival_ref}}, "threat": "high",
                 "threat_reason": "无依据", "refs": rival_ref}],
                "claims": [{"claim": "成本领先", "verdict": "challenged", "text": "竞品融资可能加强研发", "refs": rival_ref},
                           {"claim": "交付最快", "verdict": "supported", "text": "无依据", "refs": []}]}
        return {"industry": "车载激光雷达", "heat": "升温", "heat_reason": "装车量增长", "heat_refs": growth_ref,
                "overview": "需求增长。", "overview_refs": growth_ref,
                "facts": [{"kind": "规模与增速", "text": "装车量同比增长78%", "refs": growth_ref},
                          {"kind": "政策", "text": "无依据", "refs": []}],
                "metrics": [{"name": "装车量同比", "period": "2026Q3", "value": 78, "unit": "%", "refs": growth_ref},
                            {"name": "编造", "period": "2026", "value": 999, "unit": "%", "refs": growth_ref}],
                "funding": [{"company": "合成竞品甲", "round": "C", "amount": "5亿元", "date": "", "refs": rival_ref}],
                "implications": [{"text": "需求增长利好量产爬坡", "refs": growth_ref}]}


class NoSearch:
    has_search = False


def test_landscape_keeps_only_cited_claims(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    from project_watch.domain import FragmentInput

    source = repo.add_source(project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64)
    repo.enqueue_intake(project.id, source.id)
    intake = repo.claim_next_intake()
    repo.complete_intake(intake.id, [FragmentInput(ordinal=1, locator="p1", text="合成方案使用905nm技术，成本领先。")])
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {"legal_name": "光启视界科技有限公司", "short_name": "光启视界"}, consent=True)
    store.put_plan(project.id, {"competitors": [{"name": "合成竞品甲", "reason": ""}], "industry_queries": ["激光雷达"]})
    store.put_profile(project.id, {"one_liner": "车载激光雷达光学模组", "highlights": [{"text": "成本领先", "refs": [{"source_id": source.id, "locator": "p1"}]}],
                                   "sections": [], "risks": [], "milestones": []})
    for key, category, title in (("c", "competitor", "合成竞品甲完成5亿元C轮融资"), ("i", "industry", "2026年前三季度装车量同比增长78%")):
        store.add_items(project.id, category, [{"key": key, "title": title, "url": f"https://news.example/{key}",
                                                 "source": "示例", "snippet": "", "published_at": "2026-10-01T00:00:00Z"}])
    items = store.untriaged(project.id)
    store.save_triage([{"id": i["id"], "relevant": True, "importance": "major", "summary": i["title"]} for i in items])
    for i in items:
        store.create_event(project.id, {"title": i["title"], "category": i["category"], "importance": "major",
                                        "summary": i["title"], "event_at": "2026-10-01T00:00:00Z"}, [i["id"]])
    model = Model()
    data = landscape.build(store, model, NoSearch(), project.id, lambda _: None)
    comp = data["competition"]
    assert [c["name"] for c in comp["companies"]] == ["光启视界"]
    [me] = comp["companies"]
    assert me["cells"]["技术路线"]["refs"][0]["source_id"] == source.id and me["cells"]["主要客户"]["text"] == ""
    [candidate] = comp["candidates"]
    assert candidate["name"] == "合成竞品甲" and candidate["review_status"] == "pending"
    assert candidate["refs"][0]["url"].startswith("https://news.example/") and not candidate.get("threat")
    assert comp["claims"] == []  # quote presence alone does not establish an advantage verdict
    ind = data["industry"]
    assert [f["text"] for f in ind["facts"]] == ["2026年前三季度装车量同比增长78%"]
    assert [m["name"] for m in ind["metrics"]] == ["装车量同比"] and ind["heat"] == "升温"
    assert data["moves"][0]["topic"] == "融资" and data["evidence_count"] == 2
    assert data["rejected_evidence"] > 0
    assert "成本领先" in model.prompts[0][1]
    store.put_landscape(project.id, data)
    assert store.get_landscape(project.id)["competition"]["summary"] == ""


def test_landscape_is_scheduled_weekly_after_news_exists(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    store.upsert_project(project.id, {"legal_name": "光启视界科技有限公司"}, consent=True)
    assert store.due_landscape_projects() == []  # no news collected yet
    store.touch(project.id, "last_external_at")
    assert store.due_landscape_projects() == [project.id]
    store.put_landscape(project.id, {"competition": {}})
    assert store.due_landscape_projects() == []
    with store._connect() as db:
        db.execute("UPDATE studio_landscape SET updated_at='2020-01-01T00:00:00+00:00'")
    assert store.due_landscape_projects() == [project.id]
    job = store.enqueue(project.id, "landscape")
    assert store.due_landscape_projects() == [] and job["kind"] == "landscape"


def test_triage_sees_internal_plans_for_impact(tmp_path):
    from project_watch.repository import ProjectRepository
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    repo = ProjectRepository(tmp_path / "db.sqlite3")
    project = repo.create_project("光启视界")
    store = StudioStore(repo.db_path)
    store.put_profile(project.id, {"one_liner": "车载激光雷达光学模组", "risks": [{"text": "芯片交期"}],
                                   "milestones": [{"date": "2026Q2", "text": "完成客户验收", "status": "planned"}]})
    pipeline = Pipeline(repo, store, SettingsStore(tmp_path / "settings.json"))
    text = pipeline._internal_background(project.id)
    assert "计划：2026Q2 完成客户验收" in text and "风险：芯片交期" in text

    class Capture:
        ready = True
        user = ""
        system = ""

        def json(self, system, user, max_tokens=0):
            Capture.user = user
            Capture.system = system
            return {"results": []}

    store.add_items(project.id, "competitor", [{"key": "k", "title": "竞品降价", "url": "https://n.example/1",
                                                 "source": "示例", "snippet": "", "published_at": None}])
    pipeline.triage(project.id, Capture(), {"legal_name": "光启视界科技有限公司"}, {"competitors": []}, lambda _: None)
    assert "内部背景" in Capture.user and "完成客户验收" in Capture.user
    assert "当前日期" in Capture.user and "BP 当时的计划" in Capture.system
    assert "不能据此声称收入目标已得到验证" in Capture.system


def test_detail_and_portfolio_include_finance(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app

    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    project = client.post("/api/studio/projects", json={"entity": {"legal_name": "光启视界"}, "consent": True}).json()
    store = app.state.studio_store
    store.replace_metrics(project["id"], ["s"], [
        {**_m("营业收入", "2024", 3200), "source_id": "s"}, {**_m("营业收入", "2025", 5800), "source_id": "s"}])
    detail = client.get(f"/api/studio/projects/{project['id']}").json()
    assert detail["finance"]["key_numbers"]["revenue_growth"] == 81.2
    assert set(detail["finance"]["sections"].values()) == {"income"} and detail["landscape"] is None
    [row] = client.get("/api/studio/projects").json()
    assert row["finance"]["revenue"]["value"] == 5800
    assert json.dumps(row["finance"], ensure_ascii=False)


def test_hk_ifrs_wording_maps_to_cas_statement_lines():
    hk = {"一般及行政开支": "管理费用", "销售开支": "销售费用", "研发开支": "研发投入", "税前亏损": "利润总额",
          "所得税开支": "所得税费用", "流动资产总值": "流动资产合计", "合约负债": "合同负债", "权益总额": "净资产",
          "融资活动所得现金净额": "筹资活动现金流净额", "现金流出净额": "现金及现金等价物净增加额"}
    for raw, std in hk.items():
        m = _m(raw, "2025", 100)
        assert m["std_name"] == std, raw
        assert finance.section_of(m) in ("income", "balance", "cashflow"), raw
    assert _m("税前亏损", "2025", 55000)["value_norm"] == -55000  # loss label carries the sign
    assert _m("现金流出净额", "2026H1", 11100)["value_norm"] == -11100  # outflow wording
    assert _m("融资活动所得现金净额", "2025", 300)["value_norm"] == 300


def test_layout_lists_every_statement_line_once_in_cas_order():
    layout = finance.view([])["layout"]
    assert [s["key"] for s in layout] == ["income", "balance", "cashflow"]
    names = [i["name"] for s in layout for g in s["groups"] for i in g["items"]]
    assert len(names) == len(set(names))
    income = [i["name"] for g in layout[0]["groups"] for i in g["items"]]
    assert income.index("营业收入") < income.index("毛利") < income.index("营业利润") < income.index("利润总额") < income.index("净利润")
    assert {i["name"] for g in layout[1]["groups"] for i in g["items"] if i["total"]} >= {"流动资产合计", "总资产", "总负债", "净资产"}


def test_cas_line_prefixes_and_rich_statement_lines_are_placed():
    assert _m("其中：利息费用", "2025", 10)["std_name"] == "利息费用"
    assert _m("减：营业成本", "2025", 10)["std_name"] == "营业成本"
    assert _m("加：其他收益", "2025", 10)["std_name"] == "其他收益"
    for raw, section in {"税金及附加": "income", "信用减值损失": "income", "营业外支出": "income", "应付职工薪酬": "balance",
                         "股本": "balance", "未分配利润": "balance", "资本开支": "cashflow", "偿还债务支付的现金": "cashflow",
                         "其中：利息费用": "income"}.items():
        assert finance.section_of(_m(raw, "2025", 1)) == section, raw
    eps = _m("基本每股收益", "2025", 0.6, unit="元/股")
    assert finance.section_of(eps) == "income" and eps["value_norm"] == 0.6
    key = {i["name"] for s in finance.layout() for g in s["groups"] for i in g["items"] if i["key"] or i["total"]}
    assert {"营业收入", "净利润", "货币资金", "总资产", "经营活动现金流净额"} <= key and "税金及附加" not in key
