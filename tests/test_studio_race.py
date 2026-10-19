from datetime import date

from project_watch.studio import race

TODAY = date(2026, 10, 9)
PROJECT = {"name": "合成视界", "short_name": "合成视界", "legal_name": "合成视界科技有限公司", "industry": "激光雷达光学",
           "website": "www.hecheng.example", "aliases": []}


def ev(title, day, *, url="https://news.example/a", source="某媒体", importance="notable", impact=""):
    return {"title": title, "summary": title, "url": url, "source": source, "importance": importance, "impact": impact,
            "event_at": f"{day}T08:00:00+00:00", "created_at": f"{day}T08:00:00+00:00",
            "sources": [{"title": title, "url": url, "source": source}]}


def build(**kw):
    args = dict(project=PROJECT, plan={"competitors": [{"name": "合成竞品甲", "reason": "客户重叠"}]},
                peers=[{"name": "合成竞品甲", "aliases": [], "note": "客户重叠"}], company_events=[],
                competitor_events=[], threads=[], event_states=[], commercial_records=[], rd_items=[], today=TODAY)
    args.update(kw)
    return race.build(**args)


def test_template_by_sector():
    assert race.template_for({"industry": "创新药"}, None) == "pharma"
    assert race.template_for({"industry": "企业服务 SaaS"}, None) == "software"
    assert race.template_for({"industry": "激光雷达光学"}, None) == "hardware"
    assert race.template_for({"industry": "其他"}, {"is_biomed": True}) == "pharma"


def test_plans_and_forecasts_never_count_as_reached():
    assert race.classify_step("合成竞品甲预计明年量产", "hardware") is None
    assert race.classify_step("合成竞品甲拟启动定点项目", "hardware") is None
    assert race.classify_step("合成竞品甲实现量产交付", "hardware") == 4
    assert race.classify_step("合成药业 III期临床完成首例患者给药", "pharma") == 4
    assert race.classify_step("合成药业 II期临床启动", "pharma") == 3


def test_peer_step_needs_the_company_to_lead_the_title():
    data = build(competitor_events=[
        ev("行业观察：合成竞品甲等厂商量产在即", "2026-08-01"),
        ev("某车企激光雷达供应商合成竞品甲已量产", "2026-08-02"),
        ev("合成竞品甲获某车企定点", "2026-05-01", url="https://www.ccgp.gov.cn/x"),
    ])
    peer = data["rows"][1]
    assert peer["stage"]["label"] == "定点"
    assert peer["stage"]["basis"] == "title"
    assert peer["stage"]["grade"] == "公示"


def test_pace_only_from_the_step_directly_below():
    data = build(competitor_events=[ev("合成竞品甲送样某车企", "2025-01-10"), ev("合成竞品甲获某车企定点", "2026-05-01")])
    assert data["rows"][1]["stage"].get("previous") is None
    data = build(competitor_events=[ev("合成竞品甲XG-500进入某车企验证测试", "2025-11-10"), ev("合成竞品甲XG-500获某车企定点", "2026-05-01")])
    stage = data["rows"][1]["stage"]
    assert stage["previous"] == {"label": "试用", "date": "2025-11-10"}
    assert data["rows"][1]["pace_months"] == 6


def test_vendor_funding_preferred_and_kept_as_text():
    records = [{"kind": "funding", "company": "合成竞品甲", "round": "C轮", "amount": "数亿元人民币", "date": "2026-06-18",
                "investors": "某产业基金", "provider": "xiniu"},
               {"kind": "funding", "company": "合成竞品甲", "round": "C轮", "amount": "5亿元", "date": "2026-06-20",
                "investors": "", "provider": "wind"}]
    data = build(commercial_records=records, competitor_events=[ev("合成竞品甲完成5亿元C轮融资，某产业基金领投", "2026-06-19")])
    f = data["rows"][1]["funding"]
    assert f["grade"] == "数据商" and f["amount"] == "5亿元" and f["round"] == "C轮"
    assert data["rows"][1]["months_since_funding"] == 4


