from datetime import date

from project_watch.studio import red_flags

TODAY = date(2026, 10, 9)
PROJECT = {"name": "合成视界", "short_name": "合成视界", "legal_name": "合成视界科技有限公司", "website": "hecheng.example"}
PEERS = [{"name": "合成竞品乙", "aliases": []}]


def ev(title, day, source="某媒体", url="https://news.example/1", eid=None):
    return {"id": eid or title, "title": title, "summary": title, "url": url, "source": source,
            "event_at": f"{day}T08:00:00+00:00", "created_at": f"{day}T08:00:00+00:00",
            "sources": [{"title": title, "url": url, "source": source}]}


def build(**kw):
    args = dict(project=PROJECT, peers=PEERS, company_events=[], competitor_events=[], event_states=[],
                site_changes=[], site_pages=[], silence=[], today=TODAY)
    args.update(kw)
    return red_flags.build(**args)


def test_classify_levels_and_denials():
    assert red_flags.classify("合成视界被起诉侵犯专利") == ("高", "起诉")
    assert red_flags.classify("合成视界CTO离职")[0] == "中"
    assert red_flags.classify("合成视界否认裁员传闻")[0] == "低"
    assert red_flags.classify("合成视界官网招聘页下线")[0] == "低"
    assert red_flags.classify("合成视界新产线首台产品下线") is None
    assert red_flags.classify("合成视界获定点") is None


def test_only_reports_led_by_the_company_and_within_window():
    data = build(company_events=[ev("合成视界被起诉侵犯专利", "2026-09-01"),
                                 ev("行业观察：多家企业裁员，合成视界未受影响", "2026-09-02"),
                                 ev("合成视界被罚款", "2025-12-01")],
                 competitor_events=[ev("合成竞品乙被曝拖欠供应商货款", "2026-09-27", source="界面新闻")])
    assert [f["title"] for f in data["company"]] == ["合成视界被起诉侵犯专利"]
    assert data["peers"][0]["level"] == "高" and data["peers"][0]["source"] == "界面新闻"
    assert data["counts"] == {"company": 1, "peers": 1, "high": 1}


def test_denial_note_and_sorting():
    data = build(company_events=[ev("合成视界否认裁员传闻", "2026-10-01"), ev("合成视界高管离职", "2026-08-01"),
                                 ev("合成视界被立案调查", "2026-07-01")])
    assert [f["level"] for f in data["company"]] == ["高", "中", "低"]
    assert data["company"][2]["note"] == "公司已回应否认"


def test_event_states_team_page_and_silence():
    states = [{"support": "source_assertion", "state": "delayed", "name": "二期产线", "effective_at": "2026-09-10",
               "history": [{"subject_name": "合成视界科技有限公司"}]},
              {"support": "pending", "state": "cancelled", "name": "某订单", "effective_at": "2026-09-10",
               "history": [{"subject_name": "合成视界"}]}]
    pages = [{"id": "p1", "kind": "team"}, {"id": "p2", "kind": "news"}]
    changes = [{"page_id": "p1", "detected_at": "2026-10-07T01:00:00+00:00", "removed": ["CTO 李四"], "added": ["CFO 赵六"], "url": "https://hecheng.example/team"},
               {"page_id": "p2", "detected_at": "2026-10-07T01:00:00+00:00", "removed": ["旧新闻"], "added": [], "url": ""}]
    silence = [{"kind": "thread_stalled", "level": "watch", "title": "产能线索停滞：二期产线", "detail": "最近记录 2026-06-01"}]
    data = build(event_states=states, site_pages=pages, site_changes=changes, silence=silence)
    kinds = [f["kind"] for f in data["company"]]
    assert kinds == ["site_team", "event_state", "silence"]
    assert data["company"][0]["title"] == "官网管理团队页删除：CTO 李四；新增：CFO 赵六"
    assert data["company"][1]["title"] == "二期产线：延期"
    assert data["company"][2]["source"] == "该有却没有"


def test_nothing_found_is_empty_not_an_error():
    data = build()
    assert data["company"] == [] and data["peers"] == []


def test_no_verify_wording():
    data = build(company_events=[ev("合成视界被起诉", "2026-09-01")])
    text = str(data)
    assert "核实" not in text and "核验" not in text
