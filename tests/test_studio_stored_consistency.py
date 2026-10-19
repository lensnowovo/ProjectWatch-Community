from datetime import date
from types import SimpleNamespace

import pytest

from project_watch.studio import race
from project_watch.studio.public_profile import build_public_profile
from project_watch.studio.resolve import resolve_company
from project_watch.studio.settings import RegistryConfig


def test_profile_prioritizes_new_dated_sources_without_using_founding_date():
    old = {"title": "合成公司招聘", "url": "https://old.example/a", "snippet": "合成公司成立于2006年，制造天线", "published_at": "2008-01-01"}
    new = {"title": "合成公司产品", "url": "https://new.example/a", "snippet": "合成公司提供车载连接器", "published_at": "2026-01-01"}
    unknown = {"title": "合成公司简介", "url": "https://unknown.example/a", "snippet": "合成公司成立于2026年，提供光学产品"}

    class Model:
        ready = True

        def json(self, system, text, **kwargs):
            assert text.index(new["snippet"]) < text.index(old["snippet"]) < text.index(unknown["snippet"])
            return {"facts": []}

    result = build_public_profile({"name": "合成公司", "legal_name": "合成公司"}, SimpleNamespace(has_search=False), Model(), [unknown, old, new])
    assert result["sources"] == 3


def test_resolver_input_keeps_publication_date_separate_from_founding_date():
    class Search:
        has_search = True

        def web_search(self, *args, **kwargs):
            return [{"title": "合成公司", "source": "合成来源", "url": "https://source.example/a",
                     "snippet": "公司成立于2006年", "published_at": "2026-01-01"}]

    class Model:
        ready = True

        def json(self, system, text, **kwargs):
            assert "来源日期 2026-01-01" in text
            assert "公司成立于2006年" in text
            assert "成立日期不等于来源发布日期" in system
            return {"candidates": [{"legal_name": "合成公司"}]}

    resolve_company("合成公司", llm=Model(), collectors=Search(), http=None, registry=RegistryConfig())


def object_record(oid, name, aliases):
    return {"id": oid, "kind": "company", "visibility": "public", "name": name, "aliases": aliases}


def test_peer_uses_only_current_unambiguous_public_aliases():
    aliases = [{"name": "合成简称", "visibility": "public", "valid_from": "2020-01-01", "valid_to": ""},
               {"name": "旧品牌", "visibility": "public", "valid_to": "2020-01-01"},
               {"name": "未来品牌", "visibility": "public", "valid_from": "2099-01-01"},
               {"name": "私有称呼", "visibility": "internal"}, {"name": "共用名", "visibility": "public"}]
    peers = race.peers_for({"name": "本公司"}, {"competitors": [{"name": "合成连接系统有限公司"}]}, {},
                          public_objects=[object_record("p", "合成连接系统有限公司", aliases),
                                          object_record("q", "另一个主体", [{"name": "共用名", "visibility": "public"}])],
                          today=date(2026, 10, 10))
    assert peers[0]["aliases"] == ["合成简称"]
    assert race._leads("合成简称获车企定点", peers[0]["aliases"])


def test_peer_does_not_guess_abbreviation_or_cross_private_objects():
    peers = race.peers_for({"name": "本公司"}, {"competitors": [{"name": "合成甲"}]}, {}, public_objects=[
        object_record("p", "合成甲乙", [{"name": "合成甲", "visibility": "public"}]) | {"visibility": "internal"}])
    assert peers[0]["aliases"] == []


def test_peer_alias_exclusion_blocks_the_same_public_identity():
    obj = object_record("p", "合成甲有限公司", [{"name": "合成简称", "visibility": "public"}])
    result = race.peers_for({"name": "本公司"}, {"competitors": [{"name": "合成甲有限公司"}]},
                            {"exclusions": [{"company_wide": True, "peer_names": ["合成简称"]}]}, public_objects=[obj])
    assert result == []


def test_relation_does_not_reintroduce_expired_alias_from_legacy_labels():
    obj = object_record("p", "合成甲", [{"name": "旧简称", "visibility": "public", "valid_to": "2008-01-01"}])
    rows = {"items": [{"peer_id": "p", "peer_name": "合成甲", "peer_names": ["合成甲", "旧简称"], "origin": "user_override"}]}
    [peer] = race.peers_for({"name": "本公司"}, {}, rows, public_objects=[obj])
    assert peer["aliases"] == []


def build(**kwargs):
    args = dict(project={"name": "合成甲", "industry": "车载连接器线束"}, plan={}, peers=[], company_events=[],
                competitor_events=[], threads=[], event_states=[], commercial_records=[], rd_items=[], today=date(2026, 10, 10))
    return race.build(**(args | kwargs))


