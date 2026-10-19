from datetime import date

from project_watch.studio import track

TODAY = date(2026, 10, 9)
PROJECT = {"name": "合成视界", "short_name": "合成视界", "industry": "激光雷达"}


def ev(title, day, importance="notable", source="某媒体", url="https://news.example/x"):
    return {"title": title, "summary": title, "url": url, "source": source, "importance": importance,
            "event_at": f"{day}T08:00:00+00:00", "created_at": f"{day}T08:00:00+00:00",
            "sources": [{"title": title, "url": url, "source": source}]}


def build(**kw):
    args = dict(project=PROJECT, industry=None, industry_events=[], competitor_events=[], policy_events=[],
                commercial_records=[], today=TODAY)
    args.update(kw)
    return track.build(**args)


def cell(data, key):
    return next(c for c in data["cells"] if c["key"] == key)


def test_quarters_and_series():
    assert track._quarters(TODAY) == ["25Q3", "25Q4", "26Q1", "26Q2", "26Q3", "26Q4"]
    s = track._series(["2026-10-01", "2026-08-01", "2026-08-02", "2024-01-01"], TODAY)
    assert [x["value"] for x in s] == [0, 0, 0, 0, 2, 1]


def test_nothing_collected_is_unknown_not_a_guess():
    data = build()
    assert all(c["tone"] == "unknown" and c["direction"] == "信息不足" for c in data["cells"])
    assert data["summary"] == "" and data["balance"] == "方向不一"


def test_demand_prefers_cited_metric():
    industry = {"metrics": [{"name": "装车量同比增长", "period": "2026 1-9月", "value": 78, "unit": "%",
                             "refs": [{"url": "https://a.example", "source": "高工产研", "date": "2026-10-02"}]},
                            {"name": "单价", "period": "2026", "value": 900, "unit": "元", "refs": []}]}
    c = cell(build(industry=industry), "demand")
    assert (c["direction"], c["tone"], c["value"]) == ("变大", "good", "+78%")
    assert c["sub"] == "装车量同比增长 · 2026 1-9月"
    assert c["evidence"][0]["source"] == "高工产研"


def test_demand_from_report_titles_needs_two():
    c = cell(build(industry_events=[ev("激光雷达装车量同比增长", "2026-09-01")]), "demand")
    assert c["tone"] == "unknown"
    c = cell(build(industry_events=[ev("激光雷达装车量同比增长", "2026-09-01"), ev("激光雷达出货创新高", "2026-08-01")]), "demand")
    assert c["direction"] == "报道信号" and c['tone'] == 'neutral'


def test_capital_dedupes_company_round_and_compares_windows():
    records = [{"kind": "funding", "company": "合成甲科技有限公司", "round": "C轮", "date": "2026-09-01", "amount": "数亿元", "provider": "xiniu"}]
    reports = [ev("合成甲完成5亿元C轮融资", "2026-09-02"), ev("合成丁完成天使轮融资", "2026-08-20"),
               ev("合成戊完成A轮融资", "2026-07-30"), ev("合成己完成B轮融资", "2026-05-01")]
    c = cell(build(commercial_records=records, industry_events=reports), "capital")
    assert c["value"] == "近 90 天 3 起" and c["sub"] == "此前 90 天 1 起"
    assert (c["direction"], c["tone"]) == ("升温", "good")
    assert any(e["source"] == "烯牛" for e in c["evidence"]) or c["sources"] >= 1


def test_capital_too_few_events():
    c = cell(build(industry_events=[ev("合成丁完成天使轮融资", "2026-08-20")]), "capital")
    assert c["tone"] == "unknown" and c["value"] == "近 180 天 1 起" and c["sub"] == "事件太少，暂不判断方向"


def test_price_down_is_headwind_with_percentage():
    c = cell(build(competitor_events=[ev("合成甲发布新方案，价格下探20%", "2026-10-05")],
                   industry_events=[ev("激光雷达价格战持续", "2026-09-17")]), "price")
    assert (c["direction"], c["tone"], c["value"]) == ("下行", "bad", "降价 20%")
    assert "毛利" in c["for_us"]


def test_policy_reads_support_or_restriction():
    c = cell(build(policy_events=[ev("工信部发布准入试点第二批名单", "2026-09-29")]), "policy")
    assert (c["direction"], c["tone"]) == ("顺风", "good")
    c = cell(build(policy_events=[ev("监管部门约谈多家企业，从严规范测试", "2026-09-29")]), "policy")
    assert (c["direction"], c["tone"]) == ("逆风", "bad")


def test_summary_balance():
    data = build(policy_events=[ev("工信部发布准入试点第二批名单", "2026-09-29")],
                 competitor_events=[ev("合成甲价格下探20%", "2026-10-05")])
    assert data["summary"] == "价格下行、政策顺风" and data["balance"] == "方向不一"