def test_title_funding_parse_when_no_vendor_record():
    data = build(company_events=[ev("合成视界完成3亿元B轮融资，红杉中国领投", "2026-10-06")])
    f = data["rows"][0]["funding"]
    assert (f["round"], f["amount"], f["lead"], f["grade"]) == ("B轮", "3亿元", "红杉", "媒体")
    assert data["rows"][0]["recent"]["funding"] is True


def test_missing_data_stays_missing():
    data = build()
    peer = data["rows"][1]
    assert peer["stage"] is None and peer["funding"] is None and peer["move"] is None
    assert peer["rd"] == {"papers": None, "patents": None, "collected": False}
    assert data["rows"][0]["rd"]["patents"] is None  # no patent source ever returned anything
    assert data["comparable"] == 0


def test_storyline_and_company_promo_grade():
    threads = [{"kind": "客户与订单", "support": "source_assertion", "steps": [{"stage": "定点/中标", "date": "2026-10-04", "title": "获某车企定点", "url": "https://hecheng.example/news/1"}]}]
    data = build(threads=threads)
    s = data["rows"][0]["stage"]
    assert (s["label"], s["basis"], s["grade"]) == ("定点", "storyline", "公司宣传")


def test_key_move_prefers_major_within_90_days():
    data = build(competitor_events=[ev("合成竞品甲获专精特新认定", "2026-09-25"),
                                    ev("合成竞品甲发布新方案，价格下探20%", "2026-10-05", importance="major", impact="压缩报价空间"),
                                    ev("合成竞品甲完成A轮融资", "2026-01-01", importance="major")])
    move = data["rows"][1]["move"]
    assert move["title"].startswith("合成竞品甲发布新方案") and move["impact"] == "压缩报价空间"


def test_peers_respect_exclusions_and_confirmations():
    rel = {"items": [{"peer_name": "合成竞品乙", "peer_names": ["合成竞品乙", "Rival B"], "origin": "user_override"},
                     {"peer_name": "合成竞品丁", "origin": "auto_research", "support": "pending", "status": "current"}],
           "exclusions": [{"company_wide": True, "peer_names": ["合成竞品丙"]}]}
    plan = {"competitors": [{"name": "合成竞品甲", "reason": "客户重叠"}, {"name": "合成竞品丙"}, {"name": "合成视界"}]}
    peers = race.peers_for(PROJECT, plan, rel)
    assert [p["name"] for p in peers] == ["合成竞品乙", "合成竞品甲"]
    assert peers[0]["note"] == "已确认跟踪" and peers[0]["aliases"] == ["Rival B"]


def _row(name, *, step=None, day=None, rnd=None, fday=None, move=None, mday=None, imp="major", is_self=False):
    stage = {"index": step, "label": race.TEMPLATES["hardware"]["steps"][step], "date": day} if step is not None else None
    funding = {"round": rnd, "date": fday} if rnd else None
    mv = {"title": move, "date": mday, "importance": imp} if move else None
    return {"name": name, "is_self": is_self, "stage": stage, "funding": funding,
            "months_since_funding": race._months_between(fday, TODAY) if fday else None, "move": mv}


def test_round_rank_orders_rounds():
    assert race.round_rank("C轮") > race.round_rank("B+轮") > race.round_rank("B轮") > race.round_rank("Pre-A轮")
    assert race.round_rank("战略融资") is None


def test_position_sentence_has_place_progress_capital_threat():
    rows = [_row("合成视界", step=3, day="2026-10-04", rnd="B轮", fday="2026-10-06", is_self=True),
            _row("甲", step=4, day="2026-06-11", rnd="C轮", fday="2026-06-18", move="甲发布新方案，价格下探20%", mday="2026-10-05"),
            _row("乙", step=2, day="2026-03-13", rnd="A+轮", fday="2025-09-04"),
            _row("丙", step=3, day="2025-12-23", rnd="B轮", fday="2025-11-23")]
    rows[0]['stage']['previous'] = {'label': '试用', 'date': '2026-05-01'}
    p = race.position(rows, TODAY)
    assert p["ranked"] is False
    assert p["sentence"] == ("公开记录显示“定点”，近 90 天上了一级台阶（定点）。刚完成B轮。"
                             "同行近期进展：甲发布新方案，价格下探20%（10-05）。")
    assert [c["label"] for c in p["chips"]] == ["已披露阶段", "资本", "近 30 天同行进展"]