def test_cited_public_funding_prevents_contradictory_absence_sentence():
    fact = {"kind": "financing", "text": "完成数千万元A轮融资", "label": "A轮融资", "date": "2025-06-01",
            "refs": [{"url": "https://source.example/a", "source": "合成媒体", "quote": "完成数千万元A轮融资"}]}
    data = build(public_facts=[fact])
    funding = data["rows"][0]["funding"]
    assert funding["round"] == "A轮" and funding["amount"] == "数千万元"
    assert funding["url"] == "https://source.example/a"
    assert "暂无公开融资" not in data["position"]["sentence"]
    assert data["template"] == "hardware"


@pytest.mark.parametrize("text,day,refs", [("拟完成A轮融资", "2025-06-01", [{"url": "https://source.example/a"}]),
                                           ("完成A轮融资", "2099-01-01", [{"url": "https://source.example/a"}]),
                                           ("完成A轮融资", "2025-06-01", [])])
def test_planned_future_or_uncited_funding_cannot_be_promoted(text, day, refs):
    assert build(public_facts=[{"kind": "financing", "text": text, "date": day, "refs": refs}])["rows"][0]["funding"] is None


def test_empty_projection_does_not_assert_absence_of_business_events():
    sentence = build()["position"]["sentence"]
    assert sentence == ""  # nothing recorded: no placeholder and no claim that nothing happened
    assert not any(word in sentence for word in ("没有", "未见", "尚未整理"))


@pytest.mark.parametrize("quote,url", [("尚未完成A轮融资", "https://source.example/a"),
                                      ("拟完成A轮融资", "https://source.example/a"),
                                      ("完成A轮融资", "javascript:alert(1)")])
def test_funding_quote_must_be_realized_and_have_a_safe_source(quote, url):
    fact = {"kind": "financing", "text": "完成A轮融资", "date": "2025-06-01",
            "refs": [{"url": url, "quote": quote}]}
    assert build(public_facts=[fact])["rows"][0]["funding"] is None


def test_funding_amount_and_source_stay_bound_to_the_same_quote():
    fact = {"kind": "financing", "text": "完成十亿元A轮融资", "date": "2025-06-01", "refs": [
        {"url": "https://source.example/a", "quote": "完成A轮融资"},
        {"url": "https://source.example/b", "quote": "公司估值十亿元"}]}
    funding = build(public_facts=[fact])["rows"][0]["funding"]
    assert funding["amount"] == "未披露"
    assert funding["title"] == "完成A轮融资"
    assert funding["url"] == "https://source.example/a"


def test_future_or_invalid_source_dates_do_not_rank_above_valid_dates():
    from project_watch.studio.public_profile import recent_sources

    hits = [{"published_at": "2099-01-01"}, {"published_at": "2026-02-30"},
            {"published_at": "2025-01-01"}, {"snippet": "成立于2026年"}]
    assert recent_sources(hits)[0] == hits[2]


def test_race_get_reads_public_aliases_without_jobs_or_provider_requests(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.monitoring_contracts import Alias, ObjectInput

    def deny(*args, **kwargs):
        pytest.fail("read projection must not call providers")

    monkeypatch.setattr("project_watch.studio.http.Http.get", deny)
    monkeypatch.setattr("project_watch.studio.http.Http.post_json", deny)
    app = create_studio_app(tmp_path, start_worker=False)
    client = TestClient(app)
    pid = client.post("/api/studio/projects", json={"entity": {"legal_name": "合成公司"}, "consent": True}).json()["id"]
    application = app.state.studio_application
    store = app.state.studio_pipeline.store
    application.objects.save(pid, ObjectInput(kind="company", name="合成连接系统有限公司", visibility="public",
                              aliases=(Alias(name="合成简称", visibility="public"),)), actor="user")
    store.put_plan(pid, {"competitors": [{"name": "合成连接系统有限公司"}]})
    store.add_items(pid, "competitor", [{"key": "synthetic", "title": "合成简称获车企定点",
                    "url": "https://source.example/a", "source": "合成来源", "snippet": "", "published_at": "2026-01-01"}])
    item = store.untriaged(pid)[0]
    store.create_event(pid, {"category": "competitor", "title": item["title"], "summary": item["title"],
                       "importance": "notable", "tone": "neutral", "event_at": "2026-01-01"}, [item["id"]])
    before = client.get(f"/api/studio/projects/{pid}").json()["jobs"]
    for _ in range(2):
        response = client.get(f"/api/studio/projects/{pid}/race?template=hardware")
        assert response.status_code == 200
        peer = response.json()["rows"][1]
        assert peer["stage"]["label"] == "定点"
        assert peer["stage"]["url"] == "https://source.example/a"
    assert client.get(f"/api/studio/projects/{pid}").json()["jobs"] == before