def test_position_without_enough_peers_states_facts_without_rank():
    rows = [_row("合成视界", step=3, day="2025-11-01", is_self=True), _row("甲")]
    p = race.position(rows, TODAY)
    assert p["ranked"] is False
    assert "第" not in p["parts"][0]["text"]
    assert "未见下一级" not in p["parts"][0]["text"]
    assert not any(x["kind"] == "capital" for x in p["parts"])  # no financing record: nothing is said


def test_position_capital_lead_and_no_threat():
    rows = [_row("合成视界", rnd="C轮", fday="2026-01-01", is_self=True), _row("甲", rnd="B轮", fday="2026-02-01"),
            _row("乙", move="乙参加展会", mday="2026-10-01", imp="routine")]
    p = race.position(rows, TODAY)
    assert p["parts"][0]["text"] == "C轮已过去 9 个月"  # no stage record: the stage part is left out
    assert not any(x["kind"] == "threat" for x in p["parts"])


def test_reported_outlets_counted_once_per_outlet():
    e = ev("合成竞品甲获某车企定点", "2026-05-01")
    e["sources"] = [{"title": e["title"], "url": "https://a.example/1", "source": "汽车之心"},
                    {"title": e["title"], "url": "https://a.example/2", "source": "汽车之心"},
                    {"title": e["title"], "url": "https://b.example/1", "source": "盖世汽车"}]
    stage = build(competitor_events=[e])["rows"][1]["stage"]
    assert stage["outlets"] == ["汽车之心", "盖世汽车"]


def test_extras_team_and_customers_from_public_records():
    public = [{"kind": "person", "label": "创始人", "text": "张三，曾任某激光雷达公司光学总监", "date": "",
               "refs": [{"url": "https://36kr.example/1", "source": "36氪", "published_at": "2026-09-01T00:00:00+00:00"}]},
              {"kind": "product", "label": "产品", "text": "LX-905", "refs": []}]
    data = build(public_facts=public,
                 company_events=[ev("合成视界905nm发射模组获某新势力车企定点", "2026-10-04"),
                                 ev("合成视界计划拿下某车企订单", "2026-09-01")],
                 competitor_events=[ev("合成竞品甲与某Tier1签署战略合作", "2026-10-01"),
                                    ev("合成竞品甲CTO离职", "2026-08-30"),
                                    ev("行业观察：合成竞品甲获定点", "2026-08-01")])
    me, peer = data["rows"][0]["extras"], data["rows"][1]["extras"]
    assert me["team"][0]["text"] == "创始人：张三，曾任某激光雷达公司光学总监" and me["team"][0]["source"] == "36氪"
    assert [(c["who"], c["what"]) for c in me["customers"]] == [("某新势力车企", "定点")]
    assert [(c["who"], c["what"]) for c in peer["customers"]] == [("某Tier1", "战略合作")]
    assert [t["text"] for t in peer["team"]] == ["合成竞品甲CTO离职"]


def test_extras_empty_when_no_public_record():
    data = build()
    assert data["rows"][1]["extras"] == {"team": [], "customers": []}


def test_competitor_trouble_is_an_opening_not_a_threat():
    rows = [_row("合成视界", step=3, day="2026-10-04", is_self=True),
            _row("乙", step=2, day="2026-03-13", move="乙被曝拖欠供应商货款", mday="2026-09-27"),
            _row("甲", step=4, day="2026-06-11", move="甲发布新方案，价格下探20%", mday="2026-10-05", imp="notable")]
    p = race.position(rows, TODAY)
    kinds = [x["kind"] for x in p["parts"]]
    assert kinds == ["position", "threat", "opening"]  # no financing record: no capital placeholder
    assert p["parts"][1]["text"].startswith("同行近期进展：甲发布新方案")
    assert p["parts"][2]["text"] == "同行负面消息：乙被曝拖欠供应商货款（09-27）"


def test_interview_is_not_a_team_change():
    data = build(competitor_events=[ev("合成竞品甲创始人谈降本", "2026-09-01"), ev("合成竞品甲新任CTO履新", "2026-09-02")])
    assert [t["text"] for t in data["rows"][1]["extras"]["team"]] == ["合成竞品甲新任CTO履新"]
